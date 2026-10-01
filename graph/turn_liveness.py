"""Turn liveness: progress from an in-flight subagent run, and why a turn was stopped (#3940).

**Progress.**

A subagent run (``graph.agent._run_subagent_inner``) streams its sub-graph's state one
super-step at a time — each model call, each tool round. A caller that runs subagents
OUTSIDE a lead graph (the ``/<workflow>`` short-circuit, whose steps go through
``graph.sdk.run_subagent``) otherwise sees nothing between a step's start and its end,
and the A2A stall guard (``a2a_impl.executor._stall_guarded``) reads that silence as a
hang. Binding a listener with :class:`progress_scope` lets such a caller hear each
super-step and turn it into a frame the stall guard counts.

The signal is REAL progress, never a timer: a step wedged inside ONE tool call (or one
model call) completes no super-step, so it stays silent and the stall guard still ends
it — exactly as it ends a native turn wedged the same way. A step that keeps making
progress is bounded by its subagent's ``max_turns`` and the recipe's opt-in per-step
``timeout``, as before.

A ``ContextVar``, so the listener follows the run into the tasks the workflow engine
spawns per step (each copies the context it was created in) and never leaks into an
unrelated turn. Outside a scope :func:`note_progress` is a no-op.

**Stop reason.** When the stall guard ends a turn it cancels the work under it, and to
that work a cancel looks the same whether the guard sent it or an operator did (an A2A
``CancelTask``). The guard records WHY on a :class:`TurnStop` it binds into the context
it runs the turn in, BEFORE it cancels. Everything the turn spawned (a ``/<workflow>``
runner and its step tasks) copied that context, so it shares the same object and can tell
a stall (a failure) from a cancel. Empty outside a guarded turn. Host-free.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

log = logging.getLogger(__name__)

ProgressListener = Callable[[str], None]

_listener_ctx: contextvars.ContextVar[ProgressListener | None] = contextvars.ContextVar(
    "protoagent_turn_progress", default=None
)


def note_progress(subagent_type: str) -> None:
    """Tell the bound listener (if any) that a ``subagent_type`` run just completed a
    super-step. Never raises: liveness reporting must not break the run it reports on."""
    listener = _listener_ctx.get()
    if listener is None:
        return
    try:
        listener(subagent_type)
    except Exception:  # noqa: BLE001 — best-effort by contract
        log.debug("[subagent] progress listener raised", exc_info=True)


class progress_scope:
    """Bind ``listener`` for subagent runs started inside the enclosed block.

    ``listener(subagent_type)`` is called synchronously from the run's own task, once per
    super-step; keep it cheap and non-blocking (e.g. ``queue.put_nowait``)."""

    def __init__(self, listener: ProgressListener | None):
        self._listener = listener
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = _listener_ctx.set(self._listener)
        return self

    def __exit__(self, *_exc):
        if self._token is not None:
            try:
                _listener_ctx.reset(self._token)
            except ValueError:  # exited in a different context than it was entered in
                _listener_ctx.set(None)
            self._token = None


class TurnStop:
    """Mutable, shared by every context copied from the one it was bound into."""

    __slots__ = ("reason",)

    def __init__(self) -> None:
        self.reason = ""


_stop_ctx: contextvars.ContextVar[TurnStop | None] = contextvars.ContextVar("protoagent_turn_stop", default=None)


def bind_turn_stop(ctx: contextvars.Context, stop: TurnStop) -> None:
    """Bind ``stop`` into ``ctx`` (a context the caller runs the turn's work in)."""
    ctx.run(_stop_ctx.set, stop)


def turn_stop_reason() -> str:
    """Why the current turn was stopped from above (a stall), or ``""`` — not stopped,
    or stopped by a plain cancel."""
    stop = _stop_ctx.get()
    return stop.reason if stop is not None else ""
