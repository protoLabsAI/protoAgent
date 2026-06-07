"""Monitor goal disposition + cadence tick (ADR 0030 D1/D2.1/D3)."""

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
async def test_monitor_not_met_returns_none_and_records(tmp_path):
    async def _no(spec, ctx):
        return VerifyResult(False, "waiting", "42")
    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor")
        d = await c.evaluate("s", last_text="")
        assert d is None                       # no continuation — the agent has nothing to do
        g = c.active_goal("s")
        assert g and g.active and g.last_evidence == "42" and g.last_checked is not None
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_monitor_never_exhausts(tmp_path):
    async def _no(spec, ctx):
        return VerifyResult(False, "x", "e")   # never met, identical evidence
    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        # tiny budgets that WOULD trip a drive goal — monitor must ignore them
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"},
                        mode="monitor", max_iterations=2, no_progress_limit=1)
        for _ in range(10):
            assert await c.evaluate("s", last_text="") is None
        assert c.active_goal("s").active       # still active — no exhausted/unachievable
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_monitor_achieved_fires_hook(tmp_path):
    fired = []
    set_goal_hooks([{"plugin_id": "p", "on_achieved": lambda st: fired.append(st.status), "on_failed": None}])

    async def _yes(spec, ctx):
        return VerifyResult(True, "done", "1M")
    set_plugin_verifiers({"p:c": _yes})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor")
        d = await c.evaluate("s", last_text="")
        assert d.action == "done" and d.state.status == "achieved" and fired == ["achieved"]
    finally:
        set_plugin_verifiers({})
        set_goal_hooks([])


@pytest.mark.asyncio
async def test_tick_evaluates_monitor_goals_only(tmp_path):
    async def _yes(spec, ctx):
        return VerifyResult(True, "ok", "")
    set_plugin_verifiers({"p:c": _yes})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("mon", "m", {"type": "plugin", "check": "p:c"}, mode="monitor")
        await c.parse_control('/goal {"condition":"d","verifier":{"type":"plugin","check":"p:c"}}', "drv")
        n = await c.tick_monitor_goals()
        assert n == 1                          # only the monitor goal was ticked + finished
        assert c.active_goal("mon") is None     # achieved → no longer active
        assert c.active_goal("drv") is not None  # drive goal untouched by the tick
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_parse_control_and_status_line_monitor(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition":"x","mode":"monitor","verifier":{"type":"plugin","check":"a:b"}}', "s")
    g = c.active_goal("s")
    assert g.mode == "monitor" and "(monitor)" in g.status_line()
