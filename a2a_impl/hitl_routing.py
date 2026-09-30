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

The internal settle message is recognised by its ``message_id`` alone, registered here a
moment before it is sent: nothing a client can put on the wire marks a message as a
settle. Contexts are the grouping key because a console session's LangGraph thread is its
context (``server.turn_control._resolve_thread_id``) — one thread has one pending interrupt.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from a2a.types import Message, Part, Role, SendMessageRequest, Task, TaskState
from a2a.types.a2a_pb2 import ListTasksRequest

log = logging.getLogger(__name__)

# The states a task waits in for its client. Only input-required is ever produced by this
# executor's HITL pause, and only an input-required task is routed to; auth-required is
# listed for the "is this task still paused" check.
_PAUSED = (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED)

# message_id → the id of the task that superseded the one the message is sent to.
_PENDING_SETTLES: dict[str, str] = {}

_ROUTER: list[ParkedTaskRouter | None] = [None]


def take_settle(message: Message | None) -> str | None:
    """The superseding task id if ``message`` is one of this module's settle messages,
    else ``None``. One-shot: the registration is consumed."""
    if message is None or not message.message_id:
        return None
    return _PENDING_SETTLES.pop(message.message_id, None)


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
        """Point a ``hitl_resume`` message at the context's parked task (in place)."""
        message = params.message
        if not _is_hitl_resume(message) or not message.context_id:
            return
        try:
            if message.task_id:
                named = await self._handler.task_store.get(message.task_id, call_context)
                if named is not None and named.status.state in _PAUSED:
                    return  # already continues a paused task — the spec's own path
            parked = await self.parked_tasks(message.context_id, call_context)
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
            message_id = f"settle-{uuid.uuid4()}"
            _PENDING_SETTLES[message_id] = keep_task_id
            message = Message(
                message_id=message_id,
                context_id=context_id,
                task_id=task.id,
                role=Role.ROLE_USER,
                parts=[Part(text=f"[superseded by task {keep_task_id}]")],
            )
            # `hidden`: a chat rebuilt from this task's history draws no bubble for it.
            message.metadata.update({"hidden": True})
            try:
                await self._send(SendMessageRequest(message=message), call_context)
                settled.append(task.id)
                log.info("[a2a] settled parked task %s — superseded by %s", task.id, keep_task_id)
            except Exception:  # noqa: BLE001 — one stuck sibling must not block the rest
                log.warning("[a2a] could not settle parked task %s", task.id, exc_info=True)
            finally:
                _PENDING_SETTLES.pop(message_id, None)
        return settled

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
