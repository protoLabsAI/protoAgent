"""Plugin update / version-awareness backend (ADR 0027 follow-on).

check_updates() over plugins.lock with `git ls-remote` mocked (behind / up-to-date /
pinned / error + the TTL cache), and the /api/plugins/{id}/update route with the
installer + reload mocked.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.plugins import installer

# A real (full) commit SHA and a different "latest" one.
_CUR = "a" * 40
_LATEST = "b" * 40


@pytest.fixture(autouse=True)
def _clear_cache():
    """The ls-remote TTL cache is module-level — wipe it around every test."""
    installer._lsremote_cache.clear()
    yield
    installer._lsremote_cache.clear()


def _lock(monkeypatch, plugins: list[dict], *, bundles: list[dict] | None = None):
    """Make installer._read_lock() return a fixed lock (no disk)."""
    monkeypatch.setattr(
        installer, "_read_lock",
        lambda: {"plugins": list(plugins), "bundles": list(bundles or [])},
    )


# ── check_updates() ───────────────────────────────────────────────────────────
def test_behind_when_latest_differs(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])
    monkeypatch.setattr(installer, "_git", lambda *a, **k: f"{_LATEST}\tHEAD")

    rows = installer.check_updates()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "demo"
    assert row["current_sha"] == _CUR
    assert row["latest_sha"] == _LATEST
    assert row["behind"] is True
    assert row["pinned"] is False
    assert row["error"] is None


def test_up_to_date_when_latest_equals(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])
    monkeypatch.setattr(installer, "_git", lambda *a, **k: f"{_CUR}\tHEAD")

    row = installer.check_updates()[0]
    assert row["latest_sha"] == _CUR
    assert row["behind"] is False
    assert row["error"] is None


def test_up_to_date_is_case_insensitive(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR.upper()},
    ])
    monkeypatch.setattr(installer, "_git", lambda *a, **k: f"{_CUR}\tHEAD")
    assert installer.check_updates()[0]["behind"] is False


def test_pinned_sha_skips_network(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": _CUR,
         "resolved_sha": _CUR},
    ])
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("ls-remote must NOT run for a pinned plugin")

    monkeypatch.setattr(installer, "_git", _boom)

    row = installer.check_updates()[0]
    assert row["pinned"] is True
    assert row["behind"] is False
    assert row["latest_sha"] is None
    assert row["error"] is None
    assert called["n"] == 0


def test_empty_ref_uses_head(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "",
         "resolved_sha": _CUR},
    ])
    seen: list[tuple] = []

    def _git(*args, **kw):
        seen.append(args)
        return f"{_LATEST}\tHEAD"

    monkeypatch.setattr(installer, "_git", _git)
    row = installer.check_updates()[0]
    assert row["behind"] is True
    # empty requested_ref → ls-remote <url> HEAD
    assert seen[0] == ("ls-remote", "https://x/y.git", "HEAD")


def test_error_when_ls_remote_fails(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])

    def _git(*a, **k):
        raise installer.InstallError("git ls-remote failed: could not read from remote")

    monkeypatch.setattr(installer, "_git", _git)
    row = installer.check_updates()[0]
    assert row["latest_sha"] is None
    assert row["behind"] is False
    assert "ls-remote failed" in row["error"]


def test_error_on_timeout(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])

    def _git(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=5.0)

    monkeypatch.setattr(installer, "_git", _git)
    row = installer.check_updates()[0]
    assert row["error"] is not None
    assert "timed out" in row["error"]
    assert row["behind"] is False


def test_error_when_no_source_url(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "", "requested_ref": "main", "resolved_sha": _CUR},
    ])

    def _boom(*a, **k):
        raise AssertionError("must not ls-remote a sourceless entry")

    monkeypatch.setattr(installer, "_git", _boom)
    row = installer.check_updates()[0]
    assert "no source_url" in row["error"]
    assert row["behind"] is False


def test_error_when_no_remote_sha_resolved(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "nope",
         "resolved_sha": _CUR},
    ])
    monkeypatch.setattr(installer, "_git", lambda *a, **k: "")  # ref matched nothing
    row = installer.check_updates()[0]
    assert row["latest_sha"] is None
    assert "could not resolve" in row["error"]


# ── TTL cache ─────────────────────────────────────────────────────────────────
def test_ttl_cache_avoids_second_ls_remote(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])
    calls = {"n": 0}

    def _git(*a, **k):
        calls["n"] += 1
        return f"{_LATEST}\tHEAD"

    monkeypatch.setattr(installer, "_git", _git)
    # within the TTL: two checks, only one ls-remote
    installer.check_updates()
    installer.check_updates()
    assert calls["n"] == 1


def test_ttl_cache_expires(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])
    calls = {"n": 0}

    def _git(*a, **k):
        calls["n"] += 1
        return f"{_LATEST}\tHEAD"

    monkeypatch.setattr(installer, "_git", _git)
    monkeypatch.setattr(installer, "_LSREMOTE_TTL_S", 0.0)  # everything is stale
    installer.check_updates()
    installer.check_updates()
    assert calls["n"] == 2


def test_cache_is_keyed_by_source_and_ref(monkeypatch):
    _lock(monkeypatch, [
        {"id": "a", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR},
        {"id": "b", "source_url": "https://x/y.git", "requested_ref": "dev",
         "resolved_sha": _CUR},
        {"id": "c", "source_url": "https://x/z.git", "requested_ref": "main",
         "resolved_sha": _CUR},
    ])
    calls = {"n": 0}

    def _git(*a, **k):
        calls["n"] += 1
        return f"{_LATEST}\tHEAD"

    monkeypatch.setattr(installer, "_git", _git)
    installer.check_updates()
    installer.check_updates()
    # 3 distinct (url, ref) keys → 3 ls-remotes the first pass, 0 the second
    assert calls["n"] == 3


# ── /api/plugins/{id}/update + /api/plugins/updates routes ───────────────────────
def _client():
    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def _wire_state(monkeypatch, *, enabled, disabled, meta):
    """Fake STATE.graph_config + plugin_meta and the hot-reload apply."""
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
    return captured


def test_updates_route_returns_check_updates(monkeypatch):
    monkeypatch.setattr(
        installer, "check_updates",
        lambda: [{"id": "demo", "behind": True, "pinned": False, "error": None}],
    )
    body = _client().get("/api/plugins/updates").json()
    assert body == {"plugins": [{"id": "demo", "behind": True, "pinned": False, "error": None}]}


def test_update_route_reinstalls_and_reloads(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR, "present": True},
    ])
    monkeypatch.setattr(installer, "live_plugins_dir", lambda: __import__("pathlib").Path("/tmp/none"))
    captured = _wire_state(monkeypatch, enabled=["demo"], disabled=[], meta=[{"id": "demo", "views": []}])

    install_calls: list = []

    def _install(url, ref=None, *, force=False, by="cli", allow=None):
        install_calls.append((url, ref, force))
        return {"id": "demo", "version": "0.2.0", "resolved_sha": _LATEST}

    monkeypatch.setattr(installer, "install", _install)

    body = _client().post("/api/plugins/demo/update").json()
    assert body["ok"] is True
    assert body["id"] == "demo"
    assert body["version"] == "0.2.0"
    assert body["resolved_sha"] == _LATEST
    assert body["reloaded"] is True
    assert body["restart_recommended"] is False
    # re-installed at its ref with force
    assert install_calls == [("https://x/y.git", "main", True)]
    # reloaded via the same _apply_settings_changes path the enable route uses
    assert captured["config"]["plugins"]["enabled"] == ["demo"]


def test_update_route_disabled_plugin_reinstalls_without_reload(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR, "present": True},
    ])
    captured = _wire_state(monkeypatch, enabled=[], disabled=["demo"], meta=[])

    monkeypatch.setattr(
        installer, "install",
        lambda *a, **k: {"id": "demo", "version": "0.2.0", "resolved_sha": _LATEST},
    )

    body = _client().post("/api/plugins/demo/update").json()
    assert body["ok"] is True
    assert body["resolved_sha"] == _LATEST
    assert body["reloaded"] is False
    # installed-but-disabled: nothing to reload, _apply not invoked
    assert "config" not in captured


def test_update_route_flags_restart_for_view_plugin(monkeypatch):
    _lock(monkeypatch, [
        {"id": "boardy", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR, "present": True},
    ])
    _wire_state(monkeypatch, enabled=["boardy"], disabled=[],
                meta=[{"id": "boardy", "views": [{"id": "board"}]}])
    monkeypatch.setattr(
        installer, "install",
        lambda *a, **k: {"id": "boardy", "version": "0.2.0", "resolved_sha": _LATEST},
    )
    body = _client().post("/api/plugins/boardy/update").json()
    assert body["reloaded"] is True
    assert body["restart_recommended"] is True


def test_update_route_404_on_unknown_id(monkeypatch):
    _lock(monkeypatch, [])
    _wire_state(monkeypatch, enabled=[], disabled=[], meta=[])
    resp = _client().post("/api/plugins/nope/update")
    assert resp.status_code == 404


def test_update_route_400_on_sourceless_id(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "", "requested_ref": "", "resolved_sha": _CUR,
         "present": True},
    ])
    _wire_state(monkeypatch, enabled=[], disabled=[], meta=[])
    resp = _client().post("/api/plugins/demo/update")
    assert resp.status_code == 400
    assert "source_url" in resp.json()["detail"]


def test_update_route_400_on_install_error(monkeypatch):
    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR, "present": True},
    ])
    _wire_state(monkeypatch, enabled=["demo"], disabled=[], meta=[])

    def _install(*a, **k):
        raise installer.InstallError("clone failed: network down")

    monkeypatch.setattr(installer, "install", _install)
    resp = _client().post("/api/plugins/demo/update")
    assert resp.status_code == 400
    assert "network down" in resp.json()["detail"]


def test_update_route_purges_stale_module_subtree(monkeypatch):
    """Updating an ENABLED plugin drops its whole sys.modules subtree before the
    reload, so the loader re-execs every file (a multi-file plugin's `from .tools
    import …` would otherwise resolve the cached, stale submodule). Unrelated and
    prefix-sibling modules are left alone (the match is .-boundary-aware)."""
    from graph.plugins.loader import _plugin_module_name

    _lock(monkeypatch, [
        {"id": "demo", "source_url": "https://x/y.git", "requested_ref": "main",
         "resolved_sha": _CUR, "present": True},
    ])
    _wire_state(monkeypatch, enabled=["demo"], disabled=[], meta=[{"id": "demo", "views": []}])
    monkeypatch.setattr(
        installer, "install",
        lambda *a, **k: {"id": "demo", "version": "0.2.0", "resolved_sha": _LATEST},
    )

    prefix = _plugin_module_name("demo")          # protoagent_plugin_demo
    other = _plugin_module_name("other")
    sibling = f"{prefix}2"                          # shares the string prefix, NOT a submodule
    seeded = [prefix, f"{prefix}.tools", f"{prefix}.sub.deep", sibling, other]
    for name in seeded:
        sys.modules[name] = types.ModuleType(name)
    try:
        body = _client().post("/api/plugins/demo/update").json()
        assert body["reloaded"] is True
        # demo's entire subtree is gone → the reload re-imports it from disk
        assert prefix not in sys.modules
        assert f"{prefix}.tools" not in sys.modules
        assert f"{prefix}.sub.deep" not in sys.modules
        # the .-boundary check spares a sibling with a shared string prefix …
        assert sibling in sys.modules
        # … and an unrelated plugin entirely
        assert other in sys.modules
    finally:
        for name in seeded:
            sys.modules.pop(name, None)
