"""Proof gate W1 — "Wrong recipient / overbroad release" (Conversations in Work, Phase 1).

Executed evidence for docs/design/conversations-in-work-proposal.md's proof matrix (row ~line 681): a sealed
private result reaches ONLY the original requester — no forger, substituted push destination or second peer
gets it, and the private input/replies never surface on a peer-readable channel. Drives the REAL path end to
end (production ``_handler`` wiring, ``ProtoAgentExecutor``, a persisted store and push sender), asserting at
the *received* peer surfaces (stream, re-subscribe, poll, push, reopened history, tool output) and the reopened
store. Adds nothing to ``src/``; a real defect becomes a strict ``xfail`` in the PR body. Runs for all four kinds.
"""

from __future__ import annotations

import asyncio

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import (AgentSkill, GetTaskRequest, ListTasksRequest, Message, Part, Role,
                       SendMessageRequest, SubscribeToTaskRequest, TaskPushNotificationConfig, TaskState)
from a2a.utils.errors import TaskNotFoundError
from google.protobuf.json_format import MessageToDict

import protolabs_a2a as pa
from a2a_impl import hitl_routing, private_intake, stores
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.requester_delivery import RequesterProjection

CTX = "chat-wrong-recipient"
_HANDLERS: list = []
# Private content the agent steers on and replies with — none of it may reach a peer surface.
STEER, REPLY, TOOL_ARG, TOOL_OUT = "W1-STEER", "W1-REPLY", "W1-TOOLARG", "W1-TOOLOUT"
PRIVATE_CANARIES = (STEER, REPLY, TOOL_ARG, TOOL_OUT)
RESULT = "The sealed public result."  # the ONE public value the requester is entitled to
ORIGINAL_HOOK, SECOND_HOOK, STOLEN_HOOK = "https://8.8.8.8/hook", "https://8.8.4.4/other", "https://8.8.4.4/stolen"
# kind -> (requester state, second-peer state, operator_requester), stamped onto the call-context state as the
# auth middleware would. The peer shares the owner tier with a DIFFERENT principal (so only the requester-binding
# gate denies it, not a tier/human-operator check); ``device`` reads as the human operator (ADR 0131 P1a).
REQUESTERS = {
    "federation": ({"requester_principal": "fed:peer-1", "principal_kind": "federation", "trust_tier": "federation"},
                   {"requester_principal": "fed:intruder", "principal_kind": "federation", "trust_tier": "federation"},
                   False),
    "fleet-service": ({"requester_principal": "fleet:service-1", "principal_kind": "fleet-service"},
                      {"requester_principal": "fleet:intruder", "principal_kind": "fleet-service"}, False),
    "paired-agent": ({"requester_principal": "device:agent-1", "principal_kind": "agent-device"},
                     {"requester_principal": "device:agent-2", "principal_kind": "agent-device"}, False),
    "enrolled-console": ({"requester_principal": "device:console-1", "principal_kind": "device"},
                         {"requester_principal": "device:agent-2", "principal_kind": "agent-device"}, True),
}


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


def _handler(stream_fn, *, task_store, push_config_store, push_sender, requester_projection_factory):
    """Production wiring (mirrors tests/test_a2a_hitl_parked_routing.py::_handler)."""
    card = pa.build_agent_card(name="t", description="d", url="http://t/a2a", version="0.0.0", bearer=False,
                               skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])])
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn, requester_projection_factory=requester_projection_factory),
        task_store=task_store, agent_card=card, push_config_store=push_config_store, push_sender=push_sender)
    router = hitl_routing.install_parked_task_routing(handler)
    _HANDLERS.append((handler, router))
    return handler, router


def _request(text, *, mid, ctx):
    return SendMessageRequest(message=Message(message_id=mid, context_id=ctx, role=Role.ROLE_USER,
                                              parts=[Part(text=text)]))


def _snapshot(event):
    copy = type(event)()
    copy.CopyFrom(event)
    return copy


def _forged_bindings(binding):
    """Each of the five identity fields, one at a time, replaced by a forgery."""
    for key in ("principal", "context_id", "task_id", "agent_id", "message_id"):
        yield {**binding, key: "forged"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(REQUESTERS))
async def test_wrong_recipient_and_overbroad_release(tmp_path, kind):
    import httpx
    req_state, second_state, operator_requester = REQUESTERS[kind]
    requester = ServerCallContext(state=dict(req_state))
    second = ServerCallContext(state=dict(second_state))
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    path = str(tmp_path / "wrong-recipient.db")
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                               owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    ref = await private_intake.record_private_steer(store, session_id=CTX, text=STEER, msg_id="steer", context=op)
    projections, pushes, progress, outcomes = [], [], [], []
    ready, proceed = asyncio.Event(), asyncio.Event()
    config = TaskPushNotificationConfig(id="original", url=ORIGINAL_HOOK)
    push_store = InMemoryPushNotificationConfigStore()

    async def factory(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, requester)
        projection = await RequesterProjection.accept(store, context, queue, agent_id="agent:owner", operator=op,
                                                       push_configs=[config])
        # A SECOND peer's config, outside the accepted binding, can't widen the recipient set (digest not in it).
        await push_store.set_info(context.task_id, TaskPushNotificationConfig(
            id="second-peer", task_id=context.task_id, url=SECOND_HOOK), second)
        projections.append(projection)
        return projection

    async def stream(*args, **kwargs):
        ready.set()
        await proceed.wait()
        projection = projections[0]
        await projection.begin_private(ref)
        yield "steer_consumed", {"items": [{"text": STEER}]}
        yield "text", REPLY
        yield "tool_start", {"id": "c1", "name": "private", "input": TOOL_ARG}
        yield "usage", {"input_tokens": 11, "output_tokens": 7, "cost_usd": 0.25, "model": "m-private"}
        yield "tool_end", {"id": "c1", "name": "private", "output": TOOL_OUT, "duration_ms": 9}
        # A forged-audience content frame and a raw B-protocol event are swallowed — neither is a delivery.
        yield "text", {"audience": "requester", "text": REPLY}
        yield "deliver_to_requester", {"text": REPLY, "audience": "requester"}
        delivery_id = await projection.prepare_result(RESULT)  # seal the public bytes; selection is now immutable
        assert await projection.prepare_result(RESULT) == delivery_id
        yield "deliver_to_requester", delivery_id
        yield "deliver_to_requester", delivery_id  # a retry publishes no second artifact
        yield "done", REPLY
    set_progress_hook(lambda *a: progress.append(a))
    set_terminal_hook(outcomes.append)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (pushes.append((str(request.url), request.content.decode())), httpx.Response(200))[1]))
    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    handler, router = _handler(stream, task_store=store, push_config_store=push_store, push_sender=sender,
                               requester_projection_factory=factory)
    frames, subscriptions = [], []

    async def collect(iterator, target):
        async for event in iterator:
            target.append(_snapshot(event))
    running = asyncio.create_task(
        collect(handler.on_message_send_stream(_request("please proceed", mid="request", ctx=CTX), requester), frames))
    await asyncio.wait_for(ready.wait(), 5)
    task_id = projections[0].binding["task_id"]
    subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), requester)
    subscriptions.append(_snapshot(await asyncio.wait_for(anext(subscribed), 5)))
    watching = asyncio.create_task(collect(subscribed, subscriptions))
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=task_id), second)
    with pytest.raises(TaskNotFoundError):
        await anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), second))
    proceed.set()
    await asyncio.wait_for(asyncio.gather(running, watching), 15)
    await router.drain()
    # r5 / r2: the original requester receives exactly the sealed payload, once.
    got = await handler.on_get_task(GetTaskRequest(id=task_id), requester)
    assert got.status.state == TaskState.TASK_STATE_COMPLETED and got.context_id == CTX
    assert [p.text for a in got.artifacts for p in a.parts] == [RESULT]
    assert [sum(RESULT in str(f) for f in frames), sum(RESULT in str(f) for f in subscriptions),
            sum(RESULT in body for _, body in pushes)] == [1, 1, 1]
    # r3 (item 6): no private byte on ANY peer-readable surface in this same run.
    surfaces = {"stream": [MessageToDict(f) for f in frames], "subscription": [MessageToDict(f) for f in subscriptions],
                "poll": MessageToDict(got), "push": pushes, "progress": progress,
                "outcomes": [vars(o) for o in outcomes]}
    for name, surface in surfaces.items():
        assert all(c not in str(surface) for c in PRIVATE_CANARIES), (kind, name)
    # r1: the second peer receives NOTHING on every post-run surface; the original DOES list its own task.
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=task_id), second)
    with pytest.raises(TaskNotFoundError):
        await anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), second))
    second_listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id=CTX), second)).tasks}
    assert task_id not in second_listed and await store.get(task_id, second) is None
    requester_listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id=CTX), requester)).tasks}
    assert task_id in requester_listed
    assert {url for url, _ in pushes} == {ORIGINAL_HOOK}
    assert [url for url, body in pushes if RESULT in body] == [ORIGINAL_HOOK]
    # r1: substituted push destination gets nothing — bound destination dispatches once; replacing the URL
    # under the same config id is refused (its digest is not in the allow-list).
    artifact = next(f for f in frames if RESULT in str(f))
    original_pushes = list(pushes)
    await push_store.set_info(task_id, TaskPushNotificationConfig(id="original", task_id=task_id, url=STOLEN_HOOK),
                              requester)
    await sender.send_notification(task_id, artifact)
    assert pushes == original_pushes
    assert not {SECOND_HOOK, STOLEN_HOOK}.intersection(url for url, _ in pushes)
    # r1 / r4: binding forgeries and unbound ids cannot create or redirect a release.
    projection = projections[0]
    for forged in _forged_bindings(projection.binding):
        attacker = RequesterProjection(store, projection.context, projection.queue, forged, op)
        attacker.private_ref = ref
        assert not await attacker.deliver_to_requester("unbound")
        with pytest.raises(PermissionError):
            await store.bind_requester(got, forged, requester)
    outcome = next(o for o in outcomes if o.state == "completed")
    assert outcome.text == "" and outcome.error == ""
    assert outcome.usage["input_tokens"] == 11 and outcome.llm_calls == 1 and outcome.tool_calls == 1
    await client.aclose()
    await store.engine.dispose()
    # r2 / r3: reopened store (a fresh browser / restarted process).
    reopened = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                                  owner_resolver=stores.resolve_task_owner)
    try:
        requester_task = await reopened.get(task_id, requester)
        assert requester_task is not None
        wire = str(MessageToDict(requester_task))
        assert RESULT in wire and all(c not in wire for c in PRIVATE_CANARIES)
        requester_history = await reopened.history_records(CTX, requester)
        assert RESULT in str(requester_history)
        # Non-operator history is public-only; an enrolled console IS the human operator (ADR 0131 P1a) so its
        # own history legitimately holds the private partition — the second-peer asserts below prove it there.
        if not operator_requester:
            assert all(c not in str(requester_history) for c in PRIVATE_CANARIES)
        assert await reopened.history_records(CTX, second) == []
        assert await reopened.get(task_id, second) is None
        private = await reopened.get_record(private_intake.ref_task_id(ref), op)
        assert STEER in str(private.private_payload) and REPLY in str(private.private_payload)
        binding, delivery = await reopened.delivery_data(task_id)
        assert delivery["state"] == "delivered"
        recovered = RequesterProjection(reopened, projection.context, projection.queue, binding, op)
        recovered.private_ref = ref
        assert not await recovered.deliver_to_requester(delivery["id"])
    finally:
        await reopened.engine.dispose()
