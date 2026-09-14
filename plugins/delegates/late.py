"""Late arrivals — collecting what a room member finished after the room stopped waiting (#3360b).

A member whose address trips its no-progress bound is dropped from the room's later rounds
(``graph/room_rounds._dropped``) and is never re-addressed: a retry would be a SECOND
``SendMessage`` task on a peer still busy with the first. But the first task is still
running, and since #3360 the adapter gets its id back at once — so "rejoin" can be
**collection** rather than re-addressing. When the adapter gives up it keeps the task id in
``conversations``' pending slot; this module polls that one task with ``GetTask`` until it
settles, then hands whatever it produced back to the chat.

Three properties hold by construction rather than by care:

* **Nothing here can open a task.** The only wire call is ``A2aAdapter.get_task`` — one
  read-only ``GetTask``. No argument, state or peer reply turns a collection into a dispatch.
* **The room is unchanged.** A late arrival is not a speaking turn: it never re-enters
  ``plan_round``, never extends ``max_rounds`` and never makes the member addressable again.
  It lands the way a background ``delegate_to`` reply does — through
  ``BackgroundManager.spawn_work(result_author=…)``: persisted as the member's own room
  message, drained into the session and followed by a lead turn
  (``server.chat._drain_background``).
* **Erasing history withdraws it.** The pending handle obeys the conversation's invalidation
  rules — rewind, delete, a fork onto the thread, a re-pointed delegate
  (``conversations.forget``). A collection re-checks its handle before every poll and after
  every ``GetTask``; one whose handle has gone delivers nothing, so an answer to history the
  operator erased cannot reappear in it.

The WAIT runs as a plain task, not a background job: ``spawn_work`` holds a slot in the
background concurrency cap for its whole life, and a collection can wait on a slow peer for
up to ``_COLLECT_MAX_S``. Only the settled result goes through ``spawn_work``, which then
completes at once. Process-local, like the handle it polls: a restart loses both, and the
member simply stays failed — which is all a room ever did before this existed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import conversations
from .adapters import ADAPTERS, Delegate, DelegateError, _continuity_credential, _park_message

logger = logging.getLogger("protoagent.plugins.delegates")

# The member JUST tripped its no-progress bound, so the first look waits a moment and later
# ones back off: a collection is a courtesy running beside the chat, not a hot loop.
_FIRST_POLL_S = 2.0
_MAX_POLL_S = 30.0
_BACKOFF = 1.5
# How long a collection keeps waiting on a peer that is still working before it says so.
_COLLECT_MAX_S = 3600.0
# Consecutive failed GetTasks (a peer restarting, a network blip) tolerated before giving up.
_MAX_TRANSPORT_FAILURES = 8

ANSWERED = "answered"
PARKED = "parked"
FAILED = "failed"
WITHDRAWN = "withdrawn"

# Leads the late message, so neither the operator nor a later catch-up reads it as the
# member's reply to whatever the room is discussing NOW.
LATE_MARKER = "_(Arrived after its turn — the room had already moved on.)_"

# (conversation_key, delegate name, url, task_id) -> the running collection.
_RUNNING: dict[tuple[str, str, str, str], asyncio.Task] = {}


def start(registry, conversation_key: str, name: str, *, session_id: str = "") -> bool:
    """Collect ``name``'s unfinished task in ``conversation_key``, if its last address left one.

    Returns whether a collection is now running for it: ``False`` when there is nothing to
    collect (the address answered, failed some other way, or the delegate is not ``a2a``),
    or when called outside an event loop. A second call while one runs for the same task is
    a no-op that returns ``True``. ``session_id`` is where the result is delivered; blank
    falls back to the session the handle recorded.
    """
    d = registry.get(name) if registry is not None else None
    if d is None or getattr(d, "type", "") != "a2a" or not conversation_key:
        return False
    credential = _continuity_credential(d)
    pending = conversations.pending_for(conversation_key, d.name, d.url, credential)
    if pending is None:
        return False
    key = (conversation_key, d.name, d.url, pending.task_id)
    running = _RUNNING.get(key)
    if running is not None and not running.done():
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(
        _collect_and_deliver(d, conversation_key, credential, pending, session_id or pending.session_id),
        name=f"delegates.late.{d.name}.{pending.task_id}",
    )
    _RUNNING[key] = task
    task.add_done_callback(lambda _t, k=key: _RUNNING.pop(k, None))
    return True


async def collect(
    d: Delegate,
    conversation_key: str,
    credential: str,
    pending: conversations._Pending,
    *,
    sleep: Callable[[float], Awaitable] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[str, str]:
    """Poll ONE pending task until it settles; returns ``(outcome, text)``.

    ``outcome`` is ``ANSWERED`` (``text`` is the answer), ``PARKED`` (the task stopped on a
    question — ``text`` carries it and the resume handle, for the lead), ``FAILED`` (``text``
    says why, in words for the operator) or ``WITHDRAWN`` (deliver nothing: the handle was
    erased, or the peer no longer knows the task). The handle is released on every settled
    outcome. Never raises ``DelegateError``.
    """
    from tools.a2a_parse import _extract_context_id, _extract_text, _is_input_required, classify_answer, state_name

    adapter = ADAPTERS["a2a"]
    # Resolved per call, not bound as defaults at import, so the running module's own
    # ``asyncio.sleep`` / ``time.monotonic`` are the ones used.
    sleep = sleep or asyncio.sleep
    clock = clock or time.monotonic

    def held() -> bool:
        current = conversations.pending_for(conversation_key, d.name, d.url, credential)
        return current is not None and current.task_id == pending.task_id

    def release() -> None:
        conversations.forget_pending(conversation_key, d.name, d.url, credential, task_id=pending.task_id)

    started = clock()
    wait = _FIRST_POLL_S
    failures = 0
    while True:
        await sleep(wait)
        wait = min(wait * _BACKOFF, _MAX_POLL_S)
        if not held():
            return WITHDRAWN, ""
        if clock() - started >= _COLLECT_MAX_S:
            release()
            return FAILED, (
                f"stopped waiting for @{d.name}'s unfinished task after {int(_COLLECT_MAX_S // 60)} "
                "minutes — it was still working."
            )
        try:
            result = await adapter.get_task(d, pending.task_id)
        except DelegateError as exc:
            failures += 1
            if failures >= _MAX_TRANSPORT_FAILURES:
                release()
                return FAILED, f"lost touch with @{d.name} while waiting for its unfinished task: {exc}"
            continue
        failures = 0
        # Again AFTER the await: a rewind or a delete can land while GetTask is in flight.
        if not held():
            return WITHDRAWN, ""
        if result is None:
            # The peer no longer knows the task — it restarted, or its retention expired.
            # Task retention is the peer's policy, so this is normal: nothing more arrives,
            # which is exactly what the room did before collection existed.
            release()
            return WITHDRAWN, ""
        state = ((result.get("task", result) or {}).get("status") or {}).get("state")
        if _is_input_required(state):
            release()
            return PARKED, _park_message(d.name, pending.task_id, _extract_text(result) or "")
        verdict = classify_answer(result)
        if verdict.answerable:
            release()
            text = (_extract_text(result) or "").strip()
            if not text:
                return FAILED, f"@{d.name} finished its unfinished task but returned no text (state={state})."
            # The answer is about to be written onto this thread, so the peer's context and
            # the room agree again and the next address may continue it — unless a newer
            # address already opened a fresh context, which this older one must not replace.
            if not conversations.remembered(conversation_key, d.name, d.url, credential):
                conversations.remember(
                    conversation_key,
                    d.name,
                    d.url,
                    _extract_context_id(result),
                    credential,
                    session_id=pending.session_id,
                )
            return ANSWERED, text
        if verdict.failed:
            release()
            diag = " ".join((_extract_text(result) or "").split())[:500]
            return FAILED, (
                f"@{d.name} {state_name(state)} its unfinished task (state={state})" + (f": {diag}" if diag else "")
            )
        # Still working: poll again.


async def deliver(name: str, task_id: str, session_id: str, outcome: str, text: str) -> bool:
    """Hand a settled collection to its origin session as ``name``'s own late room message.

    Through ``BackgroundManager.spawn_work(result_author=…)``, the same path as a background
    ``delegate_to`` reply: the drain persists it as the member's room message and the
    completion wakes the lead. A ``FAILED`` outcome settles the job as failed, so the room
    message is marked failed rather than read as the member's words. Returns whether it was
    handed over — ``False`` with no background manager (a lean/CLI context) or no session.
    """
    try:
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
    except Exception:  # noqa: BLE001 — no runtime state (a unit test, a bare import)
        mgr = None
    if mgr is None or not session_id:
        logger.warning(
            "[delegates] no background manager or origin session — @%s's late %s (task %s) is dropped",
            name,
            outcome,
            task_id,
        )
        return False
    body = f"{LATE_MARKER}\n\n{text}"
    failed = outcome == FAILED

    async def _work() -> str:
        if failed:
            raise DelegateError(body)
        return body

    await mgr.spawn_work(
        origin_session=session_id,
        kind="delegate",
        description=f"late reply ← {name}",
        detail=f"a2a task {task_id}",
        work=_work,
        result_author=name,
    )
    return True


async def _collect_and_deliver(
    d: Delegate, conversation_key: str, credential: str, pending: conversations._Pending, session_id: str
) -> None:
    try:
        outcome, text = await collect(d, conversation_key, credential, pending)
        if outcome == WITHDRAWN:
            logger.info("[delegates] collection of @%s's task %s withdrawn", d.name, pending.task_id)
            return
        await deliver(d.name, pending.task_id, session_id, outcome, text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a collection must never take anything else down with it
        logger.exception("[delegates] collecting @%s's task %s failed", d.name, pending.task_id)


def running() -> dict[tuple[str, str, str, str], asyncio.Task]:
    """The collections in flight (copy) — for tests and debugging."""
    return dict(_RUNNING)
