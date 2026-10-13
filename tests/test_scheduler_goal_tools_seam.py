"""Seam guard for the ``tools/scheduler_tools.py`` + ``tools/goal_tools.py`` extraction (#3830).

The scheduler / task / watch builders and the goal builders moved out of
``tools/lg_tools.py`` and are re-exported there, so ``lg_tools.<name>`` still RESOLVES —
but a monkeypatch on ``lg_tools`` no longer INTERCEPTS a collaborator the moved code
calls by bare name: it resolves in the new module's globals. Such a patch is dead, and
the test around it can pass silently against the real thing. This scans the suite so a
stale target fails loudly.

Deliberately NOT stale: the ``_build_*`` builders themselves — ``get_all_tools`` stays in
``lg_tools`` and calls them by bare name, so a patch on ``lg_tools`` DOES intercept it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.goal_tools as goal_tools
import tools.lg_tools as lg_tools
import tools.scheduler_tools as scheduler_tools
from tests._seam_scan import stale_patches

# Collaborators whose only live callers moved: patch them on tools.scheduler_tools.
_MOVED_COLLABORATORS = {"_humanize_duration", "is_cron", "parse_ttl", "timedelta"}

_RE_EXPORTED = {
    "_build_scheduler_tools": scheduler_tools,
    "_build_task_tools": scheduler_tools,
    "_build_watch_tools": scheduler_tools,
    "_humanize_duration": scheduler_tools,
    "_build_set_goal_tool": goal_tools,
    "_build_list_verifiers_tool": goal_tools,
    "_build_goal_plan_tool": goal_tools,
    "_build_abandon_goal_tool": goal_tools,
}


def test_no_test_patches_a_moved_collaborator_on_lg_tools():
    stale = stale_patches("tools.lg_tools", _MOVED_COLLABORATORS)
    assert not stale, "patch these on tools.scheduler_tools, not lg_tools (#3830): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """lg_tools re-exports the moved names by identity (no stale copies)."""
    for name, home in _RE_EXPORTED.items():
        assert getattr(lg_tools, name) is getattr(home, name), name


def test_moved_modules_do_not_import_lg_tools():
    """The new modules sit BELOW lg_tools — importing it back would be a cycle and would
    capture lg_tools names by value at import time."""
    for mod in (scheduler_tools, goal_tools):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "lg_tools" not in "\n".join(
            line for line in src.splitlines() if line.lstrip().startswith(("import ", "from "))
        ), mod.__name__


@pytest.mark.asyncio
async def test_moved_collaborator_resolves_in_its_new_home_at_call_time(monkeypatch):
    """``wait`` looks ``_humanize_duration`` up in scheduler_tools' globals at call time,
    so a patch THERE intercepts it (and one on lg_tools would not — the guard above)."""

    class _Sched:
        def cancel_job(self, job_id):
            return False

        def add_job(self, *a, **k):
            return None

    monkeypatch.setattr(scheduler_tools, "_humanize_duration", lambda s: "PATCHED")
    wait = next(t for t in scheduler_tools._build_scheduler_tools(_Sched()) if t.name == "wait")
    out = await wait.ainvoke({"seconds": 5, "then": "go"})
    assert "PATCHED" in out


def test_get_all_tools_still_calls_the_builders_by_bare_name(monkeypatch):
    """The assembly point stayed in lg_tools, so a patch on lg_tools intercepts it."""
    sentinel = object()
    monkeypatch.setattr(lg_tools, "_build_scheduler_tools", lambda scheduler: [sentinel])
    monkeypatch.setattr(lg_tools, "_build_task_tools", lambda store: [sentinel])
    tools = lg_tools.get_all_tools(scheduler=object(), tasks_store=object())
    assert tools.count(sentinel) == 2
