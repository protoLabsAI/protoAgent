"""Ambient marker for goal-driven graph turns.

When a session has an active goal, both the initial (user-triggered) turn and
the server's self-driven continuation turns must NOT receive cross-session
``<prior_sessions>`` injection — unrelated history biases the loop (e.g. an
earlier session's "this looks like prompt injection / I can't do this"
reasoning bleeds in and the model gives up on an unrelated, achievable goal,
observed in QA).

The server wraps every goal-driven graph invocation in ``goal_turn()`` and the
memory-injecting middleware checks ``in_goal_turn()`` to suppress
``<prior_sessions>``. Threaded via a contextvar (not graph state) because
``session_id`` proves undeclared state keys are dropped by LangGraph, whereas a
contextvar set in the invoking coroutine reaches the synchronous middleware
hooks running inside the same context — the same mechanism ``trace_session``
uses for ``session_id``.

The marker is also how a goal-driven pass reports back that the round governor
ended it at the per-turn round cap (#3957): the contextvar holds a mutable
:class:`GoalTurn`, the middleware calls :func:`record_round_cap` (LangGraph runs
nodes in a COPY of the invoking context — the copy holds the same object, so the
mutation is visible to the driver), and the driver hands the marker to
``server.goal_loop.GoalDrive``, which pauses the drive instead of re-driving a
turn that just ran away.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass


@dataclass
class GoalTurn:
    """One goal-driven pass (or group of passes). ``round_cap`` is non-zero once the
    round governor ended the turn at a cap: the cap value, the ``rounds`` run, and the
    config key that set it (``goal.max_rounds_per_turn`` or ``model.round_hard_cap``)."""

    round_cap: int = 0
    rounds: int = 0
    cap_key: str = ""
    # Mid-turn verifier probes (``graph.middleware.goal_checkpoint``), debounced per pass:
    # the round (model-call count) last probed, the monotonic time before which no further
    # probe runs, and how many ran.
    probe_round: int = -1
    probe_after: float = 0.0
    probes: int = 0
    # Set when a probe passed mid-turn: the goal is already recorded achieved, and this is
    # its terminal note ("✓ goal achieved: …") for the drive to report. ``closing`` is True
    # until the one tool-less closing model call has run.
    achieved_note: str = ""
    closing: bool = False

    @property
    def capped(self) -> bool:
        return self.round_cap > 0


_goal_turn_ctx: contextvars.ContextVar[GoalTurn | None] = contextvars.ContextVar(
    "_protoagent_goal_turn",
    default=None,
)


def in_goal_turn() -> bool:
    """True while executing a goal-driven graph turn."""
    return _goal_turn_ctx.get() is not None


def record_round_cap(rounds: int, cap: int, cap_key: str) -> None:
    """Mark the current goal-driven pass as ended by the round governor at ``cap``.
    A no-op outside a goal turn."""
    marker = _goal_turn_ctx.get()
    if marker is not None:
        marker.round_cap, marker.rounds, marker.cap_key = int(cap), int(rounds), cap_key


def current_goal_turn() -> GoalTurn | None:
    """The running goal-driven pass's marker, or ``None`` outside a goal turn."""
    return _goal_turn_ctx.get()


@contextlib.contextmanager
def goal_turn(active: bool = True):
    """Mark the enclosed graph invocation as a goal-driven turn.

    ``active=False`` makes it a no-op so callers can gate inline (e.g. the
    initial turn only suppresses when a goal is already active for the session).
    Yields the pass's :class:`GoalTurn` marker (a fresh, never-installed one when
    inactive, so a caller can read ``.capped`` either way).
    """
    marker = GoalTurn()
    if not active:
        yield marker
        return
    token = _goal_turn_ctx.set(marker)
    try:
        yield marker
    finally:
        # reset can raise if the generator is torn down in a different context
        # (mirrors the trace_session guard); the contextvar resets on context
        # exit regardless, so swallowing is safe.
        try:
            _goal_turn_ctx.reset(token)
        except ValueError:
            pass
