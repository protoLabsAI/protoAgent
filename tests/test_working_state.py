"""<working_state> injection — the "Observe" step of the autonomous operating model (ADR 0079).

KnowledgeMiddleware injects the agent's OWN live commitments (active goal + plan, open tasks,
active watches, pending schedules) every turn, so the agent observes its durable state instead
of polling for it. These tests exercise the assembly directly with fake STATE surfaces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import runtime.state as rs
from graph.middleware.knowledge import KnowledgeMiddleware


def _mw() -> KnowledgeMiddleware:
    return KnowledgeMiddleware(knowledge_store=None)


class _GoalCtrl:
    def __init__(self, goal, plan=""):
        self._goal = goal
        self._store = SimpleNamespace(read_plan=lambda sid: plan)

    def active_goal(self, session_id):
        return self._goal


class _Tasks:
    def __init__(self, items):
        self._items = items

    def list(self, *, include_closed=False):
        return self._items


class _Watches:
    def __init__(self, watches):
        self._watches = watches

    def list_watches(self):
        return self._watches


class _Sched:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return self._jobs


@pytest.fixture
def clear_state(monkeypatch):
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    yield


def test_empty_when_nothing_active(clear_state):
    assert _mw()._working_state_block({"session_id": "s"}) == ""


def test_full_block_assembles_all_sections(clear_state, monkeypatch):
    goal = SimpleNamespace(status="active", iteration=2, max_iterations=8, condition="ship the redesign")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan="- [x] scope\n- [ ] build"), raising=False)
    monkeypatch.setattr(
        rs.STATE, "tasks_store",
        _Tasks([{"status": "open", "id": "task-1", "priority": 1, "issue_type": "task", "title": "wire the hero"}]),
        raising=False,
    )
    watch = SimpleNamespace(status="active", status_line=lambda: "watch [active] (w1) via plugin: 'ci green'")
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches([watch]), raising=False)
    job = SimpleNamespace(id="job-9", next_fire="2026-07-09T09:00:00", prompt="follow up on the build")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)

    block = _mw()._working_state_block({"session_id": "s"})

    assert block.startswith("<working_state>") and block.endswith("</working_state>")
    assert "GOAL [active] (iteration 2/8): ship the redesign" in block
    assert "- [ ] build" in block  # plan (orient)
    assert "task-1" in block and "wire the hero" in block
    assert "watch [active]" in block
    assert "job-9" in block and "follow up on the build" in block


def test_goal_without_plan_nudges_to_record_one(clear_state, monkeypatch):
    goal = SimpleNamespace(status="active", iteration=0, max_iterations=5, condition="do the thing")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan=""), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "no plan recorded yet" in block
    assert "update_goal_plan" in block


def test_plan_is_capped(clear_state, monkeypatch):
    from graph.middleware.knowledge import _WS_PLAN_CAP

    goal = SimpleNamespace(status="active", iteration=1, max_iterations=8, condition="c")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan="x" * (_WS_PLAN_CAP + 500)), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "[truncated]" in block
    assert len(block) < _WS_PLAN_CAP + 800  # bounded, not the full 2000 chars


def test_only_active_watches_shown(clear_state, monkeypatch):
    active = SimpleNamespace(status="active", status_line=lambda: "watch [active] (w1)")
    met = SimpleNamespace(status="met", status_line=lambda: "watch [met] (w2)")
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches([active, met]), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "w1" in block and "w2" not in block


def test_read_failure_is_skipped_not_raised(clear_state, monkeypatch):
    class _Boom:
        def active_goal(self, sid):
            raise RuntimeError("db gone")

    monkeypatch.setattr(rs.STATE, "goal_controller", _Boom(), raising=False)
    # A task still renders — the goal section is skipped, nothing propagates.
    monkeypatch.setattr(
        rs.STATE, "tasks_store",
        _Tasks([{"status": "open", "id": "task-2", "priority": 2, "issue_type": "task", "title": "t"}]),
        raising=False,
    )
    block = _mw()._working_state_block({"session_id": "s"})
    assert "task-2" in block and "GOAL" not in block
