"""Tests for the goal-continuation ambient marker."""

from __future__ import annotations

from graph.goals.continuation import goal_continuation_turn, in_goal_continuation


def test_default_is_false():
    assert in_goal_continuation() is False


def test_marker_is_set_inside_and_cleared_after():
    assert in_goal_continuation() is False
    with goal_continuation_turn():
        assert in_goal_continuation() is True
    assert in_goal_continuation() is False


def test_marker_clears_even_on_exception():
    try:
        with goal_continuation_turn():
            assert in_goal_continuation() is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert in_goal_continuation() is False
