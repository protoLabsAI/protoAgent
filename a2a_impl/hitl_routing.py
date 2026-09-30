"""Keep a context's HITL pause on ONE A2A task (#3930).

A2A models a pause for operator input as an *interrupted* task state, not a terminal
one: a task in ``TASK_STATE_INPUT_REQUIRED`` waits, and "the client continues the
interaction by sending a new message with the same ``taskId`` and ``contextId``" (A2A
spec §3.4.3). a2a-sdk implements exactly that: a message carrying the parked task's id
re-enters ``execute()`` with ``current_task`` still input-required, and the executor
resumes the graph on the same task.

The console never sent that task id. Its form answer (``hitl_resume`` metadata) arrived
as a FRESH task, which resumed the LangGraph interrupt correctly (``server.turn_control``
converts it to ``Command(resume=…)``) but left the task that parked in input-required for
good — and ``a2a_impl.stores`` rightly keeps input-required tasks out of the TTL sweep,
so the orphan lived forever and a fresh browser rebuilt it as a spinning ``ask_human``
card. A composer message HELD behind a pending form (#1560) did the same: its task
re-parks on the same interrupt, so the context then carried two input-required tasks
for one pause.

This module keeps one parked task per context:

- **Route** (:meth:`ParkedTaskRouter.route`): a ``hitl_resume`` message without a task
  id — or naming a task that is no longer paused — is pointed at the context's newest
  input-required task before the SDK sees it, so the answer continues THAT task, the way
  the spec says a client should.
- **Settle** (:meth:`ParkedTaskRouter.settle_siblings`): when a task parks, or a resume
  starts, every OTHER input-required task in the context is waiting on an interrupt that
  is no longer the pending one. Each is completed through the SDK itself — a one-shot
  internal message the executor recognises (:func:`take_settle`) — with a status message
  pointing at the task that superseded it. Going through the SDK (rather than writing the
  row) finishes its in-memory ``ActiveTask`` too, so any ``SubscribeToTask`` still
  attached to it gets the terminal frame and closes.

A settle message is marked (``SETTLE_MARKER`` metadata) and registered by ``message_id``
before it is sent, with the id of the pause it was scheduled against. The executor never
runs the graph for a marked message: it completes the task only when the registration is
present AND the task is still paused on that same pause; anything else (a forged marker,
a registration that aged out, or a task another request already answered or re-paused
while the settle waited in its queue) is a no-op. A registration is consumed by the
executor (or expires), never dropped when ``on_message_send`` returns: that call can
return on another in-flight request's event while this one is still queued. Contexts are the grouping key because a console session's LangGraph thread is its
context (``server.turn_control._resolve_thread_id``) — one thread has one pending interrupt.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from a2a.types import Message, Part, Role, SendMessageRequest, Task, TaskState
from a2a.types.a2a_pb2 import ListTasksRequest
from a2a.utils.errors import InvalidParamsError

log = logging.getLogger(__name__)

# The states a task waits in for its client. Only input-required is ever produced by this
# executor's HITL pause, and only an input-required task is routed to; auth-required is
# listed for the "is this task still paused" check.
_PAUSED = (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED)

# Metadata key marking a settle message. The executor never runs the graph for a message
# carrying it (see settle_decision) — a forged one is a no-op, never an answer.
SETTLE_MARKER = "protoagent_settle"
# A registration outlives any plausible queue wait behind a running turn; past this it is
# pruned, and its settle (if it ever runs) is a no-op.
_SETTLE_TTL_S = 60 * 60


@dataclass
class _Settle:
    superseded_by: str
    pause_id: str  # the status message id of the pause the settle was scheduled against
    registered_at: float


# message_id → the settle registered for it.
_PENDING_SETTLES: dict[str, _Settle] = {}

_ROUTER: list[ParkedTaskRouter | None] = [None]


def _pause_id(task: Any) -> str:
    try:
        return task.status.message.message_id or ""
    except AttributeError:
        return ""


def _register(message_id: str, superseded_by: str, pause_id: str) -> None:
    now = time.monotonic()
    for key in [k for k, v in _PENDING_SETTLES.items() if now - v.registered_at > _SETTLE_TTL_S]:
        _PENDING_SETTLES.pop(key, None)
    _PENDING_SETTLES[message_id] = _Settle(superseded_by, pause_id, now)


def is_settle_message(message: Message | None) -> bool:
    return bool(
        message is not None and message.HasField("metadata") and SETTLE_MARKER in message.metadata.fields
    )


def settle_decision(message: Message | None, current_task: Any) -> tuple[bool, str | None]:
    """``(is_settle, superseded_by)`` for a message the executor is about to run.

    ``is_settle`` is True for any message marked as a settle — the executor must then NOT
    run the graph. ``superseded_by`` is set only when the settle is registered (consumed
    here) AND ``current_task`` is still paused on the pause it was scheduled against;
    otherwise the settle is stale or forged and the executor does nothing."""
    if not is_settle_message(message):
        return False, None
    settle = _PENDING_SETTLES.pop(message.message_id, None) if message.message_id else None
    if settle is None:
        return True, None
    try:
        paused = current_task is not None and current_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    except AttributeError:
        paused = False
    if not paused or _pause_id(current_task) != settle.pause_id:
        return True, None
    return True, settle.superseded_by


def _is_hitl_resume(message: Message) -> bool:
    if not message.HasField("metadata"):
        return False
    value = message.metadata.fields.get("hitl_resume")
    return value is not None and value.WhichOneof("kind") == "bool_value" and value.bool_value


class ParkedTaskRouter:
    """Routes HITL answers onto the parked task and settles superseded pauses."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._send = handler.on_message_send  # the unwrapped SDK method
        # Background settles, strong-ref'd until done (an unreferenced asyncio task can
        # be garbage-collected mid-flight).
        self._inflight: set[asyncio.Task] = set()

    async def parked_tasks(self, context_id: str, call_context: Any) -> list[Task]:
        """The context's input-required tasks, newest first."""
        if not context_id:
            return []
        page = await self._handler.task_store.list(
            ListTasksRequest(context_id=context_id, status=TaskState.TASK_STATE_INPUT_REQUIRED, page_size=50),
            call_context,
        )
        return list(page.tasks)

    async def route(self, params: SendMessageRequest, call_context: Any) -> None:
        """Point a ``hitl_resume`` message at the context's parked task (in place).

        Also refuses a message naming a task of ANOTHER context: the SDK builds its request
        context without the task and never compares the two, so the message would run
        against that task under the wrong context's thread."""
        message = params.message
        if is_settle_message(message) and message.message_id not in _PENDING_SETTLES:
            # Only this module sends settles; a client's copy just loses the marker's meaning.
            message.metadata.fields.pop(SETTLE_MARKER, None)
        named = None
        if message.task_id and message.context_id:
            named = await self._handler.task_store.get(message.task_id, call_context)
            if named is not None and named.context_id and named.context_id != message.context_id:
                raise InvalidParamsError(
                    message=f"Task {message.task_id} belongs to another context than {message.context_id}"
                )
        if not _is_hitl_resume(message) or not message.context_id:
            return
        try:
            parked = await self.parked_tasks(message.context_id, call_context)
            if named is not None and named.status.state in _PAUSED and (not parked or parked[0].id == named.id):
                return  # continues THE paused task — the spec's own path
            # Otherwise the named task ended, or is an OLDER pause a newer task took over
            # (a held message re-parked): the newest parked task owns the interrupt, and
            # the older one is about to be settled — answering it would race that.
        except Exception:  # noqa: BLE001 — routing is an improvement, never a new failure
            log.warning("[a2a] could not look up the parked task for a HITL answer", exc_info=True)
            return
        if parked:
            if message.task_id:
                log.info(
                    "[a2a] HITL answer named task %s, which is no longer paused; routing it to %s",
                    message.task_id,
                    parked[0].id,
                )
            message.task_id = parked[0].id
        elif message.task_id:
            # The named task ended (or is unknown) and nothing is parked: continuing it would
            # be refused by the SDK. Run the answer as a fresh task, as before #3930.
            message.task_id = ""

    async def settle_siblings(self, context_id: str, keep_task_id: str, call_context: Any) -> list[str]:
        """Complete every input-required task in ``context_id`` except ``keep_task_id``,
        each pointing at it. Returns the ids settled."""
        settled: list[str] = []
        for task in await self.parked_tasks(context_id, call_context):
            if task.id == keep_task_id:
                continue
            if await self._busy(task.id):
                # A request is already running or queued on it (an answer that named it):
                # that request owns the task's outcome. Never race it.
                log.info("[a2a] not settling parked task %s — a request is in flight on it", task.id)
                continue
            message_id = f"settle-{uuid.uuid4()}"
            message = Message(
                message_id=message_id,
                context_id=context_id,
                task_id=task.id,
                role=Role.ROLE_USER,
                parts=[Part(text=f"[superseded by task {keep_task_id}]")],
            )
            # `hidden`: a chat rebuilt from this task's history draws no bubble for it.
            message.metadata.update({"hidden": True, SETTLE_MARKER: True})
            # Consumed by the executor (settle_decision) or pruned by age — NOT dropped when
            # the send returns: the SDK can answer it off another request's event while
            # this one is still queued on the task.
            _register(message_id, keep_task_id, _pause_id(task))
            try:
                await self._send(SendMessageRequest(message=message), call_context)
                settled.append(task.id)
                log.info("[a2a] settled parked task %s — superseded by %s", task.id, keep_task_id)
            except Exception:  # noqa: BLE001 — one stuck sibling must not block the rest
                log.warning("[a2a] could not settle parked task %s", task.id, exc_info=True)
        return settled

    async def _busy(self, task_id: str) -> bool:
        """Whether a request is running or queued on the task's live ``ActiveTask``.

        Reads a2a-sdk internals (``_request_lock`` / ``_request_queue``), guarded: if they
        move, this answers False and the executor's settle_decision check still keeps a
        settle from ever running as input."""
        try:
            active = await self._handler._active_task_registry.get(task_id)
            if active is None:
                return False
            lock = getattr(active, "_request_lock", None)
            queue = getattr(active, "_request_queue", None)
            return bool((lock is not None and lock.locked()) or (queue is not None and queue.qsize() > 0))
        except Exception:  # noqa: BLE001
            return False

    def schedule_settle(self, context_id: str, keep_task_id: str, call_context: Any) -> asyncio.Task | None:
        """Run :meth:`settle_siblings` in the background (the caller is a turn's producer:
        it must not wait on other tasks' producers)."""
        if not context_id or not keep_task_id:
            return None
        task = asyncio.create_task(
            self._settle_quietly(context_id, keep_task_id, call_context),
            name=f"a2a-settle-siblings:{keep_task_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    async def _settle_quietly(self, context_id: str, keep_task_id: str, call_context: Any) -> None:
        try:
            await self.settle_siblings(context_id, keep_task_id, call_context)
        except Exception:  # noqa: BLE001 — best-effort housekeeping
            log.warning("[a2a] settling parked siblings of %s failed", keep_task_id, exc_info=True)

    async def drain(self) -> None:
        """Wait for in-flight background settles (tests, shutdown)."""
        while self._inflight:
            await asyncio.wait(set(self._inflight))


def schedule_settle_siblings(context_id: str, keep_task_id: str, call_context: Any) -> asyncio.Task | None:
    """Settle the context's other parked tasks via the installed router; no-op without one."""
    router = _ROUTER[0]
    if router is None:
        return None
    try:
        return router.schedule_settle(context_id, keep_task_id, call_context)
    except Exception:  # noqa: BLE001 — never break the turn that asked
        log.warning("[a2a] could not schedule settling parked siblings of %s", keep_task_id, exc_info=True)
        return None


def install_parked_task_routing(handler: Any) -> ParkedTaskRouter | None:
    """Wrap ``handler``'s message-send entry points with :meth:`ParkedTaskRouter.route`
    and register the router the executor settles through. Returns the router, or
    ``None`` (logged; stock behaviour kept) if the handler lacks the expected surface."""
    try:
        router = ParkedTaskRouter(handler)
        send = handler.on_message_send
        send_stream = handler.on_message_send_stream
        _ = handler.task_store
    except Exception:  # noqa: BLE001
        log.warning("[a2a] parked-task routing not installed (unexpected request handler)", exc_info=True)
        return None

    async def on_message_send(params: SendMessageRequest, context: Any):
        await router.route(params, context)
        return await send(params, context)

    async def on_message_send_stream(params: SendMessageRequest, context: Any):
        await router.route(params, context)
        async for event in send_stream(params, context):
            yield event

    handler.on_message_send = on_message_send
    handler.on_message_send_stream = on_message_send_stream
    _ROUTER[0] = router
    return router
