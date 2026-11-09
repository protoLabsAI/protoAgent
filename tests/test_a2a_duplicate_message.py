"""A re-sent client message does not start a second run (#4975).

The desktop console streams a turn through a Tauri command. When the page navigated away
mid-turn, WebKit cancelled that command's IPC request and Tauri's IPC script replayed the
same command over ``postMessage``, so the shell POSTed the identical
``SendStreamingMessage`` body again. a2a-sdk made a second task for the same
``messageId`` and the agent answered the one message twice (jobCoach, tasks 07aa9df5 and
001536f6).

Pinned through a real a2a-sdk ``DefaultRequestHandler`` wired like production
(``install_parked_task_routing``): a message whose ``(contextId, messageId)`` was already
accepted joins the first task instead of running the agent again, whether it arrives
after the first turn ended or while it is still running, streaming or unary.
"""

from __future__ import annotations

import asyncio

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import AgentSkill, Message, Part, Role, SendMessageRequest, Task, TaskState
from a2a.types.a2a_pb2 import ListTasksRequest

import protolabs_a2a as pa
from a2a_impl import hitl_routing, message_dedupe
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook

CTX = "chat-4975"
CALL = ServerCallContext()
_HANDLERS: list = []


@pytest.fixture(autouse=True)
def _hooks():
    set_terminal_hook(None)
    set_progress_hook(None)
    yield
    set_terminal_hook(None)
    set_progress_hook(None)
    hitl_routing._ROUTER[0] = None


@pytest.fixture(autouse=True)
async def _drain():
    yield
    for handler, router in _HANDLERS:
        await router.drain()
        tasks = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    _HANDLERS.clear()


def _handler(stream_fn):
    card = pa.build_agent_card(
        name="t",
        description="d",
        url="http://t/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
        bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    router = hitl_routing.install_parked_task_routing(handler)
    assert router is not None
    _HANDLERS.append((handler, router))
    return handler


def _msg(
    text: str = "we should do a twice daily check", *, mid: str = "web-1794215494862-wdhq0i"
) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(message_id=mid, context_id=CTX, role=Role.ROLE_USER, parts=[Part(text=text)])
    )


def _counting_stream(runs: list, gate: asyncio.Event | None = None):
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        runs.append(text)
        yield ("text", "Done. ")
        if gate is not None:
            await gate.wait()
        yield ("done", f"Done. answered run {len(runs)}")

    return stream


async def _collect(handler, req) -> list:
    return [event async for event in handler.on_message_send_stream(req, CALL)]


def _task_ids(events) -> set[str]:
    return {message_dedupe.event_task_id(e) for e in events} - {""}


async def _context_tasks(handler) -> list[Task]:
    page = await handler.task_store.list(ListTasksRequest(context_id=CTX, page_size=50), CALL)
    return list(page.tasks)


@pytest.mark.asyncio
async def test_resent_stream_after_the_turn_ended_replays_the_first_task():
    runs: list = []
    handler = _handler(_counting_stream(runs))

    first = await _collect(handler, _msg())
    again = await _collect(handler, _msg())

    assert len(runs) == 1, "the re-sent message ran the agent a second time"
    assert _task_ids(again) == _task_ids(first)
    assert len(await _context_tasks(handler)) == 1
    # The duplicate gets the first task's final state, so a listener settles from it.
    final = [e for e in again if isinstance(e, Task)][-1]
    assert final.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_resent_stream_while_the_first_is_running_follows_it():
    runs: list = []
    gate = asyncio.Event()
    handler = _handler(_counting_stream(runs, gate))

    first = asyncio.create_task(_collect(handler, _msg()))
    while not runs:  # the first turn has started
        await asyncio.sleep(0.01)
    again = asyncio.create_task(_collect(handler, _msg()))
    await asyncio.sleep(0.05)
    gate.set()
    first_events, again_events = await asyncio.wait_for(asyncio.gather(first, again), 5)

    assert len(runs) == 1, "the re-sent message ran the agent a second time"
    assert _task_ids(again_events) == _task_ids(first_events)
    assert len(await _context_tasks(handler)) == 1


@pytest.mark.asyncio
async def test_resent_unary_message_answers_with_the_first_task():
    runs: list = []
    handler = _handler(_counting_stream(runs))

    first = await handler.on_message_send(_msg(), CALL)
    again = await handler.on_message_send(_msg(), CALL)

    assert len(runs) == 1
    assert again.id == first.id
    assert again.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_a_new_message_id_still_runs():
    runs: list = []
    handler = _handler(_counting_stream(runs))

    first = await _collect(handler, _msg(mid="web-1"))
    second = await _collect(handler, _msg(mid="web-2"))

    assert len(runs) == 2
    assert _task_ids(first) != _task_ids(second)


@pytest.mark.asyncio
async def test_same_message_id_in_another_context_still_runs():
    runs: list = []
    handler = _handler(_counting_stream(runs))
    other = _msg()
    other.message.context_id = "chat-other"

    await _collect(handler, _msg())
    await _collect(handler, other)

    assert len(runs) == 2


@pytest.mark.asyncio
async def test_a_send_that_failed_before_any_task_lets_its_retry_run():
    ledger = message_dedupe.MessageLedger()
    message = _msg().message
    fresh, entry = ledger.accept(message)
    assert fresh
    waiter = asyncio.create_task(ledger.task_of(ledger.accept(message)[1]))
    ledger.drop(entry)  # the first send raised before naming a task
    assert await asyncio.wait_for(waiter, 1) == ""
    assert ledger.accept(message)[0], "a retry of a send that never made a task must run"


def test_ledger_forgets_old_messages():
    ledger = message_dedupe.MessageLedger(ttl_s=0.0)
    message = _msg().message
    _, entry = ledger.accept(message)
    ledger.bind(entry, "task-1")
    import time

    time.sleep(0.01)
    assert ledger.accept(message)[0]


def test_messages_without_ids_are_never_matched():
    ledger = message_dedupe.MessageLedger()
    bare = Message(message_id="", context_id=CTX, role=Role.ROLE_USER, parts=[Part(text="x")])
    assert ledger.accept(bare) == (True, None)
    assert ledger.accept(bare) == (True, None)
