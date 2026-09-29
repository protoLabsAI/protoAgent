"""Seam guard for the ``server/plugin_wiring.py`` extraction (#3821).

Plugin router mounting + HTTP wrapping, the plugin registries and host, and the
plugin-surface reconcile moved out of ``server/agent_init.py`` and are re-exported
there, so ``agent_init.<name>`` still RESOLVES — but a monkeypatch on ``agent_init``
no longer INTERCEPTS a collaborator the moved code calls by bare name: it resolves in
``plugin_wiring``'s globals. Such a patch is dead, and the test around it can pass
silently against the real thing. This scans the suite so a stale target fails loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import server.agent_init as agent_init
import server.plugin_wiring as plugin_wiring

# Names whose only live binding (as a callee) is in plugin_wiring. Deliberately absent:
# ``_mount_plugin_routers`` / ``_reload_plugin_surfaces`` / ``_apply_plugin_registries``
# (agent_init's boot/reload path calls them by bare name, so a patch on agent_init DOES
# intercept those callers), and ``_run_on_server_loop`` / ``_apply_settings_changes``
# (they stay on agent_init; plugin_wiring calls them through the module at call time).
_MOVED_COLLABORATORS = {
    "_install_error_envelope",
    "_exclude_unschemable_routes",
    "_plugin_api_routes",
    "_wrap_plugin_endpoint",
    "_plugin_agent_invoke",
    "_populate_plugin_host",
    "_surface_key",
    "_plan_surface_reconcile",
    "_surface_reconcile_lock",
    "_SURFACE_RESTART_GRACE_S",
    "_SURFACE_CANCEL_GRACE_S",
    "_SURFACE_RECONCILE_LOCKS",
    "chat",  # _plugin_agent_invoke's collaborator; agent_init no longer imports it at all
}

_RE_EXPORTED = (_MOVED_COLLABORATORS - {"chat"}) | {
    "_mount_plugin_routers",
    "_reload_plugin_surfaces",
    "_apply_plugin_registries",
}

_TESTS = Path(__file__).resolve().parent


def _agent_init_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "server.agent_init"}
        elif isinstance(node, ast.ImportFrom) and node.module == "server":
            aliases |= {a.asname or a.name for a in node.names if a.name == "agent_init"}
    return aliases


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _agent_init_aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"setattr", "object", "patch"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target.startswith("server.agent_init.") and target.rsplit(".", 1)[1] in _MOVED_COLLABORATORS:
                    stale.append(f"{path.name}:{node.lineno} {target}")
            elif (
                isinstance(first, ast.Name)
                and first.id in aliases
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MOVED_COLLABORATORS
            ):
                stale.append(f"{path.name}:{node.lineno} {first.id}.{node.args[1].value}")
    assert not stale, "patch these on server.plugin_wiring, not agent_init (#3821): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """agent_init / server re-export the moved names by identity (no stale copies); the
    reconcile-lock registry has ONE home."""
    import server

    for name in _RE_EXPORTED:
        assert getattr(agent_init, name) is getattr(plugin_wiring, name), name
    for name in ("_mount_plugin_routers", "_plugin_agent_invoke", "_populate_plugin_host", "_reload_plugin_surfaces"):
        assert getattr(server, name) is getattr(plugin_wiring, name), name
    assert not hasattr(agent_init, "chat")


def test_agent_init_seams_are_called_through_the_module(monkeypatch):
    """The two agent_init seams plugin_wiring uses are looked up at call time, so a patch
    on agent_init intercepts them from the moved code."""
    from graph.plugins.host import HOST
    from runtime.state import STATE

    applied: list = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: applied.append(kw) or (True, []))
    for attr in ("invoke", "publish", "subscribe", "on", "config", "apply_settings"):
        monkeypatch.setattr(HOST, attr, getattr(HOST, attr, None), raising=False)
    plugin_wiring._populate_plugin_host()
    HOST.apply_settings({"x": 1})
    assert applied == [{"config": {"x": 1}}]

    scheduled: list = []
    monkeypatch.setattr(agent_init, "_run_on_server_loop", lambda make, what: scheduled.append(what))
    monkeypatch.setattr(STATE, "plugin_surfaces_started", True, raising=False)
    plugin_wiring._reload_plugin_surfaces(object())
    assert scheduled == ["surface reconcile"]
