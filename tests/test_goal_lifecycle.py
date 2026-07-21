"""Goal timeline (per-iteration history) + lifecycle (rearm) — ADR 0079."""

from __future__ import annotations

import pytest

from graph.goals import verifiers as verifiers_mod
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult


def _ctrl(tmp_path):
    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_history_records_each_iteration_and_terminal(tmp_path, monkeypatch):
    calls = {"i": 0}

    async def _fuzzy(spec, ctx):
        calls["i"] += 1
        return VerifyResult(calls["i"] >= 3, f"check {calls['i']}", "")  # met on the 3rd

    monkeypatch.setitem(verifiers_mod.VERIFIERS, "llm", _fuzzy)
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "llm"}, max_iterations=10)
    c.record_plan("s", "p1")
    await c.evaluate("s", last_text="")  # continue
    c.record_plan("s", "p2")
    await c.evaluate("s", last_text="")  # continue (plan advanced → no stall)
    d3 = await c.evaluate("s", last_text="")  # met → achieved
    assert d3.state.status == "achieved"

    g = c._store.get("s")
    assert [e["status"] for e in g.history] == ["continue", "continue", "achieved"]
    assert all({"iteration", "at", "status", "reason"} <= set(e) for e in g.history)


@pytest.mark.asyncio
async def test_rearm_reactivates_terminal_and_resets(tmp_path, monkeypatch):
    async def _never(spec, ctx):
        return VerifyResult(False, "nope", "e")

    monkeypatch.setitem(verifiers_mod.VERIFIERS, "llm", _never)
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "llm"}, max_iterations=1)
    await c.evaluate("s", last_text="")  # iteration 1 >= max 1 → exhausted
    assert c._store.get("s").status == "exhausted"

    ok, _msg, resumed, state = c.rearm("s", add_iterations=3)
    assert ok and resumed
    assert state.status == "active" and state.finished_at is None
    assert state.iteration == 0 and state.no_progress_streak == 0
    assert state.max_iterations == 4  # 1 + 3
    assert state.history  # the timeline survives a re-arm


def test_rearm_active_extends_budget(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "command", "command": "x"}, max_iterations=5)
    ok, _msg, resumed, state = c.rearm("s", add_iterations=4)
    assert ok and not resumed and state.max_iterations == 9 and state.status == "active"


def test_rearm_active_without_budget_is_a_noop(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "command", "command": "x"})
    ok, msg, resumed, _state = c.rearm("s", add_iterations=0)
    assert not ok and not resumed and "already active" in msg


def test_rearm_missing_goal(tmp_path):
    ok, _msg, _resumed, state = _ctrl(tmp_path).rearm("nope")
    assert not ok and state is None
