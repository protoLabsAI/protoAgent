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
        assert d is None  # no continuation — the agent has nothing to do
        g = c.active_goal("s")
        assert g and g.active and g.last_evidence == "42" and g.last_checked is not None
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_monitor_never_exhausts(tmp_path):
    async def _no(spec, ctx):
        return VerifyResult(False, "x", "e")  # never met, identical evidence

    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        # tiny budgets that WOULD trip a drive goal — monitor must ignore them
        c.set_goal_safe(
            "s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor", max_iterations=2, no_progress_limit=1
        )
        for _ in range(10):
            assert await c.evaluate("s", last_text="") is None
        assert c.active_goal("s").active  # still active — no exhausted/unachievable
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
        assert n == 1  # only the monitor goal was ticked + finished
        assert c.active_goal("mon") is None  # achieved → no longer active
        assert c.active_goal("drv") is not None  # drive goal untouched by the tick
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_parse_control_and_status_line_monitor(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition":"x","mode":"monitor","verifier":{"type":"plugin","check":"a:b"}}', "s")
    g = c.active_goal("s")
    assert g.mode == "monitor" and "(monitor)" in g.status_line()


# --- ADR 0030 D5: deadline → expired + stall_after → on_stalled ---------------


@pytest.mark.asyncio
async def test_monitor_past_deadline_expires_and_fires_on_failed(tmp_path):
    from time import time

    failed: list[str] = []
    set_goal_hooks(
        [{"plugin_id": "p", "on_achieved": None, "on_failed": lambda st: failed.append(st.status), "on_stalled": None}]
    )

    async def _no(spec, ctx):
        return VerifyResult(False, "waiting", "42")

    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        # deadline already in the past → the not-met monitor goal must finish `expired`.
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor", deadline=time() - 1)
        d = await c.evaluate("s", last_text="")
        assert d is not None and d.action == "done"
        assert d.state.status == "expired"
        assert failed == ["expired"]  # a non-achieved terminal → on_failed fired
        assert c.active_goal("s") is None  # terminal → no longer active
    finally:
        set_plugin_verifiers({})
        set_goal_hooks([])


@pytest.mark.asyncio
async def test_monitor_future_deadline_stays_active(tmp_path):
    from time import time

    async def _no(spec, ctx):
        return VerifyResult(False, "waiting", "42")

    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor", deadline=time() + 3600)
        assert await c.evaluate("s", last_text="") is None  # not met, deadline not reached
        assert c.active_goal("s").active  # stays active
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_monitor_stall_after_fires_on_stalled_without_ending(tmp_path):
    stalled: list[int] = []
    set_goal_hooks(
        [
            {
                "plugin_id": "p",
                "on_achieved": None,
                "on_failed": None,
                "on_stalled": lambda st: stalled.append(st.stall_streak),
            }
        ]
    )

    evidence = {"v": "e1"}

    async def _no(spec, ctx):
        return VerifyResult(False, "waiting", evidence["v"])  # identical evidence until we change it

    set_plugin_verifiers({"p:c": _no})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "grow", {"type": "plugin", "check": "p:c"}, mode="monitor", stall_after=2)

        await c.evaluate("s", last_text="")  # check 1: baseline (streak 0)
        assert stalled == []
        await c.evaluate("s", last_text="")  # check 2: 1st unchanged (streak 1)
        assert stalled == []
        await c.evaluate("s", last_text="")  # check 3: 2nd unchanged (streak 2 == stall_after) → fires
        assert stalled == [2]
        assert c.active_goal("s") and c.active_goal("s").active  # stall does NOT end the goal
        await c.evaluate("s", last_text="")  # check 4: still unchanged → NOT re-fired (once per episode)
        assert stalled == [2]

        # Evidence changes → re-arm; two more unchanged checks fire on_stalled again.
        evidence["v"] = "e2"
        await c.evaluate("s", last_text="")  # changed → streak reset + notified cleared
        await c.evaluate("s", last_text="")  # 1st unchanged (streak 1)
        assert stalled == [2]
        await c.evaluate("s", last_text="")  # 2nd unchanged (streak 2) → fires again
        assert stalled == [2, 2]
        assert c.active_goal("s").active
    finally:
        set_plugin_verifiers({})
        set_goal_hooks([])


@pytest.mark.asyncio
async def test_parse_control_deadline_and_stall_after(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control(
        '/goal {"condition":"x","mode":"monitor","stall_after":3,'
        '"deadline":"2999-01-01T00:00:00","verifier":{"type":"plugin","check":"a:b"}}',
        "s",
    )
    g = c.active_goal("s")
    assert g.stall_after == 3
    assert g.deadline is not None and g.deadline > 0  # ISO-8601 parsed to epoch seconds
