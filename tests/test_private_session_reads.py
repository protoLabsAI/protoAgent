"""Verified caller audiences on every per-session read; private replies stay partitioned."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from runtime.state import STATE
from tests._a2a_app import poll_terminal
from tests import test_private_steer_route as private_routes
from tests.test_private_steer_route import HUMANS, KINDS, REPLY, SECRET

rig = private_routes.rig

pytestmark = [pytest.mark.asyncio, pytest.mark.platform_sensitive]


@pytest.fixture
async def session(rig, monkeypatch, request):
    flag, private = request.param
    rig.config.private_steering_enabled = flag
    sid = "chat-reads"
    public = (await rig.steer("shared-bearer", sid, "ordinary", "public")).json()["started_turn"]
    await poll_terminal(rig.op, public)
    task_id = public
    if private:
        task_id = (await rig.steer("shared-bearer", sid, SECRET, "private", private=True)).json()["started_turn"]
        await poll_terminal(rig.op, task_id)
    # The model seam does not write checkpoints; seed the resolved conversation that
    # a real runner leaves, then exercise the real Markdown exporter.
    STATE.graph.snapshot = SimpleNamespace(
        values={"messages": [HumanMessage(content=SECRET if private else "ordinary"),
                             AIMessage(content=REPLY if private else "ok")]},
        tasks=(), interrupts=(),
    )
    monkeypatch.setattr(STATE, "checkpointer", object())
    return sid, public, task_id, private


CASES = [(True, True), (True, False), (False, False)]
CALLERS = KINDS + ("legacy-api-key", "open")


@pytest.fixture
def caller(rig, session, kind):
    from a2a_impl import auth

    if kind in KINDS:
        return rig.clients[kind]
    # Earn these identities through the same middleware, after operator intake.
    client = rig.clients["fleet-service"]
    client.headers.pop("Authorization", None)
    auth.configure(bearer_token="", federation_token="", fleet_token="",
                   api_key="legacy" if kind == "legacy-api-key" else "", allowed_origins_raw="")
    if kind == "legacy-api-key":
        client.headers["X-API-Key"] = "legacy"
    return client


@pytest.mark.parametrize("session", CASES, indirect=True)
@pytest.mark.parametrize("kind", CALLERS)
async def test_turns(caller, session, kind):
    sid, public, task_id, private = session
    response = await caller.get(f"/api/chat/sessions/{sid}/turns")
    if kind == "federation":
        assert response.status_code == 403  # existing operator API ceiling
        return
    assert response.status_code == 200
    body = response.json()
    assert public in response.text
    if private and kind not in HUMANS:
        assert task_id not in response.text and SECRET not in response.text and REPLY not in response.text
        assert len(body["turns"]) == 1
    elif private:
        assert task_id in response.text and REPLY in response.text
    else:
        assert len(body["turns"]) == 1 and body["turns"][0]["text"] == "ok"


@pytest.mark.parametrize("session", CASES, indirect=True)
@pytest.mark.parametrize("kind", CALLERS)
async def test_export(caller, session, kind):
    sid, _, _, private = session
    response = await caller.get(f"/api/chat/sessions/{sid}/export")
    if kind == "federation" or (private and kind not in HUMANS):
        assert response.status_code == 403
        assert SECRET not in response.text and REPLY not in response.text
        if kind != "federation":
            assert response.json() == {"detail": "Session read unavailable."}
    else:
        assert response.status_code == 200
        markdown = response.json()["markdown"]
        assert (SECRET if private else "ordinary") in markdown
        assert (REPLY if private else "ok") in markdown


@pytest.mark.parametrize("session", CASES, indirect=True)
@pytest.mark.parametrize("kind", CALLERS)
async def test_steer_started(caller, session, kind):
    sid, public, task_id, private = session
    response = await caller.get(f"/api/chat/sessions/{sid}/steer")
    if kind == "federation":
        assert response.status_code == 403
        return
    assert response.status_code == 200
    expected = {"public": public}
    if private and kind in HUMANS:
        expected["private"] = task_id
    assert response.json() == {"pending": [], "drained": [], "started": expected}


@pytest.mark.parametrize("session", CASES, indirect=True)
@pytest.mark.parametrize("kind", CALLERS)
async def test_summary(caller, session, kind, monkeypatch):
    from operator_api import chat_routes

    sid, _, task_id, private = session
    # Retained live state must not reintroduce a private id after the SQL filter.
    real_live = chat_routes.live_state_fields
    if private:
        monkeypatch.setattr(chat_routes, "live_state_fields",
                            lambda *a, **kw: {**real_live(*a, **kw), "live_task_id": task_id})
    response = await caller.get(f"/api/chat/sessions/{sid}")
    if kind == "federation":
        assert response.status_code == 403
        return
    assert response.status_code == 200
    assert response.json()["turn_count"] == (2 if private and kind in HUMANS else 1)
    if private and kind not in HUMANS:
        assert task_id not in response.text and SECRET not in response.text and REPLY not in response.text
    elif private:
        assert response.json()["live_task_id"] == task_id


@pytest.mark.parametrize("session", [(True, True)], indirect=True)
async def test_reply_partition_and_operator_sdk_reads(rig, session):
    sid, _, task_id, _ = session
    from a2a.server.context import ServerCallContext
    from operator_api.chat_routes import _private_partition_store

    store = _private_partition_store()
    context = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    task = await store.get(task_id, context)
    task.status.message.CopyFrom(task.history[0])
    task.status.message.parts[0].text = SECRET
    await store.save(task, context)
    recovered = await store.get(task_id, context)
    assert recovered.status.message.parts[0].text == SECRET
    rows = await rig.rows()
    assert any(REPLY in (row.private_payload or "") for row in rows)
    assert all(REPLY not in str((row.artifacts, row.status)) for row in rows)
    assert all(SECRET not in str((row.artifacts, row.status)) for row in rows)
    for kind in HUMANS:
        got = await rig.rpc(kind, "GetTask", {"id": task_id})
        listed = await rig.rpc(kind, "ListTasks", {"contextId": sid, "includeArtifacts": True})
        assert REPLY in str(got) and REPLY in str(listed)


@pytest.mark.parametrize("session", [(True, True)], indirect=True)
@pytest.mark.parametrize("suffix", ["local-turns", "turns/missing", "delegations", "directives"])
async def test_unpartitioned_reads_fail_closed(rig, session, suffix):
    sid, _, _, _ = session
    response = await rig.clients["fleet-service"].get(f"/api/chat/sessions/{sid}/{suffix}")
    assert response.status_code == 403
    assert response.json() == {"detail": "Session read unavailable."}


@pytest.mark.parametrize("session", [(True, True)], indirect=True)
async def test_read_failure_never_falls_back_to_private_live_state(rig, session, monkeypatch):
    from operator_api import chat_routes

    sid, _, task_id, _ = session

    async def unreadable(*args, **kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(chat_routes, "session_summary", unreadable)
    monkeypatch.setattr(chat_routes, "live_state_fields", lambda *a, **kw: {"live_task_id": task_id})
    response = await rig.clients["fleet-service"].get(f"/api/chat/sessions/{sid}")
    assert response.status_code == 200 and response.json()["live_task_id"] is None
    assert task_id not in response.text


@pytest.mark.parametrize("session", [(True, True)], indirect=True)
async def test_missing_store_cannot_authorize_checkpoint_export(rig, session, monkeypatch):
    sid, _, _, _ = session
    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    response = await rig.clients["fleet-service"].get(f"/api/chat/sessions/{sid}/export")
    assert response.status_code == 503
    assert response.json() == {"detail": "Session read unavailable."}


@pytest.mark.parametrize("session", [(True, True)], indirect=True)
@pytest.mark.parametrize("bubble", [SECRET, REPLY])
async def test_checkpoint_recovery_and_rewind_read_the_partition(rig, session, monkeypatch, bubble):
    """Internal readers lose nothing when the reply leaves the plain columns: a retired
    checkpoint is reseeded with the private prompt and reply, and rewind still matches
    the private turn's bubbles."""
    from unittest.mock import AsyncMock

    from server.chat_rehydrate import mark_turns_rewound, recovered_messages

    sid, _, task_id, _ = session
    rows = await rig.rows()
    assert all(REPLY not in str((row.history, row.artifacts)) for row in rows)
    monkeypatch.setattr(STATE.graph, "aget_state", AsyncMock(return_value=SimpleNamespace(values={}, config={})),
                        raising=False)
    monkeypatch.setattr(STATE, "history_store", None, raising=False)
    config = {"configurable": {"thread_id": sid}}
    assert [m.content for m in await recovered_messages(config, sid)] == ["ordinary", "ok", SECRET, REPLY]

    # Either bubble of the private turn (its prompt, its reply) resolves to that turn.
    assert await mark_turns_rewound(sid, content=bubble, occurrence=0, before=True) == 1
    assert [m.content for m in await recovered_messages(config, sid)] == ["ordinary", "ok"]
    flagged = [row.id for row in await rig.rows() if "protoagent_rewound" in str(row.metadata)]
    assert flagged == [task_id]
