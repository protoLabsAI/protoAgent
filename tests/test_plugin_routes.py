"""Operator plugin routes — the Direct enable/disable toggle (hot reload)."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def _wire(monkeypatch, *, enabled, disabled, meta, router_keys=()):
    """Fake the hot-reload apply + STATE; return a dict that captures the config patch.

    ``router_keys`` seeds the live-mount registry (``STATE.plugin_router_keys``,
    ``{(plugin_id, prefix), …}``) — the ground truth for "this plugin's router is
    already mounted" that the force re-install restart check reads (#942)."""
    captured: dict = {}
    fake = types.ModuleType("server.agent_init")

    def _apply(config=None, soul=None):
        captured["config"] = config
        return True, ["reloaded"]

    fake._apply_settings_changes = _apply
    monkeypatch.setitem(sys.modules, "server.agent_init", fake)

    import runtime.state as rs
    cfg = types.SimpleNamespace(plugins_enabled=list(enabled), plugins_disabled=list(disabled))
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_meta", meta, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_router_keys", set(router_keys), raising=False)
    return captured


def test_enable_moves_lists_and_hot_reloads(monkeypatch):
    captured = _wire(monkeypatch, enabled=["discord"], disabled=["github"],
                     meta=[{"id": "github", "views": []}])
    body = _client().post("/api/plugins/github/enabled", json={"enabled": True}).json()
    assert body == {"ok": True, "enabled": True, "reloaded": True, "restart_recommended": False}
    plugins = captured["config"]["plugins"]
    assert set(plugins["enabled"]) == {"discord", "github"}  # added, no dupes
    assert plugins["disabled"] == []                          # removed from disabled


def test_disable_moves_to_disabled(monkeypatch):
    captured = _wire(monkeypatch, enabled=["discord", "github"], disabled=[],
                     meta=[{"id": "github", "views": []}])
    body = _client().post("/api/plugins/github/enabled", json={"enabled": False}).json()
    assert body["enabled"] is False
    plugins = captured["config"]["plugins"]
    assert plugins["enabled"] == ["discord"]
    assert plugins["disabled"] == ["github"]


def test_enabling_a_view_plugin_does_not_recommend_restart(monkeypatch):
    # #822 hot-mounts the router that serves the view on the same reload, so enabling a
    # view-contributing plugin is fully live — NO restart (the P0 fix: enable → it works).
    _wire(monkeypatch, enabled=[], disabled=["boardy"],
          meta=[{"id": "boardy", "views": [{"id": "board"}]}])
    body = _client().post("/api/plugins/boardy/enabled", json={"enabled": True}).json()
    assert body == {"ok": True, "enabled": True, "reloaded": True, "restart_recommended": False}


def test_disabling_a_view_plugin_recommends_restart(monkeypatch):
    # DISABLE is the residual restart case — FastAPI can't unmount the view's router, so
    # the stale route lingers until a process restart.
    _wire(monkeypatch, enabled=["boardy"], disabled=[],
          meta=[{"id": "boardy", "views": [{"id": "board"}]}])
    body = _client().post("/api/plugins/boardy/enabled", json={"enabled": False}).json()
    assert body["enabled"] is False
    assert body["restart_recommended"] is True


def test_disabling_a_route_only_plugin_recommends_restart(monkeypatch):
    # A plugin with no views but a contributed router (e.g. delegates) still leaves a
    # stale route on disable → restart recommended.
    _wire(monkeypatch, enabled=["delegates"], disabled=[],
          meta=[{"id": "delegates", "views": [], "routers": 1}])
    body = _client().post("/api/plugins/delegates/enabled", json={"enabled": False}).json()
    assert body["restart_recommended"] is True


def test_disabling_a_plain_plugin_does_not_recommend_restart(monkeypatch):
    # A tools-only plugin (no view/route/surface) tears down cleanly on the reload — no restart.
    _wire(monkeypatch, enabled=["github"], disabled=[],
          meta=[{"id": "github", "views": []}])
    body = _client().post("/api/plugins/github/enabled", json={"enabled": False}).json()
    assert body["restart_recommended"] is False


# ── auto-enable on install (trust-by-default; install = enabled + running) ────────
def test_install_auto_enables_and_runs(monkeypatch):
    from graph.plugins import installer
    captured = _wire(monkeypatch, enabled=["delegates"], disabled=[], meta=[])
    monkeypatch.setattr(installer, "install",
                        lambda url, ref=None, **k: {"id": "spacetraders", "name": "SpaceTraders", "version": "1.0.0"})
    body = _client().post("/api/plugins/install",
                          json={"url": "https://github.com/protoLabsAI/spacetraders-plugin"}).json()
    assert body["enabled"] == ["spacetraders"] and body["reloaded"] is True and body["enable_error"] is None
    # added to plugins.enabled + persisted via the same _apply_settings_changes path the enable toggle uses
    assert set(captured["config"]["plugins"]["enabled"]) == {"delegates", "spacetraders"}


def test_install_bundle_enables_declared_members(monkeypatch):
    from graph.plugins import installer
    captured = _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {
        "bundle": "pm-stack", "installed": [{"id": "board"}, {"id": "browser"}], "enabled": ["board"]})
    body = _client().post("/api/plugins/install", json={"url": "https://x/pm-stack"}).json()
    assert body["enabled"] == ["board"]                       # the bundle's declared enable set
    assert captured["config"]["plugins"]["enabled"] == ["board"]


def test_install_bundle_without_declared_enable_enables_every_member(monkeypatch):
    from graph.plugins import installer
    _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {
        "bundle": "x", "installed": [{"id": "a"}, {"id": "b"}], "enabled": []})
    body = _client().post("/api/plugins/install", json={"url": "https://x/y"}).json()
    assert body["enabled"] == ["a", "b"]


def test_install_opt_out_stays_install_not_enable(monkeypatch):
    from graph.plugins import installer
    monkeypatch.setenv("PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE", "1")
    captured = _wire(monkeypatch, enabled=["delegates"], disabled=[], meta=[])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    body = _client().post("/api/plugins/install", json={"url": "https://x"}).json()
    assert body["enabled"] == [] and body["reloaded"] is False
    assert "config" not in captured                          # _apply_settings_changes never called


def test_install_succeeds_even_if_enable_reload_fails(monkeypatch):
    from graph.plugins import installer
    _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    sys.modules["server.agent_init"]._apply_settings_changes = lambda config=None, soul=None: (False, ["graph compile failed"])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    resp = _client().post("/api/plugins/install", json={"url": "https://x"})
    assert resp.status_code == 200                            # the install itself didn't 500
    body = resp.json()
    assert body["installed"]["id"] == "demo" and body["enabled"] == [] and "graph compile failed" in body["enable_error"]


# ── force re-install over a LIVE plugin can't hot-swap its router (#942) ──────────
def test_fresh_install_hot_mounts_no_restart(monkeypatch):
    # First install: nothing mounted yet, the reload hot-mounts the router (#822) —
    # fully live, no restart. The pre-#942 posture, preserved.
    from graph.plugins import installer
    _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "boardy"})
    body = _client().post("/api/plugins/install", json={"url": "https://x/boardy"}).json()
    assert body["reloaded"] is True
    assert body["restart_recommended"] is False


def test_force_reinstall_over_mounted_router_recommends_restart(monkeypatch):
    # The plugin's router is already mounted → the reload re-registers it and the
    # mount DROPS the new one (FastAPI can't swap in place) — the fresh routes don't
    # serve until a process restart. The response must say so, not claim hot-mount.
    from graph.plugins import installer
    _wire(monkeypatch, enabled=["boardy"], disabled=[], meta=[],
          router_keys={("boardy", "/plugins/boardy")})
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "boardy"})
    body = _client().post("/api/plugins/install", json={"url": "https://x/boardy", "force": True}).json()
    assert body["reloaded"] is True
    assert body["restart_recommended"] is True


def test_force_reinstall_over_disabled_lingering_router_recommends_restart(monkeypatch):
    # Disable doesn't unmount, so the router lingers with NO plugin_meta entry —
    # the mount registry is the signal that survives (the meta check alone misses it).
    from graph.plugins import installer
    _wire(monkeypatch, enabled=[], disabled=["boardy"], meta=[],
          router_keys={("boardy", "/plugins/boardy")})
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "boardy"})
    body = _client().post("/api/plugins/install", json={"url": "https://x/boardy", "force": True}).json()
    assert body["restart_recommended"] is True


def test_bundle_reinstall_flags_restart_only_for_mounted_members(monkeypatch):
    # A bundle re-install over one live member + one fresh member → restart (the
    # live member's routes are stale); builtin members are never fetched → ignored.
    from graph.plugins import installer
    _wire(monkeypatch, enabled=["board"], disabled=[], meta=[],
          router_keys={("board", "/plugins/board")})
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {
        "bundle": "pm-stack", "installed": [{"id": "board"}, {"id": "browser"}],
        "enabled": ["board", "browser"]})
    body = _client().post("/api/plugins/install", json={"url": "https://x/pm-stack", "force": True}).json()
    assert body["restart_recommended"] is True


def test_force_reinstall_purges_module_subtree(monkeypatch):
    # Parity with the update route: the reload re-execs the entry __init__, but a
    # multi-file plugin's submodules resolve through sys.modules — purge them so the
    # fresh checkout is what actually runs.
    from graph.plugins import installer
    from graph.plugins.loader import _plugin_module_name
    _wire(monkeypatch, enabled=["boardy"], disabled=[], meta=[],
          router_keys={("boardy", "/plugins/boardy")})
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "boardy"})
    mod = _plugin_module_name("boardy")
    monkeypatch.setitem(sys.modules, mod, types.ModuleType(mod))
    monkeypatch.setitem(sys.modules, mod + ".tools", types.ModuleType(mod + ".tools"))
    _client().post("/api/plugins/install", json={"url": "https://x/boardy", "force": True})
    assert mod not in sys.modules and (mod + ".tools") not in sys.modules


def test_update_route_flags_restart_for_disabled_lingering_router(monkeypatch):
    # The update route's restart heuristic also reads the mount registry now — a
    # disabled-but-still-mounted plugin (no meta) updating at its ref needs a restart.
    from graph.plugins import installer
    _wire(monkeypatch, enabled=[], disabled=["boardy"], meta=[],
          router_keys={("boardy", "/plugins/boardy")})
    monkeypatch.setattr(installer, "list_installed", lambda: [
        {"id": "boardy", "source_url": "https://x/boardy", "requested_ref": "v1"}])
    monkeypatch.setattr(installer, "install",
                        lambda url, ref=None, **k: {"id": "boardy", "version": "2", "resolved_sha": "b" * 40})
    body = _client().post("/api/plugins/boardy/update").json()
    assert body["reloaded"] is False                          # disabled → nothing to reload
    assert body["restart_recommended"] is True
