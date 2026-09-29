"""Seam guard for the ``server/plugin_wiring.py`` extraction (#3821).

Plugin router mounting + HTTP wrapping, the plugin registries and host, and the
plugin-surface reconcile moved out of ``server/agent_init.py`` and are re-exported
there, so ``agent_init.<name>`` still RESOLVES — but a monkeypatch on ``agent_init``
no longer INTERCEPTS a collaborator the moved code calls by bare name: it resolves in
``plugin_wiring``'s globals. Such a patch is dead, and the test around it can pass
silently against the real thing. This scans the suite so a stale target fails loudly.
"""

from __future__ import annotations


import server.agent_init as agent_init
import server.plugin_wiring as plugin_wiring
from tests._seam_scan import stale_patches

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


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale = stale_patches("server.agent_init", _MOVED_COLLABORATORS)
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
