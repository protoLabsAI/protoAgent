"""Seam guard for the ``server/settings_apply.py`` extraction (#3848).

The settings apply / reset path, its snapshot-rollback helpers, the autostart sync, the
``CONFIG_WRITE_LOCK`` decorator and the console Settings + setup-wizard callbacks moved
out of ``server/agent_init.py`` and are re-exported there, so ``agent_init.<name>`` still
RESOLVES — but a monkeypatch on ``agent_init`` no longer INTERCEPTS a collaborator the
moved code calls by bare name: it resolves in ``settings_apply``'s globals. Such a patch
is dead, and the test around it can pass silently against the real thing. This scans the
suite so a stale target fails loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import server.agent_init as agent_init
import server.settings_apply as settings_apply
from tests._seam_scan import stale_patches

# Names whose only live binding (as a callee / read) is in settings_apply. Deliberately
# absent: ``_apply_settings_changes`` — its published address stays
# ``server.agent_init``: operator_api (config_routes included, #3856) and the devkit plugin
# import it from there at call time, and maintenance_loops / plugin_wiring / the plugin
# host / ``save_all`` call it through agent_init, so a patch on agent_init DOES intercept
# every caller. And
# ``_reload_langgraph_agent``, which never moved (settings_apply calls it through
# agent_init at call time).
_MOVED_COLLABORATORS = {
    "_serialized_config_write",
    "_sync_autostart_with_config",
    "_filter_nested_to_host_keys",
    "_prune_shadowing_agent_keys",
    "_config_files_to_snapshot",
    "_snapshot_config_files",
    "_restore_config_files",
    "_ROLLBACK_NOTE",
    "_WRITE_ANNOUNCEMENTS",
    "_drop_undone_write_messages",
    "_reset_settings_keys",
    "_build_settings_callbacks",
    "_event_bus",  # the plugin.changed publish; agent_init no longer imports it at all
}

_RE_EXPORTED = (_MOVED_COLLABORATORS - {"_event_bus"}) | {"_apply_settings_changes", "_CONFIG_WRITE_LOCK"}
# ``server/__init__`` re-exports these as names only — nothing calls them through the
# package, so a patch on ``server.<name>`` intercepts nothing (``server._event_bus`` is the
# package's OWN bus, not a copy).
_PACKAGE_COPIES = (_MOVED_COLLABORATORS - {"_event_bus"}) | {"_apply_settings_changes"}


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale = stale_patches("server.agent_init", _MOVED_COLLABORATORS)
    assert not stale, "patch these on server.settings_apply, not agent_init (#3848): " + ", ".join(stale)


def test_no_test_patches_the_package_level_copies():
    """``server.<name>`` resolves (re-export) but no caller reads it there (#3856)."""
    stale = stale_patches("server", _PACKAGE_COPIES)
    assert not stale, "patch these on server.agent_init / server.settings_apply, not server: " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """agent_init / server re-export the moved names by identity (no stale copies); the
    config write lock has ONE home (graph.config_io)."""
    import graph.config_io as cio
    import server

    for name in _RE_EXPORTED:
        assert getattr(agent_init, name) is getattr(settings_apply, name), name
    for name in ("_apply_settings_changes", "_sync_autostart_with_config"):
        assert getattr(server, name) is getattr(settings_apply, name), name
    assert settings_apply._CONFIG_WRITE_LOCK is cio.CONFIG_WRITE_LOCK
    assert not hasattr(agent_init, "_event_bus")
    # The seams settings_apply reaches through agent_init must NOT be bound here — a
    # module-level copy would silently bypass a patch on agent_init.
    assert not hasattr(settings_apply, "_reload_langgraph_agent")


def _isolate_config(monkeypatch, tmp_path: Path) -> None:
    import graph.config_io as cio
    import infra.paths as paths

    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    monkeypatch.setattr(paths, "host_config_path", lambda: tmp_path / "host-config.yaml", raising=False)
    monkeypatch.setattr(settings_apply, "_event_bus", type("_Bus", (), {"publish": lambda *a, **k: None})())


def test_reload_is_called_through_agent_init(monkeypatch, tmp_path):
    """``_reload_langgraph_agent`` stays in agent_init; every moved caller looks it up there
    at call time, so the suite's many ``agent_init._reload_langgraph_agent`` patches still
    intercept the apply / reset / finish-setup paths."""
    import graph.config_io as cio

    _isolate_config(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(agent_init, "_reload_langgraph_agent", lambda: calls.append("reload") or (True, "fake"))

    assert settings_apply._apply_settings_changes() == (True, ["fake"])
    assert settings_apply._reset_settings_keys([]) == (True, ["fake"])

    monkeypatch.setattr(cio, "mark_setup_complete", lambda: None)
    monkeypatch.setattr(cio, "reset_setup", lambda: None)
    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"](None, None)
    assert ok and msg.endswith("fake")
    assert calls == ["reload", "reload", "reload"]


def test_save_all_calls_apply_through_agent_init(monkeypatch):
    """``save_all`` reaches ``_apply_settings_changes`` through agent_init — the one patch
    point every caller shares."""
    seen: list = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: seen.append(kw) or (True, ["a", "b"]))
    assert settings_apply._build_settings_callbacks()["save_all"]({"x": 1}, None) == (True, "a • b")
    assert seen == [{"config": {"x": 1}, "soul": None}]


def test_callers_resolve_apply_through_agent_init():
    """maintenance_loops / plugin_wiring / config_routes / the devkit plugin look the apply
    path up on agent_init at call time, never via a module-level ``from … import`` of it;
    and ``server/__init__`` never reads its re-exported copy."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("server/maintenance_loops.py", "server/plugin_wiring.py", "server/settings_apply.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"server.agent_init", "server.settings_apply"}:
                raise AssertionError(f"{rel}: module-level import from {node.module}")
    for rel in ("operator_api/config_routes.py", "operator_api/mcp_routes.py", "operator_api/plugin_routes.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"server.agent_init", "server.settings_apply"}:
                names = {a.name for a in node.names}
                assert "_apply_settings_changes" not in names, f"{rel}: module-level import of the apply path"
    pkg = ast.parse((root / "server/__init__.py").read_text(encoding="utf-8"))
    reads = [
        n.lineno
        for n in ast.walk(pkg)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == "_apply_settings_changes"
    ]
    assert not reads, f"server/__init__ calls its re-exported copy (a patch on agent_init misses it): {reads}"


def test_agent_init_patch_reaches_the_config_routes(monkeypatch):
    """Behavioral proof (#3856): a patch on ``agent_init._apply_settings_changes`` is what
    ``POST /api/config`` and the SOUL-history restore run."""
    import sys
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.config_routes import register_config_routes

    seen: list[dict] = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: seen.append(kw) or (True, ["patched"]))
    cio = types.ModuleType("graph.config_io")
    cio.read_soul_version = lambda vid: "archived persona"
    cio.read_soul = lambda: "current persona"
    app = FastAPI()
    register_config_routes(app)
    client = TestClient(app)

    assert client.post("/api/config", json={"config": {"a": 1}}).json() == {"ok": True, "messages": ["patched"]}
    monkeypatch.setitem(sys.modules, "graph.config_io", cio)
    assert client.post("/api/config/soul/history/v1/restore").json()["messages"] == ["patched"]
    assert seen == [{"config": {"a": 1}, "soul": None}, {"soul": "archived persona"}]
