"""Live, cancelable non-A2A chat turns (#4205).

Own references only for the driver's lifetime. These are HTTP/plugin invocations,
not A2A tasks: expose them separately and never invent durable A2A history.

A DETACHED turn (#4277) is the same driver run as a background task whose client may
leave before it finishes: it is registered here like any local turn — it shows in
``snapshot`` and operator Stop cancels it — but it is NOT wrapped in the disconnect
watcher, so a client going away never cancels it. Unlike a live turn (whose only trace
is the metadata ``snapshot``), a detached turn's OUTCOME is retained briefly so a later
poll can read how it ended. The prompt is never stored, live or finished.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class LocalTurn:
    id: str
    session_id: str
    origin: str
    task: asyncio.Task
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False


@dataclass
class _DetachSpec:
    """Carried into a detached driver's task context so the ``tracked`` wrapper adopts the
    pre-minted id (handed back to the caller at 202 time) and records the final outcome.
    ``error_adapter`` shapes a raised exception into the stored ``error`` — the caller
    supplies ``server.chat.turn_error`` because this package must not import ``server``."""
    turn_id: str
    error_adapter: Callable[[BaseException], dict]


@dataclass
class _Finished:
    """A detached turn's retained outcome. Never holds the prompt — only the assistant
    ``response``/``messages`` and the structured ``error`` (``turn_error`` shape)."""
    session_id: str
    state: str  # completed | failed | canceled
    response: str | None
    messages: list[dict] | None
    error: dict | None
    at: float  # monotonic seconds, for TTL eviction


# All accesses happen on the serving event loop; no executor-thread mutations.
_ACTIVE: dict[str, LocalTurn] = {}

# Outcomes of finished detached turns, oldest first. Bounded by count and age so a
# long-lived process never accumulates turn records (#4277).
_FINISHED: OrderedDict[str, _Finished] = OrderedDict()
_MAX_FINISHED = 200
_FINISHED_TTL_S = 3600.0

# Strong refs to in-flight detached tasks — a background task with no referrer can be
# garbage-collected mid-run. Each is dropped as it finishes (its _ACTIVE entry holds it
# meanwhile), so this never outgrows the live set.
_DETACHED_TASKS: set[asyncio.Task] = set()

# Set only across start_detached()'s create_task call; the copied task context carries it
# to the tracked wrapper, which consumes it once so a child driver is its own turn.
_DETACH: contextvars.ContextVar[_DetachSpec | None] = contextvars.ContextVar("local_turn_detach", default=None)

# Injectable clock so tests can drive TTL eviction deterministically.
_clock: Callable[[], float] = time.monotonic


def snapshot(session_id: str) -> list[dict]:
    """Metadata only — never copy prompts or incognito content into a new store."""
    return [
        {"id": turn.id, "origin": turn.origin, "started_at": turn.started_at,
         "cancel_requested": turn.cancel_requested}
        for turn in _ACTIVE.values() if turn.session_id == session_id
    ]


def start_detached(coro, *, error_adapter: Callable[[BaseException], dict]) -> str:
    """Run ``coro`` (a ``chat()`` driver call) as a background turn and return its id (#4277).

    The turn is registered exactly like any local turn — it appears in :func:`snapshot` and
    operator :func:`cancel` stops it — but it is NOT wrapped in the disconnect watcher, so a
    client leaving never cancels it. The id is minted here and adopted by the ``tracked``
    wrapper (via a task-scoped context var) so it can be returned before the turn runs.
    ``error_adapter`` shapes a raised exception into the stored ``error``.
    """
    turn_id = uuid.uuid4().hex
    token = _DETACH.set(_DetachSpec(turn_id, error_adapter))
    try:
        # create_task snapshots the current context, carrying the spec into the task.
        task = asyncio.create_task(coro)
    finally:
        _DETACH.reset(token)
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_DETACHED_TASKS.discard)
    return turn_id


def _result_error(messages) -> dict | None:
    """The structured ``error`` a failed/cancelled turn carries, mirroring the /api/chat
    and /v1 readers (``next(... m["error"] ...)``)."""
    return next(
        (m["error"] for m in messages or [] if isinstance(m, dict) and isinstance(m.get("error"), dict)),
        None,
    )


def _response_text(messages) -> str:
    """Joined assistant text — the same ``response`` string ``POST /api/chat`` returns."""
    return "\n\n".join(
        m["content"] for m in messages or []
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content")
    )


def _record(turn: LocalTurn, state: str, *, response: str | None,
            messages: list[dict] | None, error: dict | None) -> None:
    _FINISHED[turn.id] = _Finished(turn.session_id, state, response, messages, error, _clock())
    _FINISHED.move_to_end(turn.id)
    _prune_finished()


def _prune_finished() -> None:
    """Enforce the age and count bounds (#4277) — called on every write and read."""
    now = _clock()
    for turn_id in [tid for tid, fin in _FINISHED.items() if now - fin.at > _FINISHED_TTL_S]:
        _FINISHED.pop(turn_id, None)
    while len(_FINISHED) > _MAX_FINISHED:
        _FINISHED.popitem(last=False)


def get_turn(session_id: str, turn_id: str) -> dict | None:
    """The poll answer for ``GET …/turns/{turn_id}`` (#4277): ``working`` while the turn
    runs, else its recorded outcome, else ``None`` (the route maps that to 404). The prompt
    is never stored, so never present."""
    live = _ACTIVE.get(turn_id)
    if live is not None and live.session_id == session_id:
        return {"turn_id": turn_id, "state": "working"}
    _prune_finished()
    fin = _FINISHED.get(turn_id)
    if fin is None or fin.session_id != session_id:
        return None
    out: dict = {"turn_id": turn_id, "state": fin.state}
    if fin.response is not None:
        out["response"] = fin.response
    if fin.messages is not None:
        out["messages"] = fin.messages
    if fin.error is not None:
        out["error"] = fin.error
    return out


async def cancel(session_id: str, turn_id: str, *, timeout: float = 5) -> bool:
    """Cancel one invocation and wait for its driver/lock cleanup.

    A timeout is not a successful stop. Keep the entry reachable and avoid a second
    cancel interrupting cleanup; the caller can poll or retry the wait.
    """
    turn = _ACTIVE.get(turn_id)
    if turn is None or turn.session_id != session_id:
        return False
    if not turn.cancel_requested:
        turn.cancel_requested = True
        # Disconnect may already be unwinding this same driver.
        if not turn.task.cancelling():
            turn.task.cancel()
    await asyncio.wait_for(turn.stopped.wait(), timeout)
    return True


def cancellation_requested() -> bool:
    """Whether this driver's cancellation was an explicit operator Stop."""
    task = asyncio.current_task()
    return any(turn.task is task and turn.cancel_requested for turn in _ACTIVE.values())


def tracked(driver):
    """Register the entire driver, including its wait for the session lock.

    A detached launch (:func:`start_detached`) leaves a spec in this task's context: the
    wrapper adopts its pre-minted id and records the final outcome (completed/failed/
    canceled) so a later poll can read it. Without a spec the behaviour is unchanged — the
    non-detached ``/api/chat`` / ``/v1`` / console paths register and clean up exactly as
    before, and a raised exception still propagates to the caller.
    """
    @functools.wraps(driver)
    async def run(message, session_id, **kwargs):
        task = asyncio.current_task()
        assert task is not None
        spec = _DETACH.get()
        if spec is not None:
            # Consume: a child driver spawned under this turn is its own turn, not this one.
            _DETACH.set(None)
        turn_id = spec.turn_id if spec is not None else uuid.uuid4().hex
        turn = LocalTurn(turn_id, session_id, kwargs.get("origin", "local"), task)
        _ACTIVE[turn.id] = turn
        try:
            result = await driver(message, session_id, **kwargs)
            if spec is not None:
                err = _result_error(result)
                state = "canceled" if err and err.get("type") == "turn_cancelled" else ("failed" if err else "completed")
                _record(turn, state, response=_response_text(result), messages=result, error=err)
            return result
        except asyncio.CancelledError:
            if not turn.cancel_requested:
                raise  # disconnect/shutdown cancellation keeps its normal semantics
            msg = [{"role": "assistant", "content": "Turn stopped. You can choose another model and continue this conversation.",
                    "error": {"type": "turn_cancelled", "message": "Turn stopped.", "upstream_status": None}}]
            if spec is not None:
                _record(turn, "canceled", response=None, messages=msg, error=msg[0]["error"])
            return msg
        except Exception as exc:  # noqa: BLE001 — a detached turn has no caller to receive the raise
            if spec is None:
                raise
            _record(turn, "failed", response=None, messages=None, error=spec.error_adapter(exc))
            return None
        finally:
            _ACTIVE.pop(turn.id, None)
            turn.stopped.set()
    return run
