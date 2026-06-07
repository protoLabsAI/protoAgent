"""The LLM-facing set_goal tool — agent owns a plugin-verified goal (ADR 0028)."""

from __future__ import annotations

import tracing
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from runtime.state import STATE
from tools.lg_tools import _build_set_goal_tool, get_all_tools


def test_get_all_tools_gates_set_goal_on_goal_enabled():
    on = {t.name for t in get_all_tools(goal_enabled=True)}
    off = {t.name for t in get_all_tools(goal_enabled=False)}
    assert "set_goal" in on and "set_goal" not in off


def test_set_goal_reports_when_goal_mode_off(monkeypatch):
    monkeypatch.setattr(STATE, "goal_controller", None)
    out = _build_set_goal_tool().invoke({"condition": "c", "check": "x:y"})
    assert "not enabled" in out


def test_set_goal_needs_an_active_session(monkeypatch, tmp_path):
    monkeypatch.setattr(STATE, "goal_controller", GoalController(None, GoalStore(base_dir=str(tmp_path))))
    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    out = _build_set_goal_tool().invoke({"condition": "c", "check": "x:y"})
    assert "No active session" in out


def test_set_goal_sets_a_plugin_verified_goal(monkeypatch, tmp_path):
    ctrl = GoalController(None, GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(tracing, "current_session_id", lambda: "s1")
    out = _build_set_goal_tool().invoke(
        {"condition": "reach 1M", "check": "spacetraders:credits", "check_args": {"min": 1_000_000}}
    )
    assert "Goal set" in out
    g = ctrl.active_goal("s1")
    assert g is not None
    assert g.verifier == {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1_000_000}}
