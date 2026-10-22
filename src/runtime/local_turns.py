"""Live, cancelable non-A2A chat turns (#4205).

Own references only for the driver's lifetime. These are HTTP/plugin invocations,
not A2A tasks: expose them separately and never invent durable A2A history.

A DETACHED turn (#4277) is the same driver run as a background task whose client may
leave before it finishes: it is registered here like any local turn — it shows in
``snapshot`` and operator Stop cancels it — but it is NOT wrapped in the disconnect
watcher, so a client going away never cancels it. Unlike a live turn (whose only trace
is the metadata ``snapshot``), a detached turn's OUTCOME is retained briefly so a later
poll can read how it ended. The prompt is never stored, live or finished.

Hardening (#4446):

* Concurrent detached turns are bounded by a finite global cap (``_MAX_DETACHED``).
  :func:`start_detached` refuses an over-cap admission with :class:`DetachedAdmissionError`
  BEFORE minting an id or scheduling a task, and closes the coroutine it was handed, so a
  refused admission leaks neither a task nor an un-awaited coroutine. ``POST /api/chat``
  maps that to HTTP 429.
* EVERY cancellation of a detached turn — operator Stop, process shutdown, or one arriving
  before the driver's first step — records ``canceled``, so an authorized poll never
  degrades to an unknown turn. A NON-detached turn keeps its #4205 semantics (a disconnect
  or shutdown cancellation re-raises for the HTTP layer to unwind).
* An INCOGNITO turn's assistant response/messages are NEVER copied into the retained
  outcome: the no-copy privacy policy that keeps a prompt out of the store applies to the
  reply too, so a poll reads a content-free terminal state (plus the structured error).
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


class DetachedAdmissionError(RuntimeError):
    """Raised by :func:`start_detached` when the concurrent-detached cap (``_MAX_DETACHED``)
    is reached. The coroutine handed in is closed before this is raised, so no work is
    scheduled and nothing leaks; ``POST /api/chat`` maps it to HTTP 429 (#4446)."""


@dataclass
class _DetachSpec:
    """Carried into a detached driver's task context so the ``tracked`` wrapper adopts the
    pre-minted id (handed back to the caller at 202 time) and records the final outcome.
    ``error_adapter`` shapes a raised exception into the stored ``error`` — the caller
    supplies ``server.chat.turn_error`` because this package must not import ``server``.
    ``incognito`` makes the wrapper record a content-free outcome (state/error only, never
    the assistant response/messages) for a private turn (#4446)."""
    turn_id: str
    error_adapter: Callable[[BaseException], dict]
    incognito: bool = False


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
# meanwhile), so this never outgrows the live set. Its size is also the live detached count
# the admission cap gates on.
_DETACHED_TASKS: set[asyncio.Task] = set()

# Finite global cap on concurrent in-flight detached turns (#4446). Each detached turn is a
# background task that outlives its client, so an unbounded submit-then-poll surface lets a
# client — or a stuck provider — pin arbitrarily many live turns, growing memory, model
# concurrency, and the strong-ref set above without limit. Admission past this count is
# refused (HTTP 429) and the slot frees as each turn finishes; tests patch it low to drive
# the boundary.
_MAX_DETACHED = 64

# Set only across start_detached()'s create_task call; the copied task context carries it
# to the tracked wrapper, which consumes it once so a child driver is its own turn.
_DETACH: contextvars.ContextVar[_DetachSpec | None] = contextvars.ContextVar("local_turn_detach", default=None)

# Nested drivers in the same detached task share the outer registration.
_DETACHED_OWNER: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar(
    "local_turn_detached_owner", default=None,
)

# Injectable clock so tests can drive TTL eviction deterministically.
_clock: Callable[[], float] = time.monotonic


def snapshot(session_id: str) -> list[dict]:
    """Metadata only — never copy prompts or incognito content into a new store."""
    return [
        {"id": turn.id, "origin": turn.origin, "started_at": turn.started_at,
         "cancel_requested": turn.cancel_requested}
        for turn in _ACTIVE.values() if turn.session_id == session_id
    ]


def start_detached(coro, *, session_id: str, origin: str,
                   error_adapter: Callable[[BaseException], dict],
                   incognito: bool = False) -> str:
    """Register the whole chat call before returning its detached id (#4277).

    The outer wrapper retains outcomes even when chat returns before reaching its
    tracked graph driver (for example, setup is required). Nested tracked drivers
    in this task share that registration; child tasks still get their own turns.

    Raises :class:`DetachedAdmissionError` when the concurrent-detached cap is already
    reached (#4446). The check runs FIRST — before an id is minted or a task scheduled —
    and the ``coro`` handed in is closed, so a refused admission creates no work and leaks
    neither a task nor an un-awaited coroutine. ``incognito`` records a content-free outcome.
    """
    if len(_DETACHED_TASKS) >= _MAX_DETACHED:
        coro.close()
        raise DetachedAdmissionError(
            f"too many concurrent detached turns (cap {_MAX_DETACHED}); retry shortly"
        )
    turn_id = uuid.uuid4().hex

    @tracked
    async def run(message, session_id, **kwargs):
        return await coro

    token = _DETACH.set(_DetachSpec(turn_id, error_adapter, incognito))
    try:
        task = asyncio.create_task(run(None, session_id, origin=origin))
    finally:
        _DETACH.reset(token)
    turn = LocalTurn(turn_id, session_id, origin, task)
    _ACTIVE[turn_id] = turn
    _DETACHED_TASKS.add(task)

    def finished(task):
        _DETACHED_TASKS.discard(task)
        # Cancellation before the task's first step never enters tracked's finally.
        if _ACTIVE.get(turn_id) is turn:
            coro.close()
            _record(turn, "canceled", response=None, messages=None, error=None)
            _ACTIVE.pop(turn_id, None)
            turn.stopped.set()

    task.add_done_callback(finished)
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
    canceled) so a later poll can read it. EVERY cancellation of a detached turn is recorded
    as ``canceled`` — operator Stop, process shutdown, and (via :func:`start_detached`'s
    done-callback) one arriving before the first step — so an authorized poll never degrades
    to an unknown turn. An incognito spec keeps the recorded outcome content-free (state and
    error only, never the assistant response/messages). Without a spec the behaviour is
    unchanged — the non-detached ``/api/chat`` / ``/v1`` / console paths register and clean
    up exactly as before, a disconnect/shutdown cancellation re-raises, and a raised
    exception still propagates to the caller.
    """
    @functools.wraps(driver)
    async def run(message, session_id, **kwargs):
        task = asyncio.current_task()
        assert task is not None
        if _DETACHED_OWNER.get() is task:
            return await driver(message, session_id, **kwargs)
        spec = _DETACH.get()
        if spec is not None:
            # Consume: a child driver spawned under this turn is its own turn, not this one.
            _DETACH.set(None)
            _DETACHED_OWNER.set(task)
        turn_id = spec.turn_id if spec is not None else uuid.uuid4().hex
        incognito = spec.incognito if spec is not None else False
        turn = _ACTIVE.get(turn_id) or LocalTurn(turn_id, session_id, kwargs.get("origin", "local"), task)
        _ACTIVE[turn.id] = turn
        try:
            result = await driver(message, session_id, **kwargs)
            if spec is not None:
                err = _result_error(result)
                state = "canceled" if err and err.get("type") == "turn_cancelled" else ("failed" if err else "completed")
                # Incognito keeps a content-free terminal outcome: the state and structured
                # error are retained, but the assistant response/messages NEVER are (#4446).
                _record(turn, state,
                        response=None if incognito else _response_text(result),
                        messages=None if incognito else result, error=err)
            return result
        except asyncio.CancelledError:
            # A NON-detached turn (no spec) keeps its #4205 semantics: a disconnect or
            # shutdown cancellation that is not an operator Stop re-raises so the HTTP layer
            # unwinds it. A detached turn has no caller to receive the raise, so EVERY
            # cancellation — operator Stop, process shutdown, or one arriving before the
            # driver's first step — is recorded as `canceled`, never left unrecorded for an
            # authorized poll to read as an unknown turn (#4446).
            if spec is None and not turn.cancel_requested:
                raise
            msg = [{"role": "assistant", "content": "Turn stopped. You can choose another model and continue this conversation.",
                    "error": {"type": "turn_cancelled", "message": "Turn stopped.", "upstream_status": None}}]
            if spec is not None:
                _record(turn, "canceled", response=None,
                        messages=None if incognito else msg, error=msg[0]["error"])
            return msg
        except Exception as exc:  # noqa: BLE001 — a detached turn has no caller to receive the raise
            if spec is None:
                raise
            # Already content-free (no response/messages); the error adapter shapes the raise.
            _record(turn, "failed", response=None, messages=None, error=spec.error_adapter(exc))
            return None
        finally:
            _ACTIVE.pop(turn.id, None)
            turn.stopped.set()
    return run
