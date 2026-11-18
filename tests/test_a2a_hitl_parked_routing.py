"""One parked HITL task per context (#3930).

A2A models a HITL pause as an INTERRUPTED task: ``TASK_STATE_INPUT_REQUIRED`` is not
terminal, and "the client continues the interaction by sending a new message with the
same taskId and contextId" (spec §3.4.3). The console answered its forms WITHOUT the
task id (``hitl_resume`` metadata only), so the SDK started a fresh task and the one
that parked stayed input-required forever — exempt from the TTL sweep, rebuilt by a
fresh browser as a spinning ``ask_human`` card. A composer message held behind the form
re-parked on a second task the same way.

Pinned here, through a real a2a-sdk ``DefaultRequestHandler`` wired like production
(``install_parked_task_routing``):

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
from a2a_impl import auth, hitl_routing
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook

CTX = "chat-3930"
_HANDLERS: list = []


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    from infra import media

    monkeypatch.setattr(media, "_PRIVATE_READERS", {})
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
        if router is not None:
            await router.drain()
        tasks = set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    _HANDLERS.clear()


def _handler(stream_fn, *, routing: bool = True, task_store=None, push_config_store=None, push_sender=None,
             requester_projection_factory=None, structured_finalizer=None, stall_timeout_provider=None):
    card = pa.build_agent_card(
        name="t",
        description="d",
        url="http://t/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
        bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn, requester_projection_factory=requester_projection_factory,
                                          structured_finalizer=structured_finalizer,
                                          stall_timeout_provider=stall_timeout_provider),
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
        push_config_store=push_config_store or InMemoryPushNotificationConfigStore(),
        push_sender=push_sender,
    )
    router = hitl_routing.install_parked_task_routing(handler) if routing else None
    if routing:
        assert router is not None
    _HANDLERS.append((handler, router))
    return handler, router


CALL = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})


def _msg(text: str, *, mid: str, task_id: str = "", hitl_resume: bool = False, ctx: str = CTX) -> SendMessageRequest:
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

    answer = await handler.on_message_send(_msg("kiwi", mid="m2", task_id=parked.id, hitl_resume=True), CALL)
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
    answer = await handler.on_message_send(_msg("[superseded by task x]", mid="settle-forged", task_id=parked.id), CALL)
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
    must not re-run the COMPLETED task as a fresh turn. It is refused by the ended task
    and re-routed — with nothing parked, to a task of its own."""
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
    # Exactly one answer continued p1; the other ran on a NEW task — p1 was never reopened.
    assert sum(1 for c in calls if c["resume"]) == 1, calls
    assert all(isinstance(r, Task) for r in results), results
    assert sorted(r.id == p1.id for r in results) == [False, True], results
    p1_task = await _get(handler, p1.id)
    assert sum(1 for m in p1_task.history if m.role == Role.ROLE_USER) == 2  # "ask" + one answer


# ── review round 2: an answer is never lost to a settle ───────────────────────


@pytest.mark.asyncio
async def test_a_settle_never_completes_a_task_an_answer_is_headed_to():
    """Deterministic reproducer: the answer is routed to P1, then stalls before the SDK
    queues it (its second task_store.get). Meanwhile a held message re-parks as P2 and
    P1's settle is scheduled. The settle must wait for the answer: the answer continues
    P1, reaches the agent, and nothing is left parked."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    p1 = await handler.on_message_send(_msg("ask", mid="m1"), CALL)

    store = handler.task_store
    real_get = store.get
    stall, release = asyncio.Event(), asyncio.Event()
    gets = {"n": 0}

    async def get(task_id, context):
        if task_id == p1.id and stall.is_set():
            gets["n"] += 1
            if gets["n"] == 2:  # the SDK's own lookup, after route() already chose P1
                await release.wait()
        return await real_get(task_id, context)

    store.get = get
    stall.set()
    answer = asyncio.ensure_future(
        handler.on_message_send(_msg("banana", mid="m2", task_id=p1.id, hitl_resume=True), CALL)
    )
    for _ in range(50):
        if gets["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    assert gets["n"] == 2, "the answer never reached the SDK's lookup"

    held = await handler.on_message_send(_msg("held", mid="m3"), CALL)
    await router.drain()
    assert (await _get(handler, p1.id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED  # not settled

    release.set()
    result = await asyncio.wait_for(answer, 5)
    await router.drain()
    await asyncio.sleep(0.05)
    await router.drain()

    assert result.id == p1.id and result.status.state == TaskState.TASK_STATE_COMPLETED
    assert {"text": "banana", "resume": True} in calls
    assert not any("superseded" in c["text"] for c in calls)
    # P1's answer consumed the pause, so the task that re-parked on it is settled too.
    assert (await _get(handler, held.id)).status.state == TaskState.TASK_STATE_COMPLETED
    assert await _parked(handler) == []


@pytest.mark.asyncio
async def test_a_message_to_an_ended_task_is_not_silently_accepted():
    """A plain message naming a task that already ended is refused, not swallowed behind
    a 200; a HITL answer naming one is re-routed to the context's current pause.

    The refusal is UnsupportedOperationError (JSON-RPC -32004): a2a-sdk >= 1.2 rejects
    terminal-task operations with it, per the A2A spec (a2a-python#1268, #3950)."""
    from a2a.utils.errors import UnsupportedOperationError

    calls: list = []
    handler, router = _handler(_form_stream(calls))
    p1 = await handler.on_message_send(_msg("ask", mid="m1"), CALL)
    done = await handler.on_message_send(_msg("kiwi", mid="m2", task_id=p1.id, hitl_resume=True), CALL)
    assert done.status.state == TaskState.TASK_STATE_COMPLETED
    await router.drain()

    with pytest.raises(UnsupportedOperationError):
        await handler.on_message_send(_msg("hello?", mid="m3", task_id=p1.id), CALL)
    assert "hello?" not in [c["text"] for c in calls]

    p2 = await handler.on_message_send(_msg("ask again", mid="m4"), CALL)
    answer = await handler.on_message_send(_msg("mango", mid="m5", task_id=p1.id, hitl_resume=True), CALL)
    await router.drain()
    assert answer.id == p2.id and answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert {"text": "mango", "resume": True} in calls


@pytest.mark.asyncio
async def test_an_answer_whose_task_ends_after_routing_is_rerouted(monkeypatch):
    """The named task ends between the routing check and the SDK (the race the retry in
    install_parked_task_routing exists for): the SDK refuses the ended task and the answer
    is re-routed to the context's current pause — whichever error the refusal carries
    (UnsupportedOperationError since a2a-sdk 1.2, #3950; InvalidParamsError before)."""
    calls: list = []
    handler, router = _handler(_form_stream(calls))
    p1 = await handler.on_message_send(_msg("ask", mid="m1"), CALL)
    await handler.on_message_send(_msg("kiwi", mid="m2", task_id=p1.id, hitl_resume=True), CALL)
    await router.drain()
    p2 = await handler.on_message_send(_msg("ask again", mid="m3"), CALL)

    real_route = router.route
    routed: list = []

    async def _racy_route(params, context):
        routed.append(params.message.task_id)
        if len(routed) > 1:  # the first check saw p1 still paused; the retry routes for real
            await real_route(params, context)

    monkeypatch.setattr(router, "route", _racy_route)
    answer = await handler.on_message_send(_msg("mango", mid="m4", task_id=p1.id, hitl_resume=True), CALL)
    await router.drain()
    assert routed[0] == p1.id and len(routed) == 2
    assert answer.id == p2.id and answer.status.state == TaskState.TASK_STATE_COMPLETED
    assert {"text": "mango", "resume": True} in calls


@pytest.mark.asyncio
async def test_a_federation_caller_reaches_only_its_own_tasks_and_contexts(tmp_path):
    """Tasks are owned by the caller's tier: a federation caller can't read, list, cancel,
    hook, subscribe to or continue the operator's; the operator still sees both."""
    from a2a.types import CancelTaskRequest, ListTasksRequest, TaskPushNotificationConfig
    from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

    from a2a_impl import stores

    from starlette.requests import Request

    stamped = stores.tier_context_builder().build(
        Request({"type": "http", "headers": [], "state": {"trust_tier": "federation"}})
    )
    assert stores.resolve_task_owner(stamped) == "federation"
    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "t.db")), owner_resolver=stores.resolve_task_owner
    )
    handler, router = _handler(_form_stream([]), task_store=store)
    fed = ServerCallContext(state={"trust_tier": "federation"})
    mine = await handler.on_message_send(_msg("ask me", mid="m1"), CALL)
    for call in (
        handler.on_get_task(GetTaskRequest(id=mine.id), fed),
        handler.on_cancel_task(CancelTaskRequest(id=mine.id), fed),
        handler.on_create_task_push_notification_config(
            TaskPushNotificationConfig(task_id=mine.id, url="https://hook.example/x"), fed
        ),
        anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=mine.id), fed)),
    ):
        with pytest.raises(TaskNotFoundError):
            await call
    assert not (await handler.on_list_tasks(ListTasksRequest(context_id=CTX), fed)).tasks
    with pytest.raises(InvalidParamsError):  # nor answer its pause in that context
        await handler.on_message_send(_msg("banana", mid="m2", hitl_resume=True), fed)

    theirs = await handler.on_message_send(_msg("ask me", mid="m3", ctx="fed-ctx"), fed)
    assert (await _get(handler, theirs.id)).id == theirs.id
    assert [t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), CALL)).tasks] == [
        theirs.id
    ]
    assert (await _get(handler, mine.id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    await router.drain()
    await store.engine.dispose()


def _answer_stream():
    """Every turn answers at once (no park), so turns in one context stay independent."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", f"ok: {text}")

    return stream


@pytest.mark.asyncio
async def test_an_operator_steer_into_a_peer_thread_keeps_the_thread_the_peers(tmp_path):
    """#5379: an idle steer runs as an operator-owned task inside the federation peer's
    context (server/chat.py _start_steer_turn). The peer must still be able to continue its
    own thread, without seeing the operator's turn. A thread the operator started stays
    closed to the peer."""
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

    from a2a_impl import stores

    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "t.db")), owner_resolver=stores.resolve_task_owner
    )
    handler, router = _handler(_answer_stream(), task_store=store)
    fed = ServerCallContext(state={"trust_tier": "federation"})

    first = await handler.on_message_send(_msg("please review", mid="p1", ctx="fed-ctx"), fed)
    steer = await handler.on_message_send(_msg("check their assumptions", mid="s1", ctx="fed-ctx"), CALL)
    assert steer.id != first.id
    # The peer continues its own conversation after the steer.
    again = await handler.on_message_send(_msg("any update?", mid="p2", ctx="fed-ctx"), fed)
    assert again.context_id == "fed-ctx"
    # The operator's turn stays out of the peer's reads.
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=steer.id), fed)
    listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), fed)).tasks}
    assert steer.id not in listed and first.id in listed

    # A thread the operator started is still not the peer's to continue.
    await handler.on_message_send(_msg("my own chat", mid="o1", ctx="chat-own"), CALL)
    with pytest.raises(InvalidParamsError):
        await handler.on_message_send(_msg("let me in", mid="p3", ctx="chat-own"), fed)
    await router.drain()
    await store.engine.dispose()


# ── P1b.2 private intake: the output gate can no longer hide private input (Refs #5401) ──
# Phase 0 proved an output gate alone could NOT keep a private steer out of a peer-readable
# task history (the leak assertions). With the P1b.1 partition the input never travels as
# text — the loopback carries only an opaque reference — so these assert PRIVACY instead: at
# intake and at every peer read (poll, push, stream, reopened history), for a fresh (idle)
# private steer and for a parked resume, with console recovery of the input and the reply.

_SECRET = "SECRET-PRIVATE-STEER-xyz"


def test_private_input_prefix_mirrors_the_partition_module():
    from a2a_impl import private_intake

    assert hitl_routing._PRIVATE_INPUT_PREFIX == private_intake.PRIVATE_INPUT_PREFIX


def test_a_private_marker_with_a_smuggled_prompt_is_refused():
    from a2a.utils.errors import InvalidParamsError

    smuggled = Message(message_id="x", context_id=CTX, role=Role.ROLE_USER,
                       parts=[Part(text="private-input:abc"), Part(text="SECRET smuggled prompt")])
    smuggled.metadata.update({hitl_routing.PRIVATE_INPUT_MARKER: True})
    with pytest.raises(InvalidParamsError):
        hitl_routing.enforce_private_input(smuggled)
    # A clean single-reference private message, and any unmarked ordinary message, pass.
    clean = Message(message_id="y", context_id=CTX, role=Role.ROLE_USER, parts=[Part(text="private-input:abc")])
    clean.metadata.update({hitl_routing.PRIVATE_INPUT_MARKER: True})
    hitl_routing.enforce_private_input(clean)
    hitl_routing.enforce_private_input(_msg("hello", mid="z").message)


def _private_steer_stream(seen: list):
    """Parks a fresh 'ask me something' turn; otherwise answers. The private input never
    reaches it — the runner only ever sees the opaque reference."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        seen.append({"text": text, "resume": resume})
        if not resume and text == "ask me something":
            yield ("input_required", {"question": "What should I prioritise?"})
        else:
            yield ("done", "Noted; proceeding.")

    return stream


def _ref_msg(ref: str, *, mid: str, hitl_resume: bool = False, ctx: str = CTX) -> SendMessageRequest:
    m = Message(message_id=mid, context_id=ctx, role=Role.ROLE_USER, parts=[Part(text=ref)])
    if hitl_resume:
        m.metadata.update({"hitl_resume": True})
    m.metadata.update({hitl_routing.PRIVATE_INPUT_MARKER: True})
    return SendMessageRequest(message=m)


async def _assert_partition_is_the_only_home(store):
    from sqlalchemy import text as _sql

    async with store.engine.connect() as conn:
        rows = (await conn.execute(
            _sql("SELECT history, artifacts, metadata, status, private_payload FROM tasks")
        )).all()
    # The private text lives ONLY in a private_payload column, never in any peer-readable one.
    assert any(_SECRET in (r.private_payload or "") for r in rows)
    assert all(_SECRET not in str((r.history, r.artifacts, r.metadata, r.status)) for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("parked", [False, True])
async def test_phase0_output_gate_cannot_hide_private_input_history(tmp_path, delegation_env, parked):
    from a2a.server.context import ServerCallContext
    from a2a.types import TaskPushNotificationConfig
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    ctx_id = "chat-private"
    engine = stores.make_sqlite_engine(str(tmp_path / "p.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    peer = ServerCallContext(state={"principal_kind": "federation", "trust_tier": "federation"})
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    # The steer is recorded into the partition BEFORE any turn; the loopback would carry only
    # this opaque reference bound to the delegation.
    ref = await private_intake.record_private_steer(store, session_id=ctx_id, text=_SECRET, msg_id="m0", context=op)

    async def _stream_send(req):
        return [e async for e in handler.on_message_send_stream(req, CALL)]

    # stream: the live SSE frames a console watches as the turn runs — for a fresh (idle)
    # private steer and for a parked resume.
    if parked:
        parkd = await handler.on_message_send(_msg("ask me something", mid="a1", ctx=ctx_id), CALL)
        assert parkd.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        turn_id = parkd.id
        resume = _ref_msg(ref, mid="o1", hitl_resume=True, ctx=ctx_id)
        # An explicit guarded handle can continue this ordinary task; the private
        # fallback now selects only tasks created in the human-private partition.
        resume.message.task_id = parkd.id
        frames = await asyncio.wait_for(_stream_send(resume), 5)
    else:
        frames = await asyncio.wait_for(_stream_send(_ref_msg(ref, mid="o1", ctx=ctx_id)), 5)
        turn_id = next(getattr(e, "id", "") or getattr(e, "task_id", "") for e in frames
                       if getattr(e, "id", "") or getattr(e, "task_id", ""))
    await router.drain()
    assert frames and all(_SECRET not in str(MessageToDict(f)) for f in frames)
    if parked:
        assert turn_id == parkd.id  # the guarded resume continued the ORIGINAL checkpoint/task
    # The runner only ever saw the opaque reference, never the private text.
    assert any(c["text"] == ref for c in seen) and all(_SECRET not in str(c["text"]) for c in seen)

    await _assert_partition_is_the_only_home(store)  # intake
    got = await handler.on_get_task(GetTaskRequest(id=turn_id), CALL)
    assert got.status.state == TaskState.TASK_STATE_COMPLETED
    assert _SECRET not in str(MessageToDict(got))  # poll
    # push: a registered webhook ships that same Task projection — nothing private to deliver.
    await handler.on_create_task_push_notification_config(
        TaskPushNotificationConfig(task_id=turn_id, url="https://8.8.8.8/hook"), CALL
    )
    assert _SECRET not in str(MessageToDict(await handler.on_get_task(GetTaskRequest(id=turn_id), CALL)))

    # reopened history: a fresh store on the same db (a new browser rebuilding the chat) still
    # never reads the private text; the operator recovers the input and the folded-in reply.
    await engine.dispose()
    engine2 = stores.make_sqlite_engine(str(tmp_path / "p.db"))
    store2 = stores.ReasoningCoalescingTaskStore(engine2, owner_resolver=stores.resolve_task_owner)
    await store2.initialize()
    try:
        assert _SECRET not in str(MessageToDict(await store2.get(turn_id, op)))
        if auth.is_human_operator(peer.state.get("principal_kind")):
            assert _SECRET in str(await store2.history_records(ctx_id, peer))
        else:
            assert _SECRET not in str(await store2.history_records(ctx_id, peer))
        assert await store2.get(private_intake.ref_task_id(ref), peer) is None  # the shell is invisible to a peer
        assert await private_intake.record_private_reply(store2, ref, "handled privately", op)
        rec = await store2.get_record(private_intake.ref_task_id(ref), op)
        assert rec.private_payload["input"] == _SECRET and rec.private_payload["reply"] == "handled privately"
    finally:
        await engine2.dispose()


@pytest.mark.asyncio
async def test_a_private_steer_into_a_peer_thread_hides_the_input_and_keeps_the_thread_the_peers(tmp_path, delegation_env):
    """#5380: a private steer into a federation peer's context runs as the operator's own
    task carrying only the reference; the peer keeps its thread, a later peer follow-up never
    sees the private input, and the private-only shell is invisible to the peer."""
    from a2a.server.context import ServerCallContext
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import TaskNotFoundError
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    engine = stores.make_sqlite_engine(str(tmp_path / "peer.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    fed = ServerCallContext(state={"trust_tier": "federation"})
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    first = await handler.on_message_send(_msg("please look", mid="p1", ctx="fed-ctx"), fed)
    ref = await private_intake.record_private_steer(store, session_id="fed-ctx", text=_SECRET, msg_id="m0", context=op)
    steer = await handler.on_message_send(_ref_msg(ref, mid="o1", ctx="fed-ctx"), CALL)
    assert steer.id != first.id  # the operator's private turn is its OWN task in the peer's context
    await router.drain()

    # A later peer follow-up continues the peer's own thread and never sees the private input.
    again = await handler.on_message_send(_msg("any update?", mid="p2", ctx="fed-ctx"), fed)
    assert again.context_id == "fed-ctx"
    listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), fed)).tasks}
    if auth.is_human_operator(fed.state.get("principal_kind")):
        assert steer.id in listed and first.id in listed
        assert (await handler.on_get_task(GetTaskRequest(id=steer.id), fed)).id == steer.id
        assert (await store.get_record(private_intake.ref_task_id(ref), fed)).private_payload["input"] == _SECRET
    else:
        assert steer.id not in listed and first.id in listed
        with pytest.raises(TaskNotFoundError):
            await handler.on_get_task(GetTaskRequest(id=steer.id), fed)
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=private_intake.ref_task_id(ref)), fed)
    peer_tasks = (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), fed)).tasks
    assert _SECRET not in str([MessageToDict(t) for t in peer_tasks])
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["input"] == _SECRET
    assert all(_SECRET not in str(c["text"]) for c in seen)
    await router.drain()
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_private_resume_never_lands_on_a_peer_owned_parked_task(tmp_path, delegation_env):
    """#5380 / ADR 0131 P1b.2: when a PEER owns the only parked task in a context and the
    operator has none, a private steer taking the guarded resume path must NOT be re-pointed
    onto the peer's parked task — the operator→federation read fallback would otherwise do
    that and land the private reply on a peer-readable task. It runs as the operator's own
    task instead; the peer's parked task and its requires_input message keep no private input
    or reply, and an operator turn ending in the peer's context never settles the peer's pause."""
    from a2a.server.context import ServerCallContext
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import TaskNotFoundError
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    engine = stores.make_sqlite_engine(str(tmp_path / "peer-parked.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    fed = ServerCallContext(state={"trust_tier": "federation"})
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    # The PEER parks a HITL turn in its own context; the operator owns no parked task here, but
    # the operator's read of the context DOES see the peer's via the federation fallback.
    peer_parked = await handler.on_message_send(_msg("ask me something", mid="p1", ctx="fed-ctx"), fed)
    assert peer_parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert await _parked(handler, "fed-ctx") == [peer_parked.id]
    owner_scoped = await hitl_routing.ParkedTaskRouter(handler).parked_tasks("fed-ctx", CALL, owner_scoped=True)
    assert owner_scoped == []  # the operator owns no parked task — the fallback is skipped

    ref = await private_intake.record_private_steer(store, session_id="fed-ctx", text=_SECRET, msg_id="m0", context=op)
    steer = await handler.on_message_send(_ref_msg(ref, mid="o1", hitl_resume=True, ctx="fed-ctx"), CALL)
    await router.drain()

    # The private resume ran as the operator's OWN fresh task — never the peer's parked one.
    assert steer.id != peer_parked.id
    assert steer.status.state == TaskState.TASK_STATE_COMPLETED

    # The peer's parked task is untouched: still input-required (never settled by the operator
    # turn), its requires_input message / history hold no private input or reply, and the
    # private reference is invisible to the peer.
    peer_view = await handler.on_get_task(GetTaskRequest(id=peer_parked.id), fed)
    assert peer_view.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    dumped = str(MessageToDict(peer_view))
    assert _SECRET not in dumped and ref not in dumped
    peer_tasks = (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), fed)).tasks
    if auth.is_human_operator(fed.state.get("principal_kind")):
        assert steer.id in {t.id for t in peer_tasks}
        assert (await handler.on_get_task(GetTaskRequest(id=steer.id), fed)).id == steer.id
        assert (await store.get_record(private_intake.ref_task_id(ref), fed)).private_payload["input"] == _SECRET
        assert all(_SECRET not in str(MessageToDict(t)) for t in peer_tasks)
    else:
        assert steer.id not in {t.id for t in peer_tasks}
        assert all(_SECRET not in str(MessageToDict(t)) and ref not in str(MessageToDict(t)) for t in peer_tasks)
        with pytest.raises(TaskNotFoundError):  # the operator's private turn stays out of the peer's reads
            await handler.on_get_task(GetTaskRequest(id=steer.id), fed)

    # The runner only ever saw the opaque reference, never the private text; the operator still
    # recovers the input from the partition.
    assert any(c["text"] == ref for c in seen) and all(_SECRET not in str(c["text"]) for c in seen)
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["input"] == _SECRET
    await router.drain()
    await engine.dispose()


@pytest.mark.asyncio
async def test_phase0_preexisting_handles(tmp_path):
    """Model B isolates retained handles, including peers in the same owner tier."""
    import httpx
    from a2a.types import TaskPushNotificationConfig
    from a2a.utils.errors import TaskNotFoundError
    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection

    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "delivery.db")), owner_resolver=stores.resolve_task_owner,
    )
    await store.initialize()
    peer = ServerCallContext(state={"requester_principal": "device:one", "principal_kind": "paired-agent"})
    other = ServerCallContext(state={"requester_principal": "device:two", "principal_kind": "paired-agent"})
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    secret, reply, result = "STEERING-CANARY", "PRIVATE-REPLY-CANARY", "The requested result."
    ref = await private_intake.record_private_steer(store, session_id=CTX, text=secret, msg_id="steer", context=op)
    ready, proceed = asyncio.Event(), asyncio.Event()
    projections, progress, pushes, outcomes = [], [], [], []
    config = TaskPushNotificationConfig(id="original", url="https://8.8.8.8/hook")

    async def factory(context, queue):
        config.task_id = context.task_id
        await handler._push_config_store.set_info(context.task_id, config, peer)
        projection = await RequesterProjection.accept(
            store, context, queue, agent_id="agent:owner", operator=op, push_configs=[config],
        )
        # Even a config inserted outside the handler cannot widen the accepted recipient.
        await handler._push_config_store.set_info(context.task_id, TaskPushNotificationConfig(
            id="second-peer", task_id=context.task_id, url="https://8.8.4.4/other",
        ), other)
        projections.append(projection)
        return projection

    async def stream(*args, **kwargs):
        ready.set()
        await proceed.wait()
        projection = projections[0]
        await projection.begin_private(ref)
        yield "steer_consumed", {"items": [{"text": secret}]}
        yield "text", reply
        # A private turn still spends: its usage/tool telemetry must reach the executor's
        # accounting (the terminal TurnOutcome), even though none of this content publishes.
        yield "tool_start", {"id": "private-call", "name": "private"}
        yield "usage", {"input_tokens": 11, "output_tokens": 7, "cost_usd": 0.25, "model": "m-private"}
        yield "tool_end", {"name": "private", "output": secret, "duration_ms": 9}
        yield "text", {"audience": "requester", "text": reply}  # wrong labels never publish
        yield "deliver_to_requester", {"text": reply, "audience": "requester"}  # raw B event
        delivery_id = await projection.prepare_result(result)
        assert await projection.prepare_result(result) == delivery_id
        for key in ("principal", "context_id", "task_id", "agent_id", "message_id"):
            forged = {**projection.binding, key: "forged"}
            attacker = RequesterProjection(store, projection.context, projection.queue, forged, op)
            attacker.private_ref = ref
            assert not await attacker.deliver_to_requester(delivery_id)
        yield "deliver_to_requester", delivery_id
        yield "deliver_to_requester", delivery_id  # retry: one artifact frame
        yield "done", reply

    set_progress_hook(lambda *args: progress.append(args))
    set_terminal_hook(outcomes.append)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (pushes.append(request.content.decode()), httpx.Response(200))[1],
    ))
    push_store = InMemoryPushNotificationConfigStore()
    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    handler, router = _handler(stream, task_store=store, push_config_store=push_store,
                               push_sender=sender, requester_projection_factory=factory)

    async def collect(iterator, target):
        async for event in iterator:
            snapshot = type(event)()
            snapshot.CopyFrom(event)
            target.append(snapshot)

    frames, subscriptions = [], []
    running = asyncio.create_task(collect(handler.on_message_send_stream(_msg("request", mid="request"), peer), frames))
    await asyncio.wait_for(ready.wait(), 5)
    task_id = projections[0].binding["task_id"]
    subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), peer)
    initial = await asyncio.wait_for(anext(subscribed), 5)
    snapshot = type(initial)()
    snapshot.CopyFrom(initial)
    subscriptions.append(snapshot)
    watching = asyncio.create_task(collect(subscribed, subscriptions))
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=task_id), other)
    with pytest.raises(TaskNotFoundError):
        await anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), other))
    proceed.set()
    await asyncio.wait_for(asyncio.gather(running, watching), 10)
    await router.drain()
    got = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
    assert got.context_id == CTX
    assert [p.text for a in got.artifacts for p in a.parts] == [result]
    assert sum(result in str(f) for f in frames) == 1
    assert sum(result in str(f) for f in subscriptions) == 1
    assert all(canary not in str(surface) for canary in (secret, reply)
               for surface in (frames, subscriptions, got, pushes, progress, outcomes))
    assert sum(result in body for body in pushes) == 1
    # The private turn is isolated, not untelemetered: its spend reaches the terminal
    # row with the result/canary text stripped off, so nothing private rides it either.
    outcome = next(o for o in outcomes if o.state == "completed")
    assert outcome.text == "" and outcome.error == ""
    assert outcome.llm_calls == 1 and outcome.tool_calls == 1
    assert outcome.usage["input_tokens"] == 11 and outcome.usage["output_tokens"] == 7
    assert outcome.cost_usd == 0.25 and outcome.tool_durations.get("private") == [9]
    assert outcome.models == ["m-private"]
    # Dispatch through the production sender, including URL validation, then replace it.
    artifact = next(f for f in frames if result in str(f))
    pushes.clear()
    assert await sender._dispatch_notification(artifact, config, task_id)
    assert len(pushes) == 1 and result in pushes[0]
    replaced = TaskPushNotificationConfig(id="original", task_id=task_id, url="https://8.8.4.4/stolen")
    await push_store.set_info(task_id, replaced, peer)
    await sender.send_notification(task_id, artifact)
    assert len(pushes) == 1
    # Binding forgeries and unbound IDs cannot create a new release or redirect this one.
    projection = projections[0]
    for key in ("principal", "context_id", "task_id", "agent_id", "message_id"):
        forged = {**projection.binding, key: "forged"}
        attacker = RequesterProjection(store, projection.context, projection.queue, forged, op)
        attacker.private_ref = ref
        assert not await attacker.deliver_to_requester("unbound")
        with pytest.raises(PermissionError):
            await store.bind_requester(got, forged, peer)
    assert await store.get(task_id, other) is None
    binding, delivery = await store.delivery_data(task_id)
    assert delivery["state"] == "delivered" and binding == projection.binding
    await client.aclose()
    await store.engine.dispose()
    reopened = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "delivery.db")), owner_resolver=stores.resolve_task_owner,
    )
    try:
        history = await reopened.history_records(CTX, peer)
        if auth.is_human_operator(peer.state.get("principal_kind")):
            assert all(c in str(history) for c in (result, secret, reply))
            other_history = await reopened.history_records(CTX, other)
            assert result not in str(other_history)
            assert all(c in str(other_history) for c in (secret, reply))
        else:
            assert result in str(history) and all(c not in str(history) for c in (secret, reply))
            assert await reopened.history_records(CTX, other) == []
        assert (await reopened.delivery_data(task_id))[1] == delivery
        recovered = RequesterProjection(reopened, projection.context, projection.queue, binding, op)
        await recovered.begin_private(ref)
        assert await recovered.prepare_result(result) == delivery["id"]
        assert not await recovered.deliver_to_requester(delivery["id"])
        private = await reopened.get_record(private_intake.ref_task_id(ref), op)
        assert secret in str(private.private_payload) and reply in str(private.private_payload)
    finally:
        await reopened.engine.dispose()


@pytest.mark.asyncio
async def test_private_hitl_preserves_content_free_pause_and_resume_progress(tmp_path):
    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection

    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "private-hitl.db")), owner_resolver=stores.resolve_task_owner,
    )
    await store.initialize()
    peer = ServerCallContext(state={"requester_principal": "device:one", "principal_kind": "paired-agent"})
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    secret = "PRIVATE-HITL-PROMPT-CANARY"
    ref = await private_intake.record_private_steer(store, session_id=CTX, text=secret, msg_id="s1", context=op)
    frames = []

    async def factory(context, queue):
        projection = await RequesterProjection.accept(store, context, queue, agent_id="owner", operator=op)
        await projection.begin_private(ref)
        return projection

    async def stream(*args, resume=False, **kwargs):
        yield "text", secret
        if resume:
            yield "done", secret
        else:
            yield "input_required", {"question": secret}

    set_progress_hook(lambda *args: frames.append(args))
    handler, _ = _handler(stream, task_store=store, requester_projection_factory=factory)
    request = _msg(secret, mid="m1")
    request.message.metadata.update({"origin": "a2a", "caller": secret})
    parked = await handler.on_message_send(request, peer)
    assert parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert [frame for _, _, frame in frames] == [
        {"phase": "turn_started", "origin": "a2a", "resumed": False},
        {"phase": "input_required"},
        {"phase": "turn_ended", "origin": "a2a", "state": "input_required"},
    ]

    request = _msg(secret, mid="m2", task_id=parked.id)
    request.message.metadata.update({"origin": "a2a", "caller": secret})
    completed = await handler.on_message_send(request, peer)
    assert completed.status.state == TaskState.TASK_STATE_COMPLETED
    assert [frame for _, _, frame in frames[3:]] == [
        {"phase": "turn_started", "origin": "a2a", "resumed": True},
        {"phase": "turn_ended", "origin": "a2a", "state": "completed"},
    ]
    assert all(context_id == CTX and task_id == parked.id for context_id, task_id, _ in frames)
    assert secret not in str(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "digest", "claimed", "missing-private", "missing-delivery"])
async def test_delivery_recovery_fails_closed(tmp_path, fault):
    from types import SimpleNamespace
    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection

    path = str(tmp_path / "recovery.db")
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path), owner_resolver=stores.resolve_task_owner)
    peer = ServerCallContext(state={"requester_principal": "device:one"})
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    context = SimpleNamespace(call_context=peer, current_task=None, task_id="request", context_id=CTX,
                              message=_msg("request", mid="m1").message)
    events = []

    class Queue:
        async def enqueue_event(self, event):
            events.append(event)

    projection = await RequesterProjection.accept(store, context, Queue(), agent_id="owner", operator=op)
    ref = await private_intake.record_private_steer(store, session_id=CTX, text="private", msg_id="s1", context=op)
    await projection.begin_private(ref)
    identity = await projection.prepare_result("result")
    binding, record = await store.delivery_data("request")
    if fault in ("digest", "claimed"):
        mutated = {**record, **({"payload": "tampered"} if fault == "digest" else {"state": "dispatching"})}
        assert await store.cas_delivery("request", binding, record, mutated)
    elif fault == "missing-private":
        await store.delete(private_intake.ref_task_id(ref), op)
    elif fault == "missing-delivery":
        await store.delete("request", peer)
    await store.engine.dispose()
    reopened = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path), owner_resolver=stores.resolve_task_owner)
    try:
        recovered = RequesterProjection(reopened, context, Queue(), binding, op)
        recovered.private_ref = ref
        assert await recovered.deliver_to_requester(identity) is (fault == "none")
        assert len(events) == (1 if fault == "none" else 0)
        assert not await recovered.deliver_to_requester(identity)
    finally:
        await reopened.engine.dispose()


@pytest.mark.asyncio
async def test_private_final_envelope_keeps_selected_files_across_resume(tmp_path, monkeypatch):
    import hashlib
    import json
    from a2a.types import Artifact, Part
    from a2a_impl.executor import _data_part_proto
    from google.protobuf.json_format import MessageToDict
    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection
    from infra import media

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(str(tmp_path / "resume.db")),
                                              owner_resolver=stores.resolve_task_owner)
    peer = ServerCallContext(state={"requester_principal": "device:one"})
    operator = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    private = await private_intake.record_private_steer(
        store, session_id=CTX, text="STEER-CANARY", msg_id="s1", context=operator)
    projections, refs = [], []

    async def factory(context, queue):
        projection = await RequesterProjection.accept(store, context, queue, agent_id="owner", operator=operator)
        await projection.begin_private(private)
        projections.append(projection)
        return projection

    async def structured(skill, text):
        return {"metadata": {pa.MIME_KEY: "application/json"},
                "content": {"value": {"private": "STRUCTURED-CANARY", "url": refs[1].url}}}

    async def stream(*args, resume=False, **kwargs):
        if not resume:
            refs.extend([media.save_media(b"result bytes", "image/png"),
                         media.save_media(b"FILE-CONTENT-CANARY", "image/png")])
            assert media.attach_file(refs[0], name="result.png")
            assert media.attach_file(refs[1], name="FILE-NAME-CANARY")
            yield "input_required", {"question": "PRIVATE-PAUSE-CANARY"}
        else:
            carried = media.turn_files()
            assert len(carried) == 2
            assert [entry["name"] for entry in carried] == ["result.png", "FILE-NAME-CANARY"]
            selected = Artifact(parts=[Part(text="delivered result"), _data_part_proto({"answer": 42}, "application/json"),
                                       Part(url=carried[0]["url"], filename="result.png", media_type="image/png")])
            identity = await projections[-1].prepare_result(selected)
            selected.parts.append(_data_part_proto({"late": "LATE-CANARY"}, "application/json"))
            with pytest.raises(PermissionError, match="immutable"):
                await projections[-1].prepare_result(selected)
            assert identity
            # No deliver event: finalization auto-delivers the selected envelope.
            try:
                yield "done", "PRIVATE-REPLY-CANARY"
            finally:
                assert not media.attach_file(refs[1], name="LATE-FILE-CANARY")

    handler, _ = _handler(stream, task_store=store, requester_projection_factory=factory,
                          structured_finalizer=structured)
    request = _msg("request", mid="m1")
    parked = await handler.on_message_send(request, peer)
    assert not parked.artifacts
    # A broad ownership handoff cannot take the private attachments.
    for principal in ("other", "device:one"):
        thief = media.begin_turn_files(carry=CTX, principal=principal, claim_mismatch=lambda *_: True)
        assert media.turn_files() == []
        media.end_turn_files(thief)
    request = _msg("continue", mid="m2", task_id=parked.id)
    request.message.metadata.update({"skillHint": "chat"})
    completed = await handler.on_message_send(request, peer)
    wire = json.dumps(MessageToDict(completed))
    assert "delivered result" in wire and '"answer": 42' in wire
    assert "CANARY" not in wire and refs[1].path.name not in wire
    assert len(completed.artifacts) == 1
    binding, delivery = await store.delivery_data(parked.id)
    assert delivery["state"] == "delivered"
    assert hashlib.sha256(delivery["payload"].encode()).hexdigest() == delivery["digest"]
    envelope = json.loads(delivery["payload"])
    assert envelope["artifact"] == MessageToDict(completed.artifacts[0])
    assert envelope["files"] == {refs[0].path.name: {"sha256": hashlib.sha256(b"result bytes").hexdigest(),
                                                  "mime": "image/png"}}
    retained = await store.get_record(private_intake.ref_task_id(private), operator)
    assert "STRUCTURED-CANARY" in str(retained.private_payload)
    assert binding["principal"] == "device:one"
    media._PRIVATE_READERS.clear()
    await store.engine.dispose()


# ── P1e.2: a private segment exits honestly — no content leaks through progress, errors,
# cancellation or tool output, and the delivery record closes without delivering (#5400) ──
# P1e.1 proved a clean completion delivers only the sealed result. This proves the OTHER
# exits: a tool error, a model error, a crash, a cancel and a timeout each leave the peer a
# content-free terminal, with tool args / tool output / reply text / error strings absent
# from every peer surface (live stream, resubscribe, poll, push, history) and the sealed
# candidate never shipped once the turn gave up.
_C = {
    "steer": "STEER-SECRET-CANARY",
    "tool_arg": "TOOL-ARG-CANARY",
    "tool_out": "TOOL-OUTPUT-CANARY",
    "reply": "PRIVATE-REPLY-CANARY",
    "model_err": "MODEL-ERROR-CANARY",
    "crash": "CRASH-MESSAGE-CANARY",
}
_PUBLIC_RESULT = "The requested public result."


def _snapshot(event):
    copy = type(event)()
    copy.CopyFrom(event)
    return copy


# Upper bound on each wait in the private-segment exit test below. Every one of those waits is
# on the event itself (the turn reaching its hold point, a frame, the turn ending), so this only
# stops a real hang from blocking the suite. It is deliberately far above a healthy run: a Windows
# CI runner was seen taking about 1.3 s per persisted step, which put a healthy turn past the old
# 5 s bound while it was still visibly making progress (#5465).
_HANG_GUARD_S = 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,terminal,delivers,outcome_state",
    [
        ("completion", TaskState.TASK_STATE_COMPLETED, True, "completed"),
        ("tool_error", TaskState.TASK_STATE_COMPLETED, True, "completed"),
        ("model_error", TaskState.TASK_STATE_FAILED, False, "failed"),
        ("crash", TaskState.TASK_STATE_FAILED, False, "failed"),
        ("cancel", TaskState.TASK_STATE_CANCELED, False, "canceled"),
        ("timeout", TaskState.TASK_STATE_FAILED, False, "failed"),
    ],
)
async def test_a_private_segment_exit_never_leaks_and_closes_the_delivery(
    tmp_path, fault, terminal, delivers, outcome_state
):
    import httpx
    from a2a.types import CancelTaskRequest, TaskPushNotificationConfig
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection
    from tests._virtual_clock import VirtualClock

    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "exit.db")), owner_resolver=stores.resolve_task_owner,
    )
    await store.initialize()
    peer = ServerCallContext(state={"requester_principal": "device:one", "principal_kind": "paired-agent"})
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    ref = await private_intake.record_private_steer(
        store, session_id=CTX, text=_C["steer"], msg_id="steer", context=op)

    projections, pushes, progress, outcomes = [], [], [], []
    ready, proceed = asyncio.Event(), asyncio.Event()
    config = TaskPushNotificationConfig(id="original", url="https://8.8.8.8/hook")
    gated = fault != "timeout"  # the stall guard conflicts with holding the turn open
    # The stall guard runs on a virtual clock (#5465): on the wall clock its 0.3 s window
    # also timed every healthy step BEFORE the hang (the sealing write, the pushes), and a
    # loaded Windows runner stretched one past it — the turn failed before `ready` was set.
    clock = VirtualClock()

    async def factory(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, peer)
        projection = await RequesterProjection.accept(
            store, context, queue, agent_id="agent:owner", operator=op, push_configs=[config])
        projections.append(projection)
        return projection

    async def stream(*args, **kwargs):
        projection = projections[0]
        await projection.begin_private(ref)
        # Private tool args + output, a usage row, and the private answer text — all canaried.
        yield "tool_start", {"id": "c1", "name": "toolx", "input": _C["tool_arg"]}
        yield "usage", {"input_tokens": 5, "output_tokens": 2, "cost_usd": 0.1, "model": "m-priv"}
        yield "tool_end", {"id": "c1", "name": "toolx", "output": _C["tool_out"],
                           "error": fault == "tool_error", "duration_ms": 4}
        yield "text", _C["reply"]
        # The agent seals the intended public result on EVERY path — so a fault proves a
        # sealed-but-undelivered result is never shipped once the turn gives up.
        assert await projection.prepare_result(_PUBLIC_RESULT)
        ready.set()
        if fault == "cancel":
            await proceed.wait()  # the test cancels the turn while it holds here
            return
        if fault == "timeout":
            await clock.sleep(3600)  # an hour of silence: the stall guard fails it
            await asyncio.Event().wait()  # and the step never returns on its own
            return
        await proceed.wait()
        if fault in ("completion", "tool_error"):
            yield "done", _C["reply"]
        elif fault == "model_error":
            yield "error", _C["model_err"]
        elif fault == "crash":
            raise RuntimeError(_C["crash"])

    set_progress_hook(lambda *a: progress.append(a))
    set_terminal_hook(outcomes.append)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (pushes.append(request.content.decode()), httpx.Response(200))[1]))
    push_store = InMemoryPushNotificationConfigStore()
    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    handler, router = _handler(
        stream, task_store=store, push_config_store=push_store, push_sender=sender,
        requester_projection_factory=factory,
        stall_timeout_provider=(lambda: 0.3) if fault == "timeout" else None)

    frames, sub_frames = [], []

    async def collect(iterator, target):
        async for event in iterator:
            target.append(_snapshot(event))

    with pytest.MonkeyPatch.context() as mp:
        clock.install(mp)
        req = _msg("please proceed", mid="request", ctx=CTX)
        running = asyncio.create_task(collect(handler.on_message_send_stream(req, peer), frames))
        await asyncio.wait_for(ready.wait(), _HANG_GUARD_S)
        task_id = projections[0].binding["task_id"]

        watching = None
        if gated:
            # A live re-attach while the turn is still working: the snapshot is content-free and
            # every frame after it must stay that way too.
            subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), peer)
            sub_frames.append(_snapshot(await asyncio.wait_for(anext(subscribed), _HANG_GUARD_S)))
            watching = asyncio.create_task(collect(subscribed, sub_frames))

        if fault == "cancel":
            cancelled = await asyncio.wait_for(handler.on_cancel_task(CancelTaskRequest(id=task_id), peer), _HANG_GUARD_S)
            assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
        elif gated:
            proceed.set()
        # timeout: the stall guard ends it with no further prompting.

        await asyncio.wait_for(asyncio.gather(*( [running] + ([watching] if watching else []) ),
                                              return_exceptions=True), _HANG_GUARD_S)
        await router.drain()

    # Every peer-readable surface: poll, (content-free) resubscribe snapshot, reopened history.
    got = await handler.on_get_task(GetTaskRequest(id=task_id), peer)
    assert got.status.state == terminal
    history = await store.history_records(CTX, peer)

    surfaces = {
        "stream": [MessageToDict(f) for f in frames],
        "subscription": [MessageToDict(f) for f in sub_frames],
        "poll": MessageToDict(got),
        "push": pushes,
        "history": str(history),
        "progress": progress,
        "outcomes": [vars(o) for o in outcomes],
    }
    for name, surface in surfaces.items():
        blob = str(surface)
        if name == "history" and auth.is_human_operator(peer.state.get("principal_kind")):
            assert _C["steer"] in blob
        else:
            for canary in _C.values():
                assert canary not in blob, (fault, name, canary)

    # The sealed public result is delivered exactly once on a clean exit, and NEVER on a fault.
    peer_blob = str([surfaces[k] for k in ("stream", "subscription", "poll", "push", "history")])
    assert (_PUBLIC_RESULT in peer_blob) is delivers
    if delivers:
        assert [p.text for a in got.artifacts for p in a.parts] == [_PUBLIC_RESULT]
        assert sum(_PUBLIC_RESULT in body for body in pushes) == 1
    else:
        assert not got.artifacts

    # The delivery record is terminal either way, and a crash-recovery deliver can no longer
    # publish: delivered on a clean exit, closed (candidate dropped) on a fault.
    record = None
    for _ in range(200):
        _, record = await store.delivery_data(task_id)
        if record and record.get("state") in ("delivered", "closed"):
            break
        await asyncio.sleep(0.02)
    assert record["state"] == ("delivered" if delivers else "closed")
    recovery = RequesterProjection(store, projections[0].context, projections[0].queue,
                                   projections[0].binding, op)
    recovery.private_ref = ref
    assert not await recovery.deliver_to_requester(record.get("id"))

    # The private turn is isolated, not untelemetered: its spend reaches the terminal row with
    # the content stripped off, so nothing private rides it either.
    row = outcomes[-1]
    assert row.state == outcome_state
    assert row.text == "" and row.error == ""
    assert row.usage["input_tokens"] == 5 and row.tool_calls == 1 and row.llm_calls == 1

    await client.aclose()
    await store.engine.dispose()
