"""Per-goal no_progress_limit (ADR 0030 D4)."""

from __future__ import annotations

import pytest

from graph.goals import verifiers as verifiers_mod
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult
from graph.goals.verifiers import set_plugin_verifiers


def _ctrl(tmp_path):
    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_parse_control_accepts_no_progress_limit(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "x", "no_progress_limit": 5}', "s")
    g = c.active_goal("s")
    assert g.no_progress_limit == 5


def test_set_goal_safe_accepts_no_progress_limit(tmp_path):
    c = _ctrl(tmp_path)
    ok, _ = c.set_goal_safe("s", "cond", {"type": "plugin", "check": "x:y"}, no_progress_limit=7)
    assert ok and c.active_goal("s").no_progress_limit == 7


def test_default_is_none(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_safe("s", "cond", {"type": "plugin", "check": "x:y"})
    assert c.active_goal("s").no_progress_limit is None  # → config fallback


@pytest.mark.asyncio
async def test_per_goal_limit_drives_unachievable(tmp_path):
    async def _never(spec, ctx):
        return VerifyResult(False, "x", "e")  # never met, identical evidence

    set_plugin_verifiers({"p:never": _never})
    try:
        c = _ctrl(tmp_path)
        c.set_goal_safe("s", "cond", {"type": "plugin", "check": "p:never"}, no_progress_limit=1)
        d1 = await c.evaluate("s", last_text="")
        assert d1.action == "continue"  # 1st: sets the evidence baseline
        d2 = await c.evaluate("s", last_text="")
        assert d2.action == "done" and d2.state.status == "unachievable"  # streak hit limit=1
    finally:
        set_plugin_verifiers({})


@pytest.mark.asyncio
async def test_fuzzy_goal_stalls_on_unchanged_plan(tmp_path, monkeypatch):
    # The real llm verifier returns a DIFFERENT free-text reason each call and empty evidence,
    # so the (reason, evidence) fingerprint never repeats — no_progress_limit could never fire.
    # A fuzzy goal fingerprints the PLAN artifact instead: an agent that never records a plan
    # (plan stays "") is spinning, so the streak still trips.
    n = {"i": 0}

    async def _fuzzy(spec, ctx):
        n["i"] += 1
        return VerifyResult(False, f"still not done ({n['i']})", "")

    monkeypatch.setitem(verifiers_mod.VERIFIERS, "llm", _fuzzy)
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "llm"}, no_progress_limit=2)
    assert (await c.evaluate("s", last_text="")).action == "continue"  # baseline (plan "")
    assert (await c.evaluate("s", last_text="")).action == "continue"  # streak 1
    d3 = await c.evaluate("s", last_text="")
    assert d3.action == "done" and d3.state.status == "unachievable"  # streak hit limit=2


@pytest.mark.asyncio
async def test_fuzzy_goal_keeps_driving_while_plan_advances(tmp_path, monkeypatch):
    # When the agent records NEW plan content each turn (real progress), the plan fingerprint
    # changes, the streak resets, and the goal keeps driving to the iteration cap — a fuzzy
    # goal is not force-stalled just because the llm verdict is unstable.
    async def _fuzzy(spec, ctx):
        return VerifyResult(False, "keep going", "")

    monkeypatch.setitem(verifiers_mod.VERIFIERS, "llm", _fuzzy)
    c = _ctrl(tmp_path)
    c.set_goal_operator("s", "cond", {"type": "llm"}, no_progress_limit=2)
    for i in range(4):
        c.record_plan("s", f"tried step {i}; next step {i + 1}")  # agent advances the plan
        d = await c.evaluate("s", last_text="")
        assert d.action == "continue"  # never stalls — the plan changed every turn
    assert c.active_goal("s").no_progress_streak == 0
