"""The steer route's ``private: true`` branch, end to end (ADR 0131 P1h.3). Real: the auth
middleware (each caller kind earns its ``principal_kind``), the route, the loopback
``SendMessage`` (in-process), the SDK handler + parked routing, the executor, the stream
runner and the SQLite partition. Only the model turn itself is scripted."""

import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from fastapi import FastAPI
from operator_api.chat_routes import register_chat_routes
from sqlalchemy import text as sql

import protolabs_a2a as pa
from a2a_impl import auth, hitl_routing
from a2a_impl import segment_schedule as ss
from a2a_impl.executor import ProtoAgentExecutor
from a2a_impl.v03_compat import create_a2a_jsonrpc_routes
from graph import steering
from graph.middleware.steering import SteeringMiddleware
from runtime.state import STATE
from security import devices
from tests._a2a_app import A2A_HEADERS, poll_terminal, skill
from tests.test_steer_idle_starts_turn import _Graph, _idle_snapshot, _parked_snapshot, _partition

chat_mod = importlib.import_module("server.chat")
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]  # HMAC token verify + async bodies

SECRET, REPLY = "SECRET-CANARY-zq", "REPLY-CANARY-zq"
HUMANS = ("shared-bearer", "enrolled-console")
PEERS = ("fleet-service", "paired-agent", "federation")
KINDS = HUMANS + PEERS
REFUSED = "Private steering is not available for this request."


@pytest.fixture
async def rig(tmp_path, monkeypatch, delegation_env):
    """One closed instance: chat routes + /a2a behind the real auth middleware, every caller kind."""
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "devices.json")
    for name, blank in (("_BEARER", [None]), ("_FEDERATION", [None]), ("_FLEET", [None]), ("_API_KEY", [""]),
                        ("_ALLOWED_ORIGINS", [None]), ("_MEMBER_PUBLIC", [None]), ("_PLUGIN_FEDERATION", [])):
        monkeypatch.setattr(auth, name, blank)  # a clean guard, restored after the test
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet", api_key="", allowed_origins_raw="")
    tokens = {"shared-bearer": "shared", "fleet-service": "fleet", "federation": "fed"}
    for kind, device_kind in (("paired-agent", "agent"), ("enrolled-console", "device")):
        tokens[kind] = devices.claim_pairing(devices.start_pairing(kind=device_kind)[0], kind)[1]
    engine, store = await _partition(tmp_path)
    graph = _Graph(_idle_snapshot())
    config = SimpleNamespace(private_steering_enabled=True, model_vision=False, max_iterations=50)
    for name, value in (("graph", graph), ("a2a_task_engine", engine), ("graph_config", config), ("active_port", 7999)):
        monkeypatch.setattr(STATE, name, value, raising=False)
    steering._reset()
    chat_mod._PRIVATE_RUNS.clear()
    r = SimpleNamespace(config=config, seen=[], folded=[], running=asyncio.Event(), release=asyncio.Event())

    async def _native(message, session_id, config, *, resume=False, **_kwargs):
        # The scripted model turn: "park" asks a question, "hold" runs until released.
        r.seen.append((message, resume))
        if "park" in message and not resume:
            graph.snapshot = _parked_snapshot()
            yield ("input_required", {"question": "Which one?"})
            return
        if "hold" in message:
            r.running.set()
            await r.release.wait()
            # The next model call of this turn: what the steering middleware folds in.
            r.folded.append(SteeringMiddleware._inject({"session_id": session_id, "messages": []}))
        graph.snapshot = _idle_snapshot()
        yield ("done", REPLY if SECRET in message else "ok")

    async def _no_pre(pre, session_id, request_metadata):
        return
        yield  # pragma: no cover — an async generator that yields nothing

    async def _model(session_id, request_model):
        return "", ""

    monkeypatch.setattr(chat_mod._chat_dispatch, "_pre_turn_dispatch", _no_pre)
    monkeypatch.setattr(chat_mod, "_run_native_turn", _native)
    monkeypatch.setattr(chat_mod._goal_loop, "resolve_turn_model", _model)
    card = pa.build_agent_card(name="t", description="d", url="http://t/a2a", version="0", skills=[skill()], bearer=True)
    handler = DefaultRequestHandler(agent_executor=ProtoAgentExecutor(chat_mod._chat_langgraph_stream), task_store=store,
                                    agent_card=card, push_config_store=InMemoryPushNotificationConfigStore())
    router = hitl_routing.install_parked_task_routing(handler)
    register_chat_routes(app := FastAPI(), ui="none")
    app.router.routes.extend(create_a2a_jsonrpc_routes(handler, rpc_url="/a2a"))
    app.add_middleware(auth.A2AAuthMiddleware)
    real_client = httpx.AsyncClient
    def _client(kind="shared-bearer", **kwargs):
        headers = {"Authorization": f"Bearer {tokens[kind]}", **A2A_HEADERS}
        return real_client(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers, **kwargs)

    # The loopback self-POST lands on this same app, through the same auth and /a2a route.
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _client(timeout=kwargs.get("timeout")))
    clients = {kind: _client(kind) for kind in KINDS}
    async def steer(kind, session, text, mid, **extra):
        return await clients[kind].post(f"/api/chat/sessions/{session}/steer", json={"id": mid, "text": text, **extra})

    async def rpc(kind, method, params):
        body = {"jsonrpc": "2.0", "id": "r", "method": method, "params": params}
        return (await clients[kind].post("/a2a", json=body)).json()
    async def rows():
        query = "SELECT id, owner, history, artifacts, metadata, status, private_payload, public_visible FROM tasks"
        async with engine.connect() as conn:
            return (await conn.execute(sql(query))).all()
    async def restart():
        nonlocal handler, router, engine, store
        from tests.test_steer_idle_starts_turn import _operator_ctx

        # Preserve the durable crash image: SIGKILL cannot run generator cleanup,
        # whereas closing the test handler below gracefully cancels its executions.
        context = _operator_ctx()
        crashed = {sid: await store.scheduling_payloads(sid, context)
                   for sid in await ss.sessions_with_pending_turns(store, context)}
        await handler._active_task_registry.aclose()
        await router.drain()
        for sid, payloads in crashed.items():
            await store.update_scheduling_payloads(sid, context, lambda records: records.update(payloads))
        await engine.dispose()
        engine, store = await _partition(tmp_path)
        monkeypatch.setattr(STATE, "a2a_task_engine", engine)
        handler = DefaultRequestHandler(
            agent_executor=ProtoAgentExecutor(chat_mod._chat_langgraph_stream), task_store=store,
            agent_card=card, push_config_store=InMemoryPushNotificationConfigStore(),
        )
        router = hitl_routing.install_parked_task_routing(handler)
        app.router.routes[:] = [route for route in app.router.routes if route.path != "/a2a"]
        app.router.routes.extend(create_a2a_jsonrpc_routes(handler, rpc_url="/a2a"))
        steering._reset()
        chat_mod._RECOVERED_PRIVATE_TURNS.clear()
        chat_mod._PRIVATE_RUNS.clear()

    r.restart = restart
    r.steer, r.rpc, r.rows, r.clients, r.op, r.app = steer, rpc, rows, clients, clients["shared-bearer"], app
    yield r
    r.release.set()  # let a held turn finish before the store closes under it
    if detached := asyncio.all_tasks() - {asyncio.current_task()}:
        await asyncio.wait(detached, timeout=5)
    await router.drain()
    if cleanup := set(getattr(handler._active_task_registry, "_cleanup_tasks", ()) or ()):
        await asyncio.wait(cleanup, timeout=5)
    await asyncio.gather(*(client.aclose() for client in clients.values()))
    hitl_routing._ROUTER[0] = None
    steering._reset()
    await engine.dispose()


async def _assert_refused(rig, response, session, kind):
    """A refused private steer: a fixed 4xx, nothing stored, nothing queued, no turn started."""
    assert response.status_code == 403 and SECRET not in response.text
    if kind != "federation":  # federation never reaches the route: the /api operator ceiling answers
        assert response.json() == {"detail": REFUSED}
    assert not any(row.private_payload for row in await rig.rows())
    assert steering.pending_items(session) == [] and not any(SECRET in str(seen) for seen in rig.seen)


async def _assert_private_only(rig, session, task_id):
    """The canary's only home is the private partition; no peer read or ordinary queue holds it."""
    rows = await rig.rows()
    assert any(SECRET in (row.private_payload or "") for row in rows)
    assert all(SECRET not in str(tuple(row)[:6]) for row in rows)
    assert all(row.owner == "human-operator" for row in rows if REPLY in str(tuple(row)))
    assert steering.pending_items(session) == []
    for peer in PEERS:
        assert "error" in await rig.rpc(peer, "GetTask", {"id": task_id})
        listed = str(await rig.rpc(peer, "ListTasks", {"contextId": session, "includeArtifacts": True}))
        assert task_id not in listed and SECRET not in listed and REPLY not in listed
        assert SECRET not in (await rig.clients[peer].get(f"/api/chat/sessions/{session}/steer")).text


@pytest.mark.parametrize("kind", KINDS)
async def test_private_steer_into_an_idle_session(rig, kind):
    response = await rig.steer(kind, "chat-idle", SECRET, "m1", private=True)
    if kind in PEERS:
        return await _assert_refused(rig, response, "chat-idle", kind)
    body = response.json()
    assert response.status_code == 200 and body["private"] is True and body["pending"] == 0
    task = await poll_terminal(rig.op, body["started_turn"])
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    # The loopback message carried only the opaque reference; the trusted runner resolved it.
    assert [part["text"] for part in task["history"][0]["parts"]] == [body["private_ref"]]
    assert rig.seen == [(SECRET, False)]
    payload = next(row.private_payload for row in await rig.rows() if SECRET in (row.private_payload or ""))
    assert f'"reply": "{REPLY}"' in payload and '"dedupe": "m1"' in payload  # the reply is folded in privately
    await _assert_private_only(rig, "chat-idle", body["started_turn"])


@pytest.mark.parametrize("kind", HUMANS)
async def test_private_steer_into_a_parked_session_resumes_the_original_task(rig, kind):
    first = (await rig.steer(kind, "chat-park", f"{SECRET} park", "m1", private=True)).json()
    assert (await poll_terminal(rig.op, first["started_turn"]))["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    from tests.test_steer_idle_starts_turn import _operator_ctx
    store, context = chat_mod._private_partition_store(), _operator_ctx()
    (parked,) = await ss.pending_turns(store, "chat-park", context)
    assert parked["state"] == ss.STATE_PARKED and parked["interrupt_id"]
    answer = (await rig.steer(kind, "chat-park", f"{SECRET} answer", "m2", private=True)).json()
    assert answer["started_turn"] == first["started_turn"]  # the guarded resume, on the parked task
    assert (await poll_terminal(rig.op, answer["started_turn"]))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert rig.seen == [(f"{SECRET} park", False), (f"{SECRET} answer", True)]
    assert await ss.pending_turns(store, "chat-park", context) == []
    await _assert_private_only(rig, "chat-park", first["started_turn"])


@pytest.mark.parametrize("owner", ["shared-bearer", "federation"])
@pytest.mark.parametrize("kind", HUMANS)
async def test_private_steer_refuses_a_parked_task_outside_the_private_partition(rig, owner, kind):
    session = "chat-public-park"
    sent = await rig.rpc(owner, "SendMessage", {
        "message": {"messageId": "public-park", "contextId": session, "role": "ROLE_USER",
                    "parts": [{"text": "park"}]},
    })
    task_id = sent["result"]["task"]["id"]
    task = await poll_terminal(rig.clients[owner], task_id)
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    before = await rig.rows()
    assert before[0].owner == ("" if owner == "shared-bearer" else "federation")
    interrupt = await chat_mod._pending_interrupt({"configurable": {"thread_id": f"a2a:{session}"}})
    assert interrupt is not None

    response = await rig.steer(kind, session, SECRET, "private-answer", private=True)

    assert response.status_code == 409 and SECRET not in response.text
    assert response.json() == {"detail": "This conversation is waiting on a decision a private steer can't answer. Answer the decision first."}
    assert await rig.rows() == before  # no private row, new task, or changed parked task
    assert await poll_terminal(rig.clients[owner], task_id) == task
    assert await chat_mod._pending_interrupt({"configurable": {"thread_id": f"a2a:{session}"}}) == interrupt
    assert rig.seen == [("park", False)] and steering.pending_items(session) == []
    assert not steering.starting(session, "private-answer")


@pytest.mark.parametrize("flag,extra,status", [(False, {}, 403), (True, {"incognito": True}, 400),
                                               (True, {"images": [["image/png", "data:image/png;base64,AAA="]]}, 400)])
@pytest.mark.parametrize("kind", HUMANS)
async def test_private_steer_is_refused_and_never_downgraded(rig, kind, flag, extra, status):
    rig.config.private_steering_enabled = flag
    response = await rig.steer(kind, "chat-no", SECRET, "m1", **{"private": True, **extra})
    assert response.status_code == status and SECRET not in response.text
    assert await rig.rows() == [] and rig.seen == [] and steering.pending_items("chat-no") == []


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("extra", [{}, {"private": False}])
async def test_an_ordinary_steer_is_unchanged(rig, flag, extra):
    rig.config.private_steering_enabled = flag
    started = (await rig.steer("shared-bearer", "chat-pub", "hold on", "m1", **extra)).json()
    assert started == {"ok": True, "id": "m1", "started_turn": started["started_turn"], "pending": 0}
    await rig.running.wait()
    queued = (await rig.steer("shared-bearer", "chat-pub", "more", "m2", **extra)).json()
    assert queued == {"ok": True, "id": "m2", "pending": 1}
    rig.release.set()
    task = await poll_terminal(rig.op, started["started_turn"])
    assert task["history"][0]["parts"] == [{"text": "hold on"}] and "more" in rig.folded[0]["messages"][0].content
    (row,) = await rig.rows()  # one ordinary row, in the legacy NULL partition columns
    assert (row.owner, row.private_payload, row.public_visible) == ("", None, None)


@pytest.mark.parametrize("kind", KINDS)
async def test_private_steer_queues_behind_a_running_public_segment(rig, kind):
    public = (await rig.steer("shared-bearer", "chat-q1", "hold on", "p1")).json()["started_turn"]
    await rig.running.wait()
    response = await rig.steer(kind, "chat-q1", SECRET, "m1", private=True)
    if kind in PEERS:
        return await _assert_refused(rig, response, "chat-q1", kind)
    body = response.json()
    # Its own turn behind the running one: never the ordinary queue, never folded into it.
    assert body["started_turn"] != public and body["pending"] == 0 and rig.seen == [("hold on", False)]
    rig.release.set()
    assert SECRET not in str(await poll_terminal(rig.op, public)) and rig.folded == [None]
    await poll_terminal(rig.op, body["started_turn"])
    assert rig.seen == [("hold on", False), (SECRET, False)]
    await _assert_private_only(rig, "chat-q1", body["started_turn"])


@pytest.mark.parametrize("kind", KINDS)
async def test_public_steer_queues_behind_a_running_private_segment(rig, kind):
    private = (await rig.steer("shared-bearer", "chat-q2", f"{SECRET} hold", "m1", private=True)).json()
    await rig.running.wait()
    response = await rig.steer(kind, "chat-q2", "PUBLIC-STEER", "p1")
    if kind == "federation":  # as today: no federation credential reaches /api
        assert response.status_code == 403 and steering.pending_items("chat-q2") == []
    else:
        assert response.json() == {"ok": True, "id": "p1", "pending": 1}
    queued = steering.pending_items("chat-q2")
    rig.release.set()
    await poll_terminal(rig.op, private["started_turn"])
    # The private segment's model call folded nothing: the public steer waits for its own turn.
    assert rig.folded == [None] and steering.pending_items("chat-q2") == queued
    assert SECRET not in str(queued)


@pytest.mark.parametrize("kind", KINDS)
async def test_completion_race_and_duplicate_id_give_exactly_one_private_turn(rig, kind):
    await rig.steer("shared-bearer", "chat-race", "hold on", "p1")
    await rig.running.wait()
    rig.release.set()  # the public turn completes exactly as the duplicated private steer lands
    responses = await asyncio.gather(*(rig.steer(kind, "chat-race", SECRET, "m1", private=True) for _ in range(2)))
    if kind in PEERS:
        return await _assert_refused(rig, responses[0], "chat-race", kind)
    first, second = (response.json() for response in responses)
    assert first["started_turn"] == second["started_turn"]
    await poll_terminal(rig.op, first["started_turn"])
    retry = (await rig.steer(kind, "chat-race", SECRET, "m1", private=True)).json()
    assert retry["started_turn"] == first["started_turn"]  # a later retry joins it: no second turn
    assert [seen for seen in rig.seen if SECRET in seen[0]] == [(SECRET, False)]
    assert sum(1 for row in await rig.rows() if SECRET in (row.private_payload or "")) == 1
    await _assert_private_only(rig, "chat-race", first["started_turn"])
