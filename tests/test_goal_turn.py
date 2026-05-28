"""Tests for the goal-turn ambient marker."""

from __future__ import annotations

from graph.goals.goal_turn import goal_turn, in_goal_turn


def test_default_is_false():
    assert in_goal_turn() is False


def test_marker_is_set_inside_and_cleared_after():
    assert in_goal_turn() is False
    with goal_turn():
        assert in_goal_turn() is True
    assert in_goal_turn() is False


def test_marker_clears_even_on_exception():
    try:
        with goal_turn():
            assert in_goal_turn() is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert in_goal_turn() is False


def test_inactive_gate_is_noop():
    assert in_goal_turn() is False
    with goal_turn(active=False):
        # Gated off — the initial-turn path passes active=False when no goal
        # is set, so prior_sessions injection is NOT suppressed.
        assert in_goal_turn() is False
    assert in_goal_turn() is False


def test_active_gate_true_sets_marker():
    with goal_turn(active=True):
        assert in_goal_turn() is True
    assert in_goal_turn() is False
