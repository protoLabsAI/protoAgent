"""Detached submit-then-poll /api/chat turns (#4277 slice 1).

A ``detach=true`` turn runs as a background task that survives the client disconnecting,
shows up in ``/turns`` with ``live_task_id``, and is polled by id. The non-detached path
keeps its #4205 cancel-on-disconnect behaviour (guarded by tests/test_local_turn_control.py,
left untouched)."""
import asyncio
import importlib

import httpx
import pytest
from fastapi import FastAPI

from runtime import local_turns
from runtime.state import STATE
from server import turn_sync


@pytest.fixture(autouse=True)
def _clean_registries():
    """Isolate the in-memory live/finished turn maps between tests."""
    local_turns._ACTIVE.clear()
    local_turns._FINISHED.clear()
    local_turns._DETACHED_TASKS.clear()
    yield
    local_turns._ACTIVE.clear()
    local_turns._FINISHED.clear()
    local_turns._DETACHED_TASKS.clear()


@pytest.fixture
def app(monkeypatch):
    import operator_api.chat_routes as routes
    chat = importlib.import_module("server.chat")
    monkeypatch.setattr(STATE, "graph", object())
    monkeypatch.setattr(STATE, "graph_config", None)
    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    monkeypatch.setattr(chat, "_note_agent_active", lambda *a: None)
    monkeypatch.setattr(chat._turn_telemetry, "record_local_turn", lambda *a, **k: None)
    application = FastAPI()
    routes.register_chat_routes(application, ui="none")
    return application


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def _poll(client, sid, turn_id):
    for _ in range(500):
        got = (await client.get(f"/api/chat/sessions/{sid}/turns/{turn_id}")).json()
        if got["state"] != "working":
            return got
        await asyncio.sleep(0.01)
    raise AssertionError(f"turn {turn_id} never left 'working'")


@pytest.mark.asyncio
async def test_detach_202_survives_disconnect_and_completes(app, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(message, session_id, **kwargs):
        entered.set()
        await release.wait()
        return [{"role": "assistant", "content": "detached-done"}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    sid = "det-ok"

    # The POST happens on a client that closes right after the 202 — the client is gone
    # while the turn is still running, the disconnect this feature must survive.
    async with _client(app) as poster:
        r = await poster.post("/api/chat", json={"message": "SECRET-PROMPT", "session_id": sid, "detach": True})
    assert r.status_code == 202
    body = r.json()
    assert body["session_id"] == sid and body["state"] == "working" and body["turn_id"]
    turn_id = body["turn_id"]

    async with _client(app) as client:
        await asyncio.wait_for(entered.wait(), 5)
        assert not release.is_set()  # 202 returned before the turn finished

        # While running: it is the live turn and polling says 'working'.
        turns = (await client.get(f"/api/chat/sessions/{sid}/turns")).json()
        assert turns["live_task_id"] == turn_id
        row = next(t for t in turns["local_turns"] if t["id"] == turn_id)
        assert row["live"] is True and row["origin"] == "api-chat"
        assert (await client.get(f"/api/chat/sessions/{sid}/turns/{turn_id}")).json()["state"] == "working"

        release.set()
        done = await _poll(client, sid, turn_id)
        assert done["state"] == "completed"
        assert done["response"] == "detached-done"
        assert done["messages"] == [{"role": "assistant", "content": "detached-done"}]
        # The prompt is never stored (r6): it appears nowhere in the poll payload.
        assert "SECRET-PROMPT" not in str(done)

        # Finished → no longer live.
        after = (await client.get(f"/api/chat/sessions/{sid}/turns")).json()
        assert after["live_task_id"] is None and after["local_turns"] == []


@pytest.mark.asyncio
async def test_detach_in_band_failure_reports_failed(app, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(message, session_id, **kwargs):
        entered.set()
        await release.wait()
        return [{"role": "assistant", "content": "**Error:** boom",
                 "error": {"type": "server_error", "message": "boom", "upstream_status": None}}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    sid = "det-fail"
    async with _client(app) as client:
        turn_id = (await client.post("/api/chat", json={"message": "x", "session_id": sid, "detach": True})).json()["turn_id"]
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        done = await _poll(client, sid, turn_id)
        assert done["state"] == "failed"
        assert done["error"]["type"] == "server_error" and done["error"]["message"] == "boom"


@pytest.mark.asyncio
async def test_detach_raised_exception_reports_failed_with_turn_error_shape(app, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(message, session_id, **kwargs):
        entered.set()
        await release.wait()
        raise ValueError("kaboom")

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    sid = "det-raise"
    async with _client(app) as client:
        turn_id = (await client.post("/api/chat", json={"message": "x", "session_id": sid, "detach": True})).json()["turn_id"]
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        done = await _poll(client, sid, turn_id)
        assert done["state"] == "failed"
        # turn_error shape: the raised exception is carried structurally.
        assert done["error"]["message"] == "kaboom" and done["error"]["exception"] == "ValueError"
        assert done["error"]["type"] == "server_error"


@pytest.mark.asyncio
async def test_detach_operator_cancel_reports_canceled(app, monkeypatch):
    entered = asyncio.Event()

    async def provider(message, session_id, **kwargs):
        entered.set()
        await asyncio.Event().wait()  # blocks until cancelled

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    sid = "det-cancel"
    async with _client(app) as client:
        turn_id = (await client.post("/api/chat", json={"message": "x", "session_id": sid, "detach": True})).json()["turn_id"]
        await asyncio.wait_for(entered.wait(), 5)
        stopped = await client.post(f"/api/chat/sessions/{sid}/local-turns/{turn_id}/cancel")
        assert stopped.json() == {"stopped": True}
        done = (await client.get(f"/api/chat/sessions/{sid}/turns/{turn_id}")).json()
        assert done["state"] == "canceled"
        assert done["error"]["type"] == "turn_cancelled"


@pytest.mark.asyncio
async def test_unknown_turn_id_is_404(app):
    async with _client(app) as client:
        r = await client.get("/api/chat/sessions/whatever/turns/nope")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_non_detached_call_is_unchanged(app, monkeypatch):
    """detach defaults False: the synchronous path still returns the turn body inline, so a
    finished record is NOT kept and the turn is not pollable after it returns (#4205 contract)."""
    async def provider(message, session_id, **kwargs):
        return [{"role": "assistant", "content": "inline"}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    async with _client(app) as client:
        r = await client.post("/api/chat", json={"message": "hi", "session_id": "sync-1"})
        assert r.status_code == 200 and r.json()["response"] == "inline"
        assert not local_turns._FINISHED  # synchronous turns leave no retained outcome


def test_finished_records_capped_by_count(monkeypatch):
    monkeypatch.setattr(local_turns, "_clock", lambda: 1000.0)
    for n in range(local_turns._MAX_FINISHED + 5):
        turn = local_turns.LocalTurn(id=f"t{n}", session_id="s", origin="api-chat", task=None)  # type: ignore[arg-type]
        local_turns._record(turn, "completed", response="r", messages=[], error=None)
    assert len(local_turns._FINISHED) == local_turns._MAX_FINISHED
    assert local_turns.get_turn("s", "t0") is None  # oldest evicted
    assert local_turns.get_turn("s", f"t{local_turns._MAX_FINISHED + 4}")["state"] == "completed"


def test_finished_records_expire_by_ttl(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(local_turns, "_clock", lambda: clock["t"])
    turn = local_turns.LocalTurn(id="old", session_id="s", origin="api-chat", task=None)  # type: ignore[arg-type]
    local_turns._record(turn, "completed", response="r", messages=[], error=None)
    assert local_turns.get_turn("s", "old")["state"] == "completed"
    clock["t"] += local_turns._FINISHED_TTL_S + 1
    assert local_turns.get_turn("s", "old") is None  # aged out


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_error", [None, {"message": "Reconnect the provider."}])
async def test_detach_graphless_response_matches_synchronous(app, monkeypatch, auth_error):
    monkeypatch.setattr(STATE, "graph", None)
    monkeypatch.setattr(STATE, "graph_auth_error", auth_error)
    async with _client(app) as client:
        payload = {"message": "SECRET-PROMPT", "session_id": "graphless"}
        inline = await client.post("/api/chat", json=payload)
        submitted = await client.post("/api/chat", json={**payload, "detach": True})
        assert inline.status_code == 200
        assert submitted.status_code == 202
        turn_id = submitted.json()["turn_id"]
        done = await _poll(client, "graphless", turn_id)
        assert done == {"turn_id": turn_id, "state": "completed",
                        "response": inline.json()["response"], "messages": inline.json()["messages"]}
        assert "SECRET-PROMPT" not in str(done)
        assert not local_turns.snapshot("graphless")


@pytest.mark.asyncio
@pytest.mark.parametrize("detach", [False, True])
async def test_queued_local_turn_does_not_replace_running_stop_target(app, monkeypatch, detach):
    from server import turn_control

    entered = asyncio.Event()
    queued = asyncio.Event()
    second_entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(message, session_id, **kwargs):
        if message == "second":
            queued.set()
        async with turn_control._thread_lock("a2a:" + session_id):
            if message == "first":
                entered.set()
                await asyncio.Event().wait()
            second_entered.set()
            await release.wait()
            return [{"role": "assistant", "content": "second done"}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    sid = "queue"
    async with _client(app) as client:
        first = asyncio.create_task(client.post("/api/chat", json={
            "message": "first", "session_id": sid, "detach": detach}))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(client.post("/api/chat", json={
            "message": "second", "session_id": sid, "detach": detach}))
        try:
            await asyncio.wait_for(queued.wait(), 5)
            rows = local_turns.snapshot(sid)
            assert len(rows) == 2  # detached inner drivers must not register duplicates
            turns = (await client.get(f"/api/chat/sessions/{sid}/turns")).json()
            assert turns["live_task_id"] == rows[0]["id"]
            assert [row["live"] for row in turns["local_turns"]] == [True, False]
            stopped = await client.post(f"/api/chat/sessions/{sid}/local-turns/{turns['live_task_id']}/cancel")
            assert stopped.json() == {"stopped": True}
            await asyncio.wait_for(second_entered.wait(), 5)
            after = (await client.get(f"/api/chat/sessions/{sid}/turns")).json()
            assert after["live_task_id"] == rows[1]["id"]
            assert after["local_turns"][0]["live"] is True
            if detach:
                assert (await _poll(client, sid, rows[0]["id"]))["state"] == "canceled"
            else:
                assert (await first).status_code == 409
            release.set()
            await asyncio.gather(first, second)
            if detach:
                assert (await _poll(client, sid, rows[1]["id"]))["state"] == "completed"
        finally:
            release.set()
            for row in local_turns.snapshot(sid):
                await local_turns.cancel(sid, row["id"])
            await asyncio.gather(first, second)


def test_a2a_live_turn_retains_precedence_over_local_queue():
    from operator_api.chat_routes import _mark_local_live

    rows = [{"id": "running"}, {"id": "queued"}]
    assert _mark_local_live(rows, {"task_id": "a2a"}) is None
    assert all(row["live"] is False for row in rows)


@pytest.mark.asyncio
async def test_detached_id_is_registered_and_cancelable_before_driver_starts():
    entered = False

    async def driver():
        nonlocal entered
        entered = True
        await asyncio.Event().wait()

    turn_id = local_turns.start_detached(driver(), session_id="early", origin="api-chat",
                                         error_adapter=lambda exc: {"message": str(exc)})
    assert local_turns.get_turn("early", turn_id) == {"turn_id": turn_id, "state": "working"}
    assert await local_turns.cancel("early", turn_id)
    assert not entered
    assert local_turns.get_turn("early", turn_id)["state"] == "canceled"
    assert not local_turns.snapshot("early")


@pytest.mark.asyncio
async def test_detached_failure_before_tracked_graph_driver_is_retained():
    from server.chat import turn_error

    async def driver():
        raise ValueError("before graph")

    turn_id = local_turns.start_detached(driver(), session_id="early-fail", origin="api-chat",
                                         error_adapter=turn_error)
    await asyncio.gather(*local_turns._DETACHED_TASKS)
    done = local_turns.get_turn("early-fail", turn_id)
    assert done["state"] == "failed"
    assert done["error"] == turn_error(ValueError("before graph"))
    assert not local_turns.snapshot("early-fail")


@pytest.mark.asyncio
async def test_detached_child_task_keeps_its_own_registration():
    entered = asyncio.Event()
    release = asyncio.Event()

    @local_turns.tracked
    async def child(message, session_id, **kwargs):
        entered.set()
        await release.wait()
        return [{"role": "assistant", "content": "child done"}]

    async def parent():
        return await asyncio.create_task(child("private", "child", origin="plugin"))

    turn_id = local_turns.start_detached(parent(), session_id="parent", origin="api-chat",
                                         error_adapter=lambda exc: {"message": str(exc)})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        child_rows = local_turns.snapshot("child")
        assert len(child_rows) == 1
        assert child_rows[0]["id"] != turn_id
        assert child_rows[0]["origin"] == "plugin"
        assert local_turns.snapshot("parent")[0]["id"] == turn_id
    finally:
        release.set()
        await asyncio.gather(*local_turns._DETACHED_TASKS)
    assert local_turns.get_turn("parent", turn_id)["state"] == "completed"
    assert not local_turns.snapshot("child")
