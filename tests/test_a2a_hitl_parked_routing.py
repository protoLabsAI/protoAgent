"""One parked HITL task per context (#3930).

A2A models a HITL pause as an INTERRUPTED task: ``TASK_STATE_INPUT_REQUIRED`` is not
terminal, and "the client continues the interaction by sending a new message with the
same taskId and contextId" (spec §3.4.3). The console answered its forms WITHOUT the
task id (``hitl_resume`` metadata only), so the SDK started a fresh task and the one
that parked stayed input-required forever — exempt from the TTL sweep, rebuilt by a
fresh browser as a spinning ``ask_human`` card. A composer message held behind the form
re-parked on a second task the same way.

Pinned here, through a real a2a-sdk ``DefaultRequestHandler`` wired like production
(``harden_active_task_registry`` + ``install_parked_task_routing``):

- a ``hitl_resume`` answer with no task id continues the parked task (resume=True) and
  leaves no input-required task behind;
- an answer that names the parked task is unchanged;
- an answer naming a task that already ended is routed to the one still parked;
- a held message's re-park supersedes the older parked task (completed with a pointer);
- ``SubscribeToTask`` on a parked task holds the stream open after the snapshot (spec
  §3.1.6: the stream ends only at a TERMINAL state) and closes when the task settles.
"""

from __future__ import annotations

import asyncio

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import (
    AgentSkill,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskState,
)

import protolabs_a2a as pa
from a2a_impl import hitl_routing
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.registry import harden_active_task_registry

CTX = "chat-3930"
_HANDLERS: list = []


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    set_terminal_hook(None)
    set_progress_hook(None)
    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)
    yield
    set_terminal_hook(None)
    set_progress_hook(None)
    hitl_routing._ROUTER[0] = None


@pytest.fixture(autouse=True)
async def _drain():
    yield
    for handler, router in _HANDLERS:
        if router is not None:
            await router.drain()
        tasks = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    _HANDLERS.clear()


def _handler(stream_fn, *, routing: bool = True):
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
    assert harden_active_task_registry(handler)
    router = hitl_routing.install_parked_task_routing(handler) if routing else None
    if routing:
        assert router is not None
    _HANDLERS.append((handler, router))
    return handler, router


CALL = ServerCallContext()


def _msg(
    text: str, *, mid: str, task_id: str = "", hitl_resume: bool = False, ctx: str = CTX
) -> SendMessageRequest:
    message = Message(message_id=mid, context_id=ctx, role=Role.ROLE_USER, parts=[Part(text=text)])
    if task_id:
        message.task_id = task_id
    if hitl_resume:
        message.metadata.update({"hitl_resume": True})
    return SendMessageRequest(message=message)


async def _get(handler, task_id: str) -> Task:
    return await handler.on_get_task(GetTaskRequest(id=task_id), CALL)


async def _parked(handler, ctx: str = CTX) -> list[str]:
    return [t.id for t in await hitl_routing.ParkedTaskRouter(handler).parked_tasks(ctx, CALL)]


def _form_stream(calls: list):
    """Park every fresh turn on a form; a resumed one answers."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        calls.append({"text": text, "resume": resume})
        if resume:
            yield ("done", f"You like {text}.")
        else:
            yield ("input_required", {"question": "Favourite fruit?"})

    return stream


@pytest.mark.asyncio
async def test_resume_without_task_id_continues_the_parked_task():
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    parked = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    assert parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    answer = await handler.on_message_send(_msg("banana", mid="m2", hitl_resume=True), CALL)
    await router.drain()

    # The answer continued the SAME task — the spec's path — instead of minting a new one.
    assert answer.id == parked.id
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert calls[-1] == {"text": "banana", "resume": True}
    assert await _parked(handler) == []  # nothing orphaned in input-required


@pytest.mark.asyncio
async def test_resume_naming_the_parked_task_is_unchanged():
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    parked = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)

    answer = await handler.on_message_send(
        _msg("kiwi", mid="m2", task_id=parked.id, hitl_resume=True), CALL
    )
    await router.drain()

    assert answer.id == parked.id
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert calls[-1] == {"text": "kiwi", "resume": True}
    assert await _parked(handler) == []


@pytest.mark.asyncio
async def test_resume_without_task_id_and_no_routing_orphans_the_parked_task():
    """The pre-#3930 wiring, kept as the characterization of the bug: without the router
    the answer runs as a fresh task and the one that parked never leaves input-required."""
    calls: list = []
    handler, _ = _handler(_form_stream(calls), routing=False)
    parked = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    answer = await handler.on_message_send(_msg("banana", mid="m2", hitl_resume=True), CALL)

    assert answer.id != parked.id
    assert (await _get(handler, parked.id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED


@pytest.mark.asyncio
async def test_resume_naming_an_ended_task_is_routed_to_the_parked_one():
    """A console holding a stale task id (a task that already ended) must not have its
    answer refused by the SDK — it goes to the task that is actually waiting."""
    calls: list = []

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        calls.append({"text": text, "resume": resume})
        if resume:
            yield ("done", "ok")
        elif text == "plain":
            yield ("done", "plain answer")
        else:
            yield ("input_required", {"question": "go?"})

    handler, router = _handler(stream)
    ended = await handler.on_message_send(_msg("plain", mid="m0"), CALL)
    assert ended.status.state == TaskState.TASK_STATE_COMPLETED
    parked = await handler.on_message_send(_msg("ask", mid="m1"), CALL)

    answer = await handler.on_message_send(_msg("yes", mid="m2", task_id=ended.id, hitl_resume=True), CALL)
    await router.drain()

    assert answer.id == parked.id
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert await _parked(handler) == []


@pytest.mark.asyncio
async def test_held_message_reparking_supersedes_the_older_parked_task():
    """A composer message held behind the pending form re-parks on a NEW task (the hold,
    #1560). That task now owns the pause: the older one is completed with a pointer, and
    the form answer then continues the newer one — no input-required task survives."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    first = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    held = await handler.on_message_send(_msg("also, hurry", mid="m2"), CALL)
    assert held.id != first.id
    await router.drain()

    settled = await _get(handler, first.id)
    assert settled.status.state == TaskState.TASK_STATE_COMPLETED
    pointer = " ".join(p.text for p in settled.status.message.parts)
    assert held.id in pointer
    assert await _parked(handler) == [held.id]
    # The settle never ran the graph: only the two parking turns reached the stream.
    assert [c["text"] for c in calls] == ["ask me", "also, hurry"]

    answer = await handler.on_message_send(_msg("banana", mid="m3", hitl_resume=True), CALL)
    await router.drain()
    assert answer.id == held.id
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert await _parked(handler) == []


@pytest.mark.asyncio
async def test_settle_marker_cannot_be_forged_from_the_wire():
    """Only a message id this process registered a moment earlier settles a task — a
    client message is always a real answer."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    parked = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    answer = await handler.on_message_send(
        _msg("[superseded by task x]", mid="settle-forged", task_id=parked.id), CALL
    )
    await router.drain()
    assert calls[-1]["resume"] is True  # it ran as the answer, not as a settle
    assert answer.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_subscribe_to_a_parked_task_holds_open_until_the_answer_lands():
    """SubscribeToTask on an INPUT_REQUIRED task: the snapshot first, then the stream
    STAYS OPEN — spec-correct (§3.1.6 ends it only at a terminal state), which is why the
    console must treat a paused snapshot as settled rather than wait for close. When the
    answer continues the task, the subscriber receives it and the stream closes."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    parked = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)

    stream = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=parked.id), CALL)
    first = await asyncio.wait_for(anext(stream), 2)
    assert isinstance(first, Task) and first.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.3)
    assert not pending.done(), "the subscription on a paused task must stay open"

    await handler.on_message_send(_msg("banana", mid="m2", hitl_resume=True), CALL)
    events = [await asyncio.wait_for(pending, 2)]
    async for event in stream:
        events.append(event)
    await router.drain()
    states = [getattr(getattr(e, "status", None), "state", None) for e in events]
    assert TaskState.TASK_STATE_COMPLETED in states  # closed on the terminal frame


@pytest.mark.asyncio
async def test_settling_a_superseded_task_closes_its_subscription():
    """A console still subscribed to the older parked task must not hang once a newer task
    owns the pause: settling goes through the SDK, so its subscriber gets the terminal
    frame and the stream ends."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    first = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    stream = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=first.id), CALL)
    await asyncio.wait_for(anext(stream), 2)

    await handler.on_message_send(_msg("also, hurry", mid="m2"), CALL)
    await router.drain()

    async def rest():
        return [e async for e in stream]

    events = await asyncio.wait_for(rest(), 2)
    states = [getattr(getattr(e, "status", None), "state", None) for e in events]
    assert TaskState.TASK_STATE_COMPLETED in states


# ── review round (M1, minors) ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(5))
async def test_a_settle_never_reaches_the_graph_when_it_races_an_answer(attempt):
    """M1: P1 parks; a held message parks P2 (scheduling P1's settle) while an answer
    naming P1 is in flight. Whatever the interleaving, the internal settle text must
    never run as operator input — not as a resume, not as a fresh turn."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    p1 = await handler.on_message_send(_msg("first", mid="m1"), CALL)
    held = asyncio.ensure_future(handler.on_message_send(_msg("held", mid="m2"), CALL))
    answer = asyncio.ensure_future(
        handler.on_message_send(_msg("banana", mid="m3", task_id=p1.id, hitl_resume=True), CALL)
    )
    await asyncio.gather(held, answer, return_exceptions=True)
    await router.drain()
    await asyncio.sleep(0.2)  # let any queued request on either task run
    await router.drain()
    assert not any("superseded" in c["text"] for c in calls), calls
    # Every non-resume call is a message the operator sent — none reopened a task.
    assert sorted(c["text"] for c in calls if not c["resume"]) == ["first", "held"]


def test_settle_decision_only_applies_to_the_pause_it_was_scheduled_against():
    task = Task(id="t1", context_id=CTX)
    task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
    task.status.message.message_id = "pause-1"

    def settle_msg(mid: str) -> Message:
        m = Message(message_id=mid, task_id="t1", context_id=CTX, role=Role.ROLE_USER, parts=[Part(text="x")])
        m.metadata.update({hitl_routing.SETTLE_MARKER: True})
        return m

    # Forged: marked but never registered → a settle (never the graph), but a no-op.
    assert hitl_routing.settle_decision(settle_msg("forged"), task) == (True, None)
    # Registered against this pause → applies, and is consumed.
    hitl_routing._register("s1", "t2", "pause-1")
    assert hitl_routing.settle_decision(settle_msg("s1"), task) == (True, "t2")
    assert "s1" not in hitl_routing._PENDING_SETTLES
    # Registered against an EARLIER pause (the task was answered and re-paused) → no-op.
    hitl_routing._register("s2", "t2", "pause-0")
    assert hitl_routing.settle_decision(settle_msg("s2"), task) == (True, None)
    # The task is no longer paused → no-op.
    hitl_routing._register("s3", "t2", "pause-1")
    task.status.state = TaskState.TASK_STATE_COMPLETED
    assert hitl_routing.settle_decision(settle_msg("s3"), task) == (True, None)
    # An unmarked message is not a settle at all.
    plain = Message(message_id="s4", role=Role.ROLE_USER, parts=[Part(text="x")])
    assert hitl_routing.settle_decision(plain, task) == (False, None)


@pytest.mark.asyncio
async def test_routing_and_settling_stay_inside_their_context():
    """Two sessions each parked: an answer in B continues B's task, and a re-park in B
    never settles A's."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    a = await handler.on_message_send(_msg("ask A", mid="a1", ctx="ctx-a"), CALL)
    b = await handler.on_message_send(_msg("ask B", mid="b1", ctx="ctx-b"), CALL)
    b2 = await handler.on_message_send(_msg("held B", mid="b2", ctx="ctx-b"), CALL)
    await router.drain()
    assert await _parked(handler, "ctx-a") == [a.id]
    assert await _parked(handler, "ctx-b") == [b2.id]
    assert (await _get(handler, b.id)).status.state == TaskState.TASK_STATE_COMPLETED

    answer = await handler.on_message_send(_msg("kiwi", mid="b3", hitl_resume=True, ctx="ctx-b"), CALL)
    await router.drain()
    assert answer.id == b2.id
    assert await _parked(handler, "ctx-a") == [a.id]  # untouched


@pytest.mark.asyncio
async def test_a_message_naming_a_task_of_another_context_is_refused():
    from a2a.utils.errors import InvalidParamsError

    calls: list = []
    handler, router = _handler(_form_stream(calls))
    a = await handler.on_message_send(_msg("ask A", mid="a1", ctx="ctx-a"), CALL)
    with pytest.raises(InvalidParamsError):
        await handler.on_message_send(_msg("x", mid="b1", task_id=a.id, hitl_resume=True, ctx="ctx-b"), CALL)
    assert [c["text"] for c in calls] == ["ask A"]
    assert (await _get(handler, a.id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED


@pytest.mark.asyncio
async def test_a_double_submitted_answer_never_reopens_the_completed_task():
    """Two answers race onto the same parked task: the first continues it; the second
    must not re-run the completed task as a fresh turn."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    p1 = await handler.on_message_send(_msg("ask", mid="m1"), CALL)
    results = await asyncio.gather(
        handler.on_message_send(_msg("one", mid="m2", task_id=p1.id, hitl_resume=True), CALL),
        handler.on_message_send(_msg("two", mid="m3", task_id=p1.id, hitl_resume=True), CALL),
        return_exceptions=True,
    )
    await router.drain()
    await asyncio.sleep(0.2)
    assert (await _get(handler, p1.id)).status.state == TaskState.TASK_STATE_COMPLETED
    # Exactly one resume ran on p1, and no call reopened it as a fresh turn.
    assert [c for c in calls if not c["resume"]] == [{"text": "ask", "resume": False}], (calls, results)
