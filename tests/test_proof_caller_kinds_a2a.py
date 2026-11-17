"""P1 proof K1: the A2A privacy proofs hold for every KIND of caller, not one identity.

The proof matrix in ``docs/design/conversations-in-work-proposal.md`` ("Proof that turns
REVISE into CONTINUE") requires each identity-sensitive scenario to run "for federation,
fleet-service, paired-agent and enrolled-console callers; no operator-tier shortcut." The
Phase-0/P1b scenarios in ``tests/test_a2a_hitl_parked_routing.py`` prove privacy with ONE
hand-set caller apiece, so no pass may rest on that single identity.

This file re-runs six of those scenarios with the participants authenticated through the
REAL auth path — never a hand-set tier:

* the three PEER kinds come straight through ``a2a_impl.auth.A2AAuthMiddleware`` exactly as
  ``tests/test_a2a_auth.py::test_verified_principal_in_http_call_context`` classifies them —
  ``federation`` (a federation bearer), ``fleet-service`` (the ADR 0089 service token) and
  ``paired-agent`` (an ``agent``-kind device pairing → ``agent-device``). All three are
  OPERATOR-grade or federation *tier* yet are NOT human operators
  (``auth.is_human_operator`` is False), which is the whole point of "no operator-tier
  shortcut": operator tier must not be a shortcut to the human operator's private content.
* the ``enrolled-console`` kind is the human operator itself, reached the only way a human's
  authority crosses a trust hop — a verified ``fleet-proxy`` delegation (ADR 0131 P1a.2,
  ``graph.fleet.proxy.mint_member_delegation`` → ``auth.resolve_human_operator``), which
  conveys the ``shared-operator-bearer`` human kind. It takes the OPERATOR role in every run
  (recording the private steer and recovering it), so the operator tier is never hand-set.

Original privacy assertions are kept verbatim; helpers are copied from the source file, which
is left untouched. Where a scenario's ORIGINAL assertion fails for an operator-tier peer
because the leak is real, that case is a strict ``xfail`` named "PROOF FAILURE" (see the
module's ``## Proof failures`` note in the PR body) — never fixed here, and never in ``src/``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

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
    TaskState,
)

import protolabs_a2a as pa
from a2a_impl import auth, hitl_routing
from a2a_impl.executor import ProtoAgentExecutor, set_progress_hook, set_terminal_hook
from a2a_impl.stores import tier_context_builder
from security import devices

# The auth classifier reaches the device registry and HMAC-verifies tokens, like the auth
# suite itself; mark the file so the Windows-scoped lane treats it the same way.
pytestmark = pytest.mark.platform_sensitive


# ── the four caller kinds, authenticated for real ────────────────────────────────────────

CTX = "chat-3930"

# Three peer kinds (never a human operator) + the human operator reached via delegation.
PEER_KINDS = ("federation", "fleet-service", "paired-agent")
ENROLLED_CONSOLE = "enrolled-console"

# An operator-tier but non-human peer (fleet-service / agent-device) shares the operator's
# OWNER partition (``OPERATOR_OWNER``), while federation is isolated in its own partition. The
# owner-scoped parked/listing fallback keys on that partition — on trust tier, NOT on
# ``is_human_operator`` — so it cannot tell such a peer from the operator. These scenarios
# therefore leak the operator's private turn to that peer. Named, not fixed (LEAK RULE).
_OPERATOR_TIER_PEER_LEAK = (
    "PROOF FAILURE: an operator-tier but non-human peer (fleet-service / agent-device) shares "
    "the operator OWNER partition, so the owner-scoped parked-task / list fallback exposes the "
    "operator's private turn — and lands a guarded private resume on the peer's own parked task, "
    "folding the private reference into a peer-readable task. Owner isolation keys on trust tier, "
    "not is_human_operator."
)


def _kinds(*, operator_tier_leak: bool):
    """The four caller-kind params for a scenario. ``operator_tier_leak`` xfails the two
    operator-tier peer kinds whose owner-partition sharing defeats the scenario's isolation."""
    params = []
    for kind in PEER_KINDS:
        leaks = operator_tier_leak and kind in ("fleet-service", "paired-agent")
        marks = [pytest.mark.xfail(strict=True, reason=_OPERATOR_TIER_PEER_LEAK)] if leaks else []
        params.append(pytest.param(kind, marks=marks, id=kind))
    params.append(pytest.param(ENROLLED_CONSOLE, id=ENROLLED_CONSOLE))
    return params


def _authed_state(token: str) -> dict:
    """The call-context state the REAL ``A2AAuthMiddleware`` + ``tier_context_builder`` stamp
    for ``token`` — the verified principal kind and trust tier, never a hand-set value."""
    captured: dict = {}

    def inspect(request):
        captured["state"] = dict(tier_context_builder().build(request).state)
        return JSONResponse({})

    app = Starlette(routes=[Route("/a2a", inspect, methods=["GET", "POST"])])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        response = client.post("/a2a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, (token, response.status_code)
    return captured["state"]


@pytest.fixture
def principals(delegation_env, tmp_path, monkeypatch):
    """Factory for real-auth participants. ``delegation_env`` (tests/conftest.py — the same
    deterministic hub keypair the fleet-proxy suite uses) backs the human-operator delegation."""
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "caller-devices.json")
    for name in ("A2A_AUTH_TOKEN", "A2A_FEDERATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def _reset():
        auth._BEARER[0] = None
        auth._FEDERATION[0] = None
        auth._FLEET[0] = None
        auth._API_KEY[0] = ""
        auth._ALLOWED_ORIGINS[0] = None
        auth._MEMBER_PUBLIC[0] = None
        auth._PLUGIN_FEDERATION[:] = []

    _reset()
    # One closed instance that offers every tier at once, so one classifier run picks each kind.
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet",
                   api_key="", allowed_origins_raw="")

    def human_operator() -> ServerCallContext:
        """The human operator, reached as an enrolled console would be: the hub classifies the
        shared bearer as a human kind, mints a one-member delegation, and the member redeems
        that VERIFIED delegation to a ``shared-operator-bearer`` principal (ADR 0131 P1a.2).
        Never a hand-set operator tier."""
        from graph.fleet.proxy import mint_member_delegation

        hub = _authed_state("shared")
        assert auth.is_human_operator(hub["principal_kind"])
        hub_request = SimpleNamespace(state=SimpleNamespace(principal_kind=hub["principal_kind"]))
        token = mint_member_delegation(
            "member", hub_request, method="SendMessage",
            context="delegation-ctx", operation="delegation-op", task_id="",
        )
        assert token, "a human operator must earn a fleet-proxy delegation"
        grant = auth.resolve_human_operator(
            SimpleNamespace(headers={delegation_env.DELEGATION_HEADER: token}),
            audience="member", method="SendMessage",
            context="delegation-ctx", operation="delegation-op", task_id="", idempotent=True,
        )
        assert grant is not None and auth.is_human_operator(grant.principal_kind)
        return ServerCallContext(state={"principal_kind": grant.principal_kind})

    def peer(kind: str, *, requester_principal: str | None = None) -> ServerCallContext:
        """A verified NON-human peer of ``kind``. ``enrolled-console`` is the human operator, so
        it never plays the denied peer; its runs use a representative federation peer while the
        human operator (above) carries the private content."""
        if kind == "federation":
            state = _authed_state("fed")
        elif kind == "fleet-service":
            state = _authed_state("fleet")
        elif kind == "paired-agent":
            code, _ = devices.start_pairing(kind="agent")
            _, token = devices.claim_pairing(code, "proof-agent")
            state = _authed_state(token)
        elif kind == ENROLLED_CONSOLE:
            state = _authed_state("fed")
        else:  # pragma: no cover — guarded by the param lists
            raise ValueError(kind)
        if requester_principal is not None:
            state["requester_principal"] = requester_principal
        return ServerCallContext(state=state)

    yield SimpleNamespace(human_operator=human_operator, peer=peer)
    _reset()


# ── copied test harness (verbatim from tests/test_a2a_hitl_parked_routing.py) ─────────────

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


def _msg(text: str, *, mid: str, task_id: str = "", hitl_resume: bool = False, ctx: str = CTX) -> SendMessageRequest:
    message = Message(message_id=mid, context_id=ctx, role=Role.ROLE_USER, parts=[Part(text=text)])
    if task_id:
        message.task_id = task_id
    if hitl_resume:
        message.metadata.update({"hitl_resume": True})
    return SendMessageRequest(message=message)


async def _parked(handler, context: ServerCallContext, ctx: str = CTX) -> list[str]:
    return [t.id for t in await hitl_routing.ParkedTaskRouter(handler).parked_tasks(ctx, context)]


_SECRET = "SECRET-PRIVATE-STEER-xyz"


def _private_steer_stream(seen: list):
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
    assert any(_SECRET in (r.private_payload or "") for r in rows)
    assert all(_SECRET not in str((r.history, r.artifacts, r.metadata, r.status)) for r in rows)


# ── Idle and parked/resume: the private input never enters a peer-readable surface ────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=False))
@pytest.mark.parametrize("parked", [False, True])
async def test_phase0_output_gate_cannot_hide_private_input_history(tmp_path, principals, parked, kind):
    from a2a.types import TaskPushNotificationConfig
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    op = principals.human_operator()
    peer = principals.peer(kind)

    ctx_id = "chat-private"
    engine = stores.make_sqlite_engine(str(tmp_path / "p.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    # The steer is recorded into the partition BEFORE any turn; the loopback would carry only
    # this opaque reference bound to the delegation.
    ref = await private_intake.record_private_steer(store, session_id=ctx_id, text=_SECRET, msg_id="m0", context=op)

    async def _stream_send(req):
        return [e async for e in handler.on_message_send_stream(req, op)]

    # stream: the live SSE frames a console watches as the turn runs — for a fresh (idle)
    # private steer and for a parked resume.
    if parked:
        parkd = await handler.on_message_send(_msg("ask me something", mid="a1", ctx=ctx_id), op)
        assert parkd.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        turn_id = parkd.id
        frames = await asyncio.wait_for(_stream_send(_ref_msg(ref, mid="o1", hitl_resume=True, ctx=ctx_id)), 5)
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
    got = await handler.on_get_task(GetTaskRequest(id=turn_id), op)
    assert got.status.state == TaskState.TASK_STATE_COMPLETED
    assert _SECRET not in str(MessageToDict(got))  # poll
    # push: a registered webhook ships that same Task projection — nothing private to deliver.
    await handler.on_create_task_push_notification_config(
        TaskPushNotificationConfig(task_id=turn_id, url="https://8.8.8.8/hook"), op
    )
    assert _SECRET not in str(MessageToDict(await handler.on_get_task(GetTaskRequest(id=turn_id), op)))

    # reopened history: a fresh store on the same db (a new browser rebuilding the chat) still
    # never reads the private text to the PEER; the operator recovers input and folded-in reply.
    await engine.dispose()
    engine2 = stores.make_sqlite_engine(str(tmp_path / "p.db"))
    store2 = stores.ReasoningCoalescingTaskStore(engine2, owner_resolver=stores.resolve_task_owner)
    await store2.initialize()
    try:
        assert _SECRET not in str(MessageToDict(await store2.get(turn_id, op)))
        assert _SECRET not in str(await store2.history_records(ctx_id, peer))
        assert await store2.get(private_intake.ref_task_id(ref), peer) is None  # the shell is invisible to a peer
        assert await private_intake.record_private_reply(store2, ref, "handled privately", op)
        rec = await store2.get_record(private_intake.ref_task_id(ref), op)
        assert rec.private_payload["input"] == _SECRET and rec.private_payload["reply"] == "handled privately"
    finally:
        await engine2.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=True))
async def test_a_private_steer_into_a_peer_thread_hides_the_input_and_keeps_the_thread_the_peers(
    tmp_path, principals, kind
):
    """#5380: a private steer into a peer's context runs as the operator's own task carrying
    only the reference; the peer keeps its thread, a later peer follow-up never sees the private
    input, and the private-only shell is invisible to the peer."""
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import TaskNotFoundError
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    op = principals.human_operator()
    peer = principals.peer(kind)

    engine = stores.make_sqlite_engine(str(tmp_path / "peer.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    first = await handler.on_message_send(_msg("please look", mid="p1", ctx="fed-ctx"), peer)
    ref = await private_intake.record_private_steer(store, session_id="fed-ctx", text=_SECRET, msg_id="m0", context=op)
    steer = await handler.on_message_send(_ref_msg(ref, mid="o1", ctx="fed-ctx"), op)
    assert steer.id != first.id  # the operator's private turn is its OWN task in the peer's context
    await router.drain()

    # A later peer follow-up continues the peer's own thread and never sees the private input.
    again = await handler.on_message_send(_msg("any update?", mid="p2", ctx="fed-ctx"), peer)
    assert again.context_id == "fed-ctx"
    listed = {t.id for t in (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), peer)).tasks}
    assert steer.id not in listed and first.id in listed
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=steer.id), peer)
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(GetTaskRequest(id=private_intake.ref_task_id(ref)), peer)
    peer_tasks = (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), peer)).tasks
    assert _SECRET not in str([MessageToDict(t) for t in peer_tasks])
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["input"] == _SECRET
    assert all(_SECRET not in str(c["text"]) for c in seen)
    await router.drain()
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=True))
async def test_a_private_resume_never_lands_on_a_peer_owned_parked_task(tmp_path, principals, kind):
    """#5380 / ADR 0131 P1b.2: when a PEER owns the only parked task in a context and the
    operator has none, a private steer taking the guarded resume path must NOT be re-pointed
    onto the peer's parked task — the operator→federation read fallback would otherwise do that
    and land the private reply on a peer-readable task. It runs as the operator's own task
    instead; the peer's parked task and its requires_input message keep no private input or
    reply, and an operator turn ending in the peer's context never settles the peer's pause."""
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import TaskNotFoundError
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores

    op = principals.human_operator()
    peer = principals.peer(kind)

    engine = stores.make_sqlite_engine(str(tmp_path / "peer-parked.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    seen: list = []
    handler, router = _handler(_private_steer_stream(seen), task_store=store)

    # The PEER parks a HITL turn in its own context; the operator owns no parked task here, but
    # the operator's read of the context DOES see the peer's via the federation fallback.
    peer_parked = await handler.on_message_send(_msg("ask me something", mid="p1", ctx="fed-ctx"), peer)
    assert peer_parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert await _parked(handler, peer, "fed-ctx") == [peer_parked.id]
    owner_scoped = await hitl_routing.ParkedTaskRouter(handler).parked_tasks("fed-ctx", op, owner_scoped=True)
    assert owner_scoped == []  # the operator owns no parked task — the fallback is skipped

    ref = await private_intake.record_private_steer(store, session_id="fed-ctx", text=_SECRET, msg_id="m0", context=op)
    steer = await handler.on_message_send(_ref_msg(ref, mid="o1", hitl_resume=True, ctx="fed-ctx"), op)
    await router.drain()

    # The private resume ran as the operator's OWN fresh task — never the peer's parked one.
    assert steer.id != peer_parked.id
    assert steer.status.state == TaskState.TASK_STATE_COMPLETED

    # The peer's parked task is untouched: still input-required (never settled by the operator
    # turn), its requires_input message / history hold no private input or reply, and the
    # private reference is invisible to the peer.
    peer_view = await handler.on_get_task(GetTaskRequest(id=peer_parked.id), peer)
    assert peer_view.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    dumped = str(MessageToDict(peer_view))
    assert _SECRET not in dumped and ref not in dumped
    peer_tasks = (await handler.on_list_tasks(ListTasksRequest(context_id="fed-ctx"), peer)).tasks
    assert steer.id not in {t.id for t in peer_tasks}
    assert all(_SECRET not in str(MessageToDict(t)) and ref not in str(MessageToDict(t)) for t in peer_tasks)
    with pytest.raises(TaskNotFoundError):  # the operator's private turn stays out of the peer's reads
        await handler.on_get_task(GetTaskRequest(id=steer.id), peer)

    # The runner only ever saw the opaque reference, never the private text; the operator still
    # recovers the input from the partition.
    assert any(c["text"] == ref for c in seen) and all(_SECRET not in str(c["text"]) for c in seen)
    assert (await store.get_record(private_intake.ref_task_id(ref), op)).private_payload["input"] == _SECRET
    await router.drain()
    await engine.dispose()


# ── Running with preexisting handles: retained handles stay isolated per caller kind ──────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=False))
async def test_phase0_preexisting_handles(tmp_path, principals, kind):
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
    peer = principals.peer(kind, requester_principal="device:one")
    other = principals.peer(kind, requester_principal="device:two")
    op = principals.human_operator()
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


# ── Other exits: a sealed result keeps its files; every exit closes the delivery ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=False))
async def test_private_final_envelope_keeps_selected_files_across_resume(tmp_path, monkeypatch, principals, kind):
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
    peer = principals.peer(kind, requester_principal="device:one")
    operator = principals.human_operator()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _kinds(operator_tier_leak=False))
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
    tmp_path, principals, fault, terminal, delivers, outcome_state, kind
):
    import httpx
    from a2a.types import CancelTaskRequest, TaskPushNotificationConfig
    from google.protobuf.json_format import MessageToDict

    from a2a_impl import private_intake, stores
    from a2a_impl.requester_delivery import RequesterProjection

    store = stores.ReasoningCoalescingTaskStore(
        stores.make_sqlite_engine(str(tmp_path / "exit.db")), owner_resolver=stores.resolve_task_owner,
    )
    await store.initialize()
    peer = principals.peer(kind, requester_principal="device:one")
    op = principals.human_operator()
    ref = await private_intake.record_private_steer(
        store, session_id=CTX, text=_C["steer"], msg_id="steer", context=op)

    projections, pushes, progress, outcomes = [], [], [], []
    ready, proceed = asyncio.Event(), asyncio.Event()
    config = TaskPushNotificationConfig(id="original", url="https://8.8.8.8/hook")
    gated = fault != "timeout"  # the stall guard conflicts with holding the turn open

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
            await asyncio.sleep(3600)  # the stall guard fails it
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

    req = _msg("please proceed", mid="request", ctx=CTX)
    running = asyncio.create_task(collect(handler.on_message_send_stream(req, peer), frames))
    await asyncio.wait_for(ready.wait(), 5)
    task_id = projections[0].binding["task_id"]

    watching = None
    if gated:
        # A live re-attach while the turn is still working: the snapshot is content-free and
        # every frame after it must stay that way too.
        subscribed = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), peer)
        sub_frames.append(_snapshot(await asyncio.wait_for(anext(subscribed), 5)))
        watching = asyncio.create_task(collect(subscribed, sub_frames))

    if fault == "cancel":
        cancelled = await asyncio.wait_for(handler.on_cancel_task(CancelTaskRequest(id=task_id), peer), 5)
        assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
    elif gated:
        proceed.set()
    # timeout: the stall guard ends it with no further prompting.

    await asyncio.wait_for(asyncio.gather(*( [running] + ([watching] if watching else []) ),
                                          return_exceptions=True), 15)
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
