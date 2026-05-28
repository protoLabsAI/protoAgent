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
"""

from __future__ import annotations

import contextlib
import contextvars

_goal_turn_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_protoagent_goal_turn", default=False,
)


def in_goal_turn() -> bool:
    """True while executing a goal-driven graph turn."""
    return _goal_turn_ctx.get()


@contextlib.contextmanager
def goal_turn(active: bool = True):
    """Mark the enclosed graph invocation as a goal-driven turn.

    ``active=False`` makes it a no-op so callers can gate inline (e.g. the
    initial turn only suppresses when a goal is already active for the session).
    """
    if not active:
        yield
        return
    token = _goal_turn_ctx.set(True)
    try:
        yield
    finally:
        # reset can raise if the generator is torn down in a different context
        # (mirrors the trace_session guard); the contextvar resets on context
        # exit regardless, so swallowing is safe.
        try:
            _goal_turn_ctx.reset(token)
        except ValueError:
            pass
