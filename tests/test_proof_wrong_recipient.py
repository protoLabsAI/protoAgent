"""Proof gate W1 — "Wrong recipient / overbroad release" (Conversations in Work, Phase 1).

docs/design/conversations-in-work-proposal.md's proof matrix (the row near line 681) turns
Josh's REVISE into a CONTINUE only with *executed* evidence that a sealed private result
reaches ONLY the original requester — no forger, no substituted push destination and no
second peer in the context ever receives it, and the private input and the agent's private
replies never surface on any peer-readable channel.

This drives the REAL path end to end — the a2a-sdk ``DefaultRequestHandler`` wired like
production (``_handler``), ``ProtoAgentExecutor``, a persisted ``ReasoningCoalescingTaskStore``
on SQLite, and the production ``ValidatingPushNotificationSender`` — for a private steer that
auto-delivers its sealed result. It asserts at the *received* peer surfaces (live stream,
re-subscription, poll, push bodies, reopened saved history, tool output) and in the reopened
store, not at a mocked audience helper. The ``RequesterProjection``/``private_intake``/store
seams exist for exactly these proofs (``requester_delivery`` is a dormant executor seam); this
test adds nothing to ``src/`` — a real defect becomes a strict ``xfail`` named in the PR body.

The matrix mandates every identity-sensitive scenario run for ``federation``, ``fleet-service``,
``paired-agent`` and ``enrolled-console`` requesters, with no operator-tier shortcut, so the
whole proof is parametrized over the four. For each requester kind:

1. Forged requester id, forged context id, forged task id (also agent/message id): nothing is
   delivered to the forger.
2. The push destination is replaced after binding: the substituted destination receives nothing.
3. A second peer joins the context: it receives nothing.
4. Neither a wrong audience label nor a raw B ``deliver_to_requester`` event releases to a
   forged, substituted or second recipient.
5. The original requester receives exactly the sealed payload, once.
6. In the same run, the private input and the agent's private replies are absent from every
   peer surface: stream, subscription, poll, push, reopened saved history and tool output.
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
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
    TaskState,
)
from a2a.utils.errors import TaskNotFoundError
from google.protobuf.json_format import MessageToDict

import protolabs_a2a as pa
from a2a_impl import hitl_routing, private_intake, stores
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.requester_delivery import RequesterProjection

CTX = "chat-wrong-recipient"
_HANDLERS: list = []

# Private content the agent steers on and replies with — none of it may reach a peer surface.
STEER = "WRONG-RECIP-STEER-CANARY"
REPLY = "WRONG-RECIP-REPLY-CANARY"
TOOL_ARG = "WRONG-RECIP-TOOLARG-CANARY"
TOOL_OUT = "WRONG-RECIP-TOOLOUT-CANARY"
PRIVATE_CANARIES = (STEER, REPLY, TOOL_ARG, TOOL_OUT)
# The ONE public value the requester is entitled to — the sealed, delivered result.
RESULT = "The sealed public result."

ORIGINAL_HOOK = "https://8.8.8.8/hook"
SECOND_HOOK = "https://8.8.4.4/other"
STOLEN_HOOK = "https://8.8.4.4/stolen"


# The four identity-sensitive requester kinds the matrix requires, as the auth middleware would
# stamp them onto the call context's state. ``requester_principal`` is the enrolled identity the
# delivery binds to; ``principal_kind`` records how the credential was paired; ``trust_tier``
# picks the owner tier (``resolve_task_owner``). ``device`` reads as a human operator (ADR 0131
# P1a), the other three as peers — but the delivered task is requester-BOUND, so even the
# human-operator enrolled console sees nothing bound to a different principal.
#
# ``second_kind`` is the second recipient that joins the SAME owner tier with a DIFFERENT
# principal: a genuine (non-operator) peer, so the requester-binding gate — not merely a tier or
# human-operator check — is what must deny it.
REQUESTERS = {
    "federation": {
        "requester": {"requester_principal": "fed:peer-1", "principal_kind": "federation",
                      "trust_tier": "federation"},
        "second": {"requester_principal": "fed:intruder-2", "principal_kind": "federation",
                   "trust_tier": "federation"},
        "operator_requester": False,
    },
    "fleet-service": {
        "requester": {"requester_principal": "fleet:service-1", "principal_kind": "fleet-service"},
        "second": {"requester_principal": "fleet:intruder-2", "principal_kind": "fleet-service"},
        "operator_requester": False,
    },
    "paired-agent": {
        "requester": {"requester_principal": "device:agent-1", "principal_kind": "agent-device"},
        "second": {"requester_principal": "device:agent-2", "principal_kind": "agent-device"},
        "operator_requester": False,
    },
    "enrolled-console": {
        "requester": {"requester_principal": "device:console-1", "principal_kind": "device"},
        # A true peer (agent-device) sharing the operator owner tier: never a human operator, so
        # the private shell stays invisible to it and only the binding gate protects the result.
        "second": {"requester_principal": "device:agent-2", "principal_kind": "agent-device"},
        "operator_requester": True,
    },
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


def _handler(stream_fn, *, routing: bool = True, task_store=None, push_config_store=None, push_sender=None,
             requester_projection_factory=None):
    """The production wiring (mirrors tests/test_a2a_hitl_parked_routing.py::_handler)."""
    card = pa.build_agent_card(
        name="t", description="d", url="http://t/a2a", version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])], bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn, requester_projection_factory=requester_projection_factory),
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
        push_config_store=push_config_store or InMemoryPushNotificationConfigStore(),
        push_sender=push_sender,
    )
    router = hitl_routing.install_parked_task_routing(handler) if routing else None
    _HANDLERS.append((handler, router))
    return handler, router


def _request(text: str, *, mid: str, ctx: str) -> SendMessageRequest:
    return SendMessageRequest(message=Message(message_id=mid, context_id=ctx, role=Role.ROLE_USER,
                                              parts=[Part(text=text)]))


def _snapshot(event):
    copy = type(event)()
    copy.CopyFrom(event)
    return copy


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(REQUESTERS))
async def test_wrong_recipient_and_overbroad_release(tmp_path, kind):
    import httpx

    spec = REQUESTERS[kind]
    requester = ServerCallContext(state=dict(spec["requester"]))
    second = ServerCallContext(state=dict(spec["second"]))
    op = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    operator_requester = spec["operator_requester"]

    path = str(tmp_path / "wrong-recipient.db")
    store = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                               owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    # The steer is recorded into the private partition BEFORE any turn, bound to this context.
    ref = await private_intake.record_private_steer(store, session_id=CTX, text=STEER, msg_id="steer", context=op)

    projections, pushes, progress, outcomes = [], [], [], []
    ready, proceed = asyncio.Event(), asyncio.Event()
    config = TaskPushNotificationConfig(id="original", url=ORIGINAL_HOOK)

    push_store = InMemoryPushNotificationConfigStore()

    async def factory(context, queue):
        config.task_id = context.task_id
        await push_store.set_info(context.task_id, config, requester)
        projection = await RequesterProjection.accept(
            store, context, queue, agent_id="agent:owner", operator=op, push_configs=[config])
        # A config inserted by a SECOND peer, outside the accepted binding, cannot widen the
        # recipient set: its digest is absent from the binding's ``push`` allow-list.
        await push_store.set_info(context.task_id, TaskPushNotificationConfig(
            id="second-peer", task_id=context.task_id, url=SECOND_HOOK), second)
        projections.append(projection)
        return projection

    async def stream(*args, **kwargs):
        ready.set()
        await proceed.wait()
        projection = projections[0]
        await projection.begin_private(ref)
        # Private steer intake, a private reply, and a private tool call — every byte canaried.
        yield "steer_consumed", {"items": [{"text": STEER}]}
        yield "text", REPLY
        yield "tool_start", {"id": "c1", "name": "private", "input": TOOL_ARG}
        yield "usage", {"input_tokens": 11, "output_tokens": 7, "cost_usd": 0.25, "model": "m-private"}
        yield "tool_end", {"id": "c1", "name": "private", "output": TOOL_OUT, "duration_ms": 9}
        # A content frame wearing a forged audience label, and a raw B-protocol event, are
        # swallowed — neither is a delivery record, so neither releases anything.
        yield "text", {"audience": "requester", "text": REPLY}
        yield "deliver_to_requester", {"text": REPLY, "audience": "requester"}
        # The agent seals the intended public bytes; selection is immutable thereafter.
        delivery_id = await projection.prepare_result(RESULT)
        assert await projection.prepare_result(RESULT) == delivery_id
        # Forged requester / context / task (and agent / message) ids cannot deliver the sealed
        # payload: the binding on the durable record never matches the forgery.
        for forged_key in ("principal", "context_id", "task_id", "agent_id", "message_id"):
            forged = {**projection.binding, forged_key: "forged"}
            attacker = RequesterProjection(store, projection.context, projection.queue, forged, op)
            attacker.private_ref = ref
            assert not await attacker.deliver_to_requester(delivery_id)
        yield "deliver_to_requester", delivery_id
        yield "deliver_to_requester", delivery_id  # a retry publishes no second artifact
        yield "done", REPLY

    set_progress_hook(lambda *a: progress.append(a))
    set_terminal_hook(outcomes.append)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (pushes.append(request.content.decode()), httpx.Response(200))[1]))
    sender = stores.ValidatingPushNotificationSender(client, push_store, delivery_store=store)
    handler, router = _handler(stream, task_store=store, push_config_store=push_store,
                               push_sender=sender, requester_projection_factory=factory)

    frames, subscriptions = [], []

    async def collect(iterator, target):
        async for event in iterator:
            target.append(_snapshot(event))

    running = asyncio.create_task(
        collect(handler.on_message_send_stream(_request("please proceed", mid="request", ctx=CTX), requester), frames))
    await asyncio.wait_for(ready.wait(), 5)
    task_id = projections[0].binding["task_id"]

    # A live re-attach by the ORIGINAL requester stays open and receives the sealed result.
    subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), requester)
    subscriptions.append(_snapshot(await asyncio.wait_for(anext(subscribed), 5)))
    watching = asyncio.create_task(collect(subscribed, subscriptions))

    # The SECOND peer cannot read nor re-attach mid-run — the task is bound to another principal.
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=task_id), second)
    with pytest.raises(TaskNotFoundError):
        await anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), second))

    proceed.set()
    await asyncio.wait_for(asyncio.gather(running, watching), 15)
    await router.drain()

    # ── r5 / r2: the original requester receives exactly the sealed payload, once ──────────────
    got = await handler.on_get_task(GetTaskRequest(id=task_id), requester)
    assert got.status.state == TaskState.TASK_STATE_COMPLETED
    assert got.context_id == CTX
    assert [p.text for a in got.artifacts for p in a.parts] == [RESULT]
    assert sum(RESULT in str(f) for f in frames) == 1
    assert sum(RESULT in str(f) for f in subscriptions) == 1
    assert sum(RESULT in body for body in pushes) == 1

    # ── r3 (item 6): no private byte on ANY peer-readable surface in this same run ──────────────
    surfaces = {
        "stream": [MessageToDict(f) for f in frames],
        "subscription": [MessageToDict(f) for f in subscriptions],
        "poll": MessageToDict(got),
        "push": pushes,
        "progress": progress,
        "outcomes": [vars(o) for o in outcomes],
    }
    for name, surface in surfaces.items():
        blob = str(surface)
        for canary in PRIVATE_CANARIES:
            assert canary not in blob, (kind, name, canary)

    # ── r1: the second peer receives NOTHING, on every post-run surface ─────────────────────────
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=task_id), second)
    with pytest.raises(TaskNotFoundError):
        await anext(handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), second))
    second_listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id=CTX), second)).tasks}
    assert task_id not in second_listed
    assert await store.get(task_id, second) is None
    # The original requester, by contrast, DOES list its own delivered task.
    requester_listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id=CTX), requester)).tasks}
    assert task_id in requester_listed
    # The second peer's push webhook never received the result, and nothing reached its host.
    assert all(SECOND_HOOK not in body and "8.8.4.4" not in body for body in pushes)

    # ── r1: a substituted push destination receives nothing ─────────────────────────────────────
    artifact = next(f for f in frames if RESULT in str(f))
    pushes.clear()
    # The original (bound) destination still dispatches once through the production sender.
    assert await sender._dispatch_notification(artifact, config, task_id)
    assert len(pushes) == 1 and RESULT in pushes[0]
    # Replace the destination under the same config id, then try to send again: refused, because
    # the new URL's digest is not in the binding's push allow-list.
    replaced = TaskPushNotificationConfig(id="original", task_id=task_id, url=STOLEN_HOOK)
    await push_store.set_info(task_id, replaced, requester)
    await sender.send_notification(task_id, artifact)
    assert len(pushes) == 1
    assert all(STOLEN_HOOK not in body and "8.8.4.4" not in body for body in pushes)

    # ── r1 / r4: binding forgeries and unbound ids cannot create or redirect a release ──────────
    projection = projections[0]
    for forged_key in ("principal", "context_id", "task_id", "agent_id", "message_id"):
        forged = {**projection.binding, forged_key: "forged"}
        attacker = RequesterProjection(store, projection.context, projection.queue, forged, op)
        attacker.private_ref = ref
        assert not await attacker.deliver_to_requester("unbound")
        with pytest.raises(PermissionError):
            await store.bind_requester(got, forged, requester)

    # The private turn is isolated, not untelemetered: its spend reached the terminal row with
    # the content stripped, so nothing private rides the accounting either.
    outcome = next(o for o in outcomes if o.state == "completed")
    assert outcome.text == "" and outcome.error == ""
    assert outcome.usage["input_tokens"] == 11 and outcome.llm_calls == 1 and outcome.tool_calls == 1

    await client.aclose()
    await store.engine.dispose()

    # ── r2 / r3: reopened store (a fresh browser / restarted process) ───────────────────────────
    reopened = stores.ReasoningCoalescingTaskStore(stores.make_sqlite_engine(path),
                                                  owner_resolver=stores.resolve_task_owner)
    try:
        # The original requester still reads exactly the sealed result, with no private byte.
        requester_task = await reopened.get(task_id, requester)
        assert requester_task is not None
        wire = str(MessageToDict(requester_task))
        assert RESULT in wire and all(c not in wire for c in PRIVATE_CANARIES)

        requester_history = await reopened.history_records(CTX, requester)
        assert RESULT in str(requester_history)
        if not operator_requester:
            # A non-operator requester's saved history is public-only: the private shell never
            # enters it. (An enrolled console IS the human operator by design, ADR 0131 P1a, so
            # its own history legitimately includes the private partition — asserted below via a
            # genuine peer instead.)
            assert all(c not in str(requester_history) for c in PRIVATE_CANARIES)

        # The second peer sees nothing at all — not the result, not the private content.
        second_history = await reopened.history_records(CTX, second)
        assert second_history == []
        assert await reopened.get(task_id, second) is None

        # The operator still recovers the private input AND the folded-in reply from the partition.
        private = await reopened.get_record(private_intake.ref_task_id(ref), op)
        assert STEER in str(private.private_payload) and REPLY in str(private.private_payload)

        # The durable delivery is terminal; a crash-recovery deliver can no longer republish it.
        binding, delivery = await reopened.delivery_data(task_id)
        assert delivery["state"] == "delivered"
        recovered = RequesterProjection(reopened, projection.context, projection.queue, binding, op)
        recovered.private_ref = ref
        assert not await recovered.deliver_to_requester(delivery["id"])
    finally:
        await reopened.engine.dispose()
