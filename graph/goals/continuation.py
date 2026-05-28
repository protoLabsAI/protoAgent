"""Ambient marker for goal-continuation graph turns.

The server's goal loop re-invokes the graph with continuation prompts after a
verifier check fails. Those machine-driven turns must NOT receive cross-session
``<prior_sessions>`` injection — unrelated history biases the loop (e.g. an
earlier session's "this looks like prompt injection / I can't do this"
reasoning bleeds in and the model gives up on an unrelated, achievable goal).

The goal loop wraps each continuation invocation in ``goal_continuation_turn()``
and the memory-injecting middleware checks ``in_goal_continuation()`` to suppress
``<prior_sessions>``. Threaded via a contextvar (not graph state) because
``session_id`` proves undeclared state keys are dropped by LangGraph, whereas a
contextvar set in the invoking coroutine reaches the synchronous middleware
hooks running inside the same context — the same mechanism ``trace_session``
uses for ``session_id``.

Scope is deliberately the continuation turns only; the initial (user-initiated)
turn still gets normal prior-session continuity.
"""

from __future__ import annotations

import contextlib
import contextvars

_goal_continuation_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_protoagent_goal_continuation", default=False,
)


def in_goal_continuation() -> bool:
    """True while executing a goal-continuation graph turn."""
    return _goal_continuation_ctx.get()


@contextlib.contextmanager
def goal_continuation_turn():
    """Mark the enclosed graph invocation as a goal-continuation turn."""
    token = _goal_continuation_ctx.set(True)
    try:
        yield
    finally:
        # reset can raise if the generator is torn down in a different context
        # (mirrors the trace_session guard); the contextvar resets on context
        # exit regardless, so swallowing is safe.
        try:
            _goal_continuation_ctx.reset(token)
        except ValueError:
            pass
