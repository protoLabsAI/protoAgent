"""REST turn visibility, cancellation, and same-session recovery (#4205)."""
import asyncio
import importlib

import httpx
import pytest
from fastapi import FastAPI

from runtime import local_turns
from runtime.state import STATE
from server import turn_control, turn_sync


@pytest.fixture
def app(monkeypatch):
    import operator_api.chat_routes as routes
    chat = importlib.import_module("server.chat")
    monkeypatch.setattr(STATE, "graph", object())
    monkeypatch.setattr(STATE, "graph_config", None)
    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    monkeypatch.setattr(chat, "_note_agent_active", lambda *args: None)
    monkeypatch.setattr(chat._turn_telemetry, "record_local_turn", lambda *args, **kw: None)
    application = FastAPI()
    routes.register_chat_routes(application, ui="none")
    return application


@pytest.mark.asyncio
async def test_rest_turn_cancel_then_same_session_model_switch(app, monkeypatch):
    entered = asyncio.Event()
    unwound = asyncio.Event()
    holders = []

    async def provider(message, session_id, *, model=None, **kwargs):
        async with turn_control._thread_lock("a2a:" + session_id):
            assert not holders
            holders.append(model)
            try:
                if model == "exhausted":
                    entered.set()
                    await asyncio.Event().wait()
                return [{"role": "assistant", "content": "recovered"}]
            finally:
                holders.pop()
                unwound.set()

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/api/chat", json={"message": "hi", "session_id": "same", "model": "exhausted"}))
        await asyncio.wait_for(entered.wait(), 5)
        turns = (await client.get("/api/chat/sessions/same/turns")).json()["local_turns"]
        assert len(turns) == 1 and turns[0]["origin"] == "api-chat"
        turn_id = turns[0]["id"]
        wrong = await client.post(f"/api/chat/sessions/other/local-turns/{turn_id}/cancel")
        assert wrong.json() == {"stopped": False}
        assert not pending.done()
        stopped = await client.post(f"/api/chat/sessions/same/local-turns/{turn_id}/cancel")
        assert stopped.json() == {"stopped": True}
        assert unwound.is_set()
        failure = await asyncio.wait_for(pending, 5)
        assert failure.status_code == 409
        assert failure.json()["detail"]["code"] == "turn_cancelled"
        assert not local_turns.snapshot("same")
        response = await asyncio.wait_for(client.post("/api/chat", json={"message": "continue", "session_id": "same", "model": "working"}), 5)
        assert response.status_code == 200
        assert response.json()["response"] == "recovered"
        assert not turn_control._thread_lock("a2a:same").locked()


@pytest.mark.asyncio
async def test_disconnect_unwinds_driver_and_registry(monkeypatch):
    from operator_api.chat_routes import _run_disconnectable_chat

    gone = asyncio.Event()
    entered = asyncio.Event()
    lock = asyncio.Lock()

    class Request:
        async def receive(self):
            await gone.wait()
            return {"type": "http.disconnect"}

    @local_turns.tracked
    async def driver(message, session_id):
        async with lock:
            entered.set()
            await asyncio.Event().wait()

    pending = asyncio.create_task(_run_disconnectable_chat(Request(), driver("hi", "disconnect")))
    await asyncio.wait_for(entered.wait(), 5)
    gone.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    assert not lock.locked()
    assert not local_turns.snapshot("disconnect")


@pytest.mark.asyncio
async def test_v1_disconnect_keeps_turn_visible_until_explicit_stop(app, monkeypatch):
    from operator_api.chat_routes import _run_v1_turn

    entered = asyncio.Event()

    async def provider(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    request = asyncio.create_task(_run_v1_turn("hello", "v1-session", origin="v1"))
    await asyncio.wait_for(entered.wait(), 5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    rows = local_turns.snapshot("v1-session")
    assert len(rows) == 1 and rows[0]["origin"] == "v1"
    assert await local_turns.cancel("v1-session", rows[0]["id"])
    assert not local_turns.snapshot("v1-session")


@pytest.mark.asyncio
async def test_cancel_timeout_does_not_claim_stop_or_interrupt_cleanup():
    cleaning = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()

    @local_turns.tracked
    async def driver(message, session_id):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    running = asyncio.create_task(driver("hi", "cleanup"))
    await entered.wait()
    turn_id = local_turns.snapshot("cleanup")[0]["id"]
    with pytest.raises(TimeoutError):
        await local_turns.cancel("cleanup", turn_id, timeout=0.01)
    assert cleaning.is_set()
    assert local_turns.snapshot("cleanup")[0]["cancel_requested"] is True
    second = asyncio.create_task(local_turns.cancel("cleanup", turn_id))
    await asyncio.sleep(0)
    assert not running.done() and not second.done()
    release.set()
    assert await asyncio.wait_for(second, 5)
    assert (await running)[0]["error"]["type"] == "turn_cancelled"
    assert not local_turns.snapshot("cleanup")


@pytest.mark.asyncio
async def test_queued_turn_can_be_cancelled_without_canceling_lock_holder(app, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    lock = asyncio.Lock()

    async def provider(message, session_id, **kwargs):
        async with lock:
            entered.set()
            await release.wait()
            return [{"role": "assistant", "content": message}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", provider)
    chat = importlib.import_module("server.chat")
    holder = asyncio.create_task(chat.chat("first", "queued"))
    await entered.wait()
    queued = asyncio.create_task(chat.chat("second", "queued"))
    await asyncio.sleep(0)
    rows = local_turns.snapshot("queued")
    assert len(rows) == 2
    assert await local_turns.cancel("queued", rows[1]["id"])
    assert (await queued)[0]["error"]["type"] == "turn_cancelled"
    assert lock.locked() and not holder.done()
    release.set()
    assert (await holder)[0]["content"] == "first"
    assert not local_turns.snapshot("queued")


@pytest.mark.parametrize("first", ["stop", "disconnect", "request_cancel"])
@pytest.mark.asyncio
async def test_stop_and_disconnect_preserve_inflight_cleanup(first):
    from operator_api.chat_routes import _run_disconnectable_chat

    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    gone = asyncio.Event()
    lock = asyncio.Lock()

    class Request:
        async def receive(self):
            await gone.wait()
            return {"type": "http.disconnect"}

    @local_turns.tracked
    async def driver(message, session_id):
        async with lock:
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                completed.set()

    pending = asyncio.create_task(_run_disconnectable_chat(Request(), driver("hi", "cleanup-race")))
    await asyncio.wait_for(entered.wait(), 5)
    turn_id = local_turns.snapshot("cleanup-race")[0]["id"]
    stop = None
    try:
        if first == "stop":
            stop = asyncio.create_task(local_turns.cancel("cleanup-race", turn_id))
        elif first == "disconnect":
            gone.set()
        else:
            pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        if stop is None:
            stop = asyncio.create_task(local_turns.cancel("cleanup-race", turn_id))
        gone.set()
        # Let the disconnect and Stop handlers both reach the cleanup wait.
        for _ in range(10):
            await asyncio.sleep(0)
        # An ASGI task can also be canceled while draining the disconnected turn.
        pending.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert lock.locked()
        assert local_turns.snapshot("cleanup-race")
        assert not stop.done() and not pending.done()
        assert not completed.is_set()
        release.set()
        assert await asyncio.wait_for(stop, 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert completed.is_set()
        assert not lock.locked()
        assert not local_turns.snapshot("cleanup-race")
    finally:
        release.set()
        await asyncio.gather(pending, *([stop] if stop else []), return_exceptions=True)
