"""Safe programmatic goal-set — plugin-verifier only (ADR 0028 D3, PR2)."""

from __future__ import annotations

import pytest

from graph.goals.controller import GoalController
from graph.goals.store import GoalStore


def _ctrl(tmp_path):
    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


def test_accepts_a_plugin_verifier(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg = c.set_goal_safe(
        "s1",
        "reach 1M credits",
        {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1_000_000}},
    )
    assert ok is True
    assert c.active_goal("s1") is not None


@pytest.mark.parametrize("vtype", ["command", "test", "ci", "data", "llm"])
def test_rejects_every_non_plugin_verifier(tmp_path, vtype):
    c = _ctrl(tmp_path)
    ok, msg = c.set_goal_safe("s1", "do x", {"type": vtype, "command": "rm -rf /", "expr": "1"})
    assert ok is False and "operator-only" in msg
    assert c.active_goal("s1") is None  # nothing was set


def test_requires_condition_and_check(tmp_path):
    c = _ctrl(tmp_path)
    ok, _ = c.set_goal_safe("s1", "", {"type": "plugin", "check": "x:y"})
    assert ok is False
    ok, _ = c.set_goal_safe("s1", "cond", {"type": "plugin"})  # no check
    assert ok is False
    assert c.active_goal("s1") is None


@pytest.mark.asyncio
async def test_rest_handler_gates_non_plugin(tmp_path, monkeypatch):
    from operator_api import console_handlers
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "goal_controller", _ctrl(tmp_path))

    good = await console_handlers._operator_goals_set(
        {"session_id": "s", "condition": "c", "verifier": {"type": "plugin", "check": "x:y"}}
    )
    assert good["ok"] is True

    bad = await console_handlers._operator_goals_set(
        {"session_id": "s", "condition": "c", "verifier": {"type": "command", "command": "x"}}
    )
    assert bad["ok"] is False and "error" in bad

    nosession = await console_handlers._operator_goals_set(
        {"condition": "c", "verifier": {"type": "plugin", "check": "x:y"}}
    )
    assert nosession["ok"] is False
