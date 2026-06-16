"""controller.evaluate_now — prompt event-driven check (ADR 0030 D2.2)."""

from __future__ import annotations

import pytest

from graph.goals.controller import GoalController
from graph.goals.hooks import set_goal_hooks
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult
from graph.goals.verifiers import set_plugin_verifiers


def _ctrl(tmp_path):
    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_evaluate_now_no_active_goal(tmp_path):
    assert await _ctrl(tmp_path).evaluate_now("nope") is None


@pytest.mark.asyncio
async def test_evaluate_now_met_finishes_and_fires_hook(tmp_path):
    fired = []
    set_goal_hooks([{"plugin_id": "p", "on_achieved": lambda st: fired.append(st.status), "on_failed": None}])

    async def _yes(spec, ctx):
        return VerifyResult(True, "done", "1M")

    set_plugin_verifiers({"p:c": _yes})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor")
        d = await c.evaluate_now("s")
        assert d.action == "done" and d.state.status == "achieved" and fired == ["achieved"]
    finally:
        set_plugin_verifiers({})
        set_goal_hooks([])


@pytest.mark.asyncio
async def test_evaluate_now_not_met_records_without_drive_bookkeeping(tmp_path):
    async def _no(spec, ctx):
        return VerifyResult(False, "waiting", "5")

    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        # a DRIVE goal — evaluate_now must NOT advance its iteration/exhaust it
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"})
        d = await c.evaluate_now("s")
        assert d is None
        g = c.active_goal("s")
        assert g.active and g.iteration == 0 and g.last_evidence == "5" and g.last_checked is not None
    finally:
        set_plugin_verifiers({})
