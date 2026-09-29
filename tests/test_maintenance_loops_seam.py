"""Seam guard for the ``server/maintenance_loops.py`` extraction (#3807).

The maintenance loops moved out of ``server/agent_init.py`` and are re-exported
there, so ``agent_init.<name>`` still RESOLVES — but a monkeypatch on
``agent_init`` no longer INTERCEPTS: the moved code looks its collaborators up in
``maintenance_loops``' globals. Such a patch is dead, and the test around it can
pass silently against the real function (``_server_is_idle`` does exactly that in
a quiet test process). This scans the suite so a stale target fails loudly.
"""

from __future__ import annotations


import server.agent_init as agent_init
import server.maintenance_loops as maintenance_loops
from tests._seam_scan import stale_patches

# Names whose only live binding is in maintenance_loops. ``_audit_persona_tools`` is
# deliberately absent: agent_init's boot/reload path calls it by bare name, so a patch
# on agent_init DOES intercept those callers.
_MOVED_COLLABORATORS = {
    "_server_is_idle",
    "_run_soul_drift_pass",
    "_judge_soul_drift",
    "_maybe_run_soul_drift_pass",
    "_last_soul_drift_check",
    "_retire_thread",
    "_autoupdate_one_plugin",
    "_plugin_autoupdate_sweep",
    "_HARVEST_FAILURES",
    "_HARVEST_FAILURE_CAP",
    "_AUTOUPDATE_IDLE_QUIET_S",
    "A2A_REAPER_INTERVAL_S",
    "MEMORY_GUARD_INTERVAL_S",
}


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale = stale_patches("server.agent_init", _MOVED_COLLABORATORS)
    assert not stale, "patch these on server.maintenance_loops, not agent_init (#3807): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """agent_init / server re-export the moved names by identity (no stale copies);
    the drift gate's rebindable float is intentionally NOT re-exported."""
    import server

    for name in _MOVED_COLLABORATORS - {"_last_soul_drift_check"}:
        assert getattr(agent_init, name) is getattr(maintenance_loops, name), name
    assert agent_init._audit_persona_tools is maintenance_loops._audit_persona_tools
    assert server._retire_thread is maintenance_loops._retire_thread
    assert not hasattr(agent_init, "_last_soul_drift_check")
