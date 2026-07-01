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
async def test_rest_handler_is_the_operator_channel(tmp_path, monkeypatch):
    # ADR 0066: POST /api/goals is the OPERATOR channel — gated to operator-tier by the
    # /api auth path ceiling, not by the handler — so it accepts ANY verifier type
    # (command/test/ci/data included), unlike the plugin-only agent/SDK path (set_goal_safe).
    from operator_api import console_handlers
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "goal_controller", _ctrl(tmp_path))

    plugin_goal = await console_handlers._operator_goals_set(
        {"session_id": "s", "condition": "c", "verifier": {"type": "plugin", "check": "x:y"}}
    )
    assert plugin_goal["ok"] is True

    # A command verifier now SUCCEEDS via the operator channel (was rejected pre-0066) —
    # the security control is the /api operator-tier ceiling, not this handler.
    command_goal = await console_handlers._operator_goals_set(
        {"session_id": "s2", "condition": "c", "verifier": {"type": "command", "command": "true"}}
    )
    assert command_goal["ok"] is True

    nosession = await console_handlers._operator_goals_set(
        {"condition": "c", "verifier": {"type": "plugin", "check": "x:y"}}
    )
    assert nosession["ok"] is False
