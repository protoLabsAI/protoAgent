"""Answer delivery is acknowledged on consumption and is idempotent end to end (#5099).

These drive the REAL loopback delivery (``graph.decision_resume.resume_parked_turn``) into a
fake ``/a2a`` endpoint that does what the native chat driver does — take the session lock,
``validate_decision_resume``, then ``decision_resume`` around a LangGraph run — so the guards
under test are the production ones. The resumed turn is a real graph whose second node blocks
on an event (a slow model step), and the loopback transport enforces httpx's read timeout on a
shrunk clock (120s → a fraction of a second) WITHOUT cancelling the server-side turn, which is
what a real client timeout does.
"""

import asyncio
import importlib
import logging
from typing import TypedDict

import httpx
import pytest
from fastapi import FastAPI, Request
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from graph.decisions import DecisionStore
from operator_api import decision_routes
from runtime.state import STATE

turn_control = importlib.import_module("server.turn_control")

RealAsyncClient = httpx.AsyncClient
THREAD = "test:delivery"
SESSION = "session-delivery"


class _ShrunkTimeoutASGI(httpx.AsyncBaseTransport):
    """ASGI transport that honours the request's read timeout on a shrunk clock.

    The app call runs in its own task: a client-side timeout abandons the response but the
    server keeps running the turn, exactly like a real socket read timeout."""

    def __init__(self, app, scale: float):
        self.inner = httpx.ASGITransport(app=app)
        self.scale = scale
        self.running: set[asyncio.Task] = set()

    async def handle_async_request(self, request):
        call = asyncio.create_task(self.inner.handle_async_request(request))
        self.running.add(call)
        call.add_done_callback(self.running.discard)
        read = (request.extensions.get("timeout") or {}).get("read")
        if read is None:
            return await asyncio.shield(call)
        try:
            return await asyncio.wait_for(asyncio.shield(call), read * self.scale)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout("timed out", request=request) from None


class Turn:
    """The parked graph plus the knobs a test turns: hold the slow step, fail after consuming."""

    def __init__(self, fail_after_consume: bool = False):
        self.release = asyncio.Event()
        self.running = asyncio.Event()
        self.finished = asyncio.Event()
        self.fail_after_consume = fail_after_consume
        self.a2a_calls: list[str] = []

        class State(TypedDict):
            answer: str

        def choose(state):
            return {"answer": interrupt({"kind": "decision", "question": "Merge?", "options": ["yes", "no"]})}

        async def slow(state):
            self.running.set()
            await self.release.wait()  # the long part of the resumed turn (a merge, a watch…)
            if self.fail_after_consume:
                raise RuntimeError("model gateway went away mid-turn")
            return {}

        builder = StateGraph(State)
        builder.add_node("choose", choose)
        builder.add_node("slow", slow)
        builder.add_edge(START, "choose")
        builder.add_edge("choose", "slow")
        builder.add_edge("slow", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.config = {"configurable": {"thread_id": THREAD}}

    def a2a_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/a2a")
        async def a2a(request: Request):
            body = await request.json()
            message = body["params"]["message"]
            meta = message["metadata"]
            self.a2a_calls.append(meta.get("decision_id"))
            answer = message["parts"][0]["text"]
            try:
                async with turn_control._thread_lock(THREAD):
                    cfg = await turn_control.validate_decision_resume(
                        self.config, meta["decision_id"], message["contextId"])
                    resolution = {**(meta.get("decision_resolution") or {}), "id": meta["decision_id"]}
                    cfg = {**cfg, "configurable": {**cfg["configurable"], "decision_resolution": resolution}}
                    async with turn_control.decision_resume(cfg, answer):
                        await self.graph.ainvoke(Command(resume=answer), cfg)
                state, text = "TASK_STATE_COMPLETED", "done"
            except Exception as exc:  # noqa: BLE001 — the executor turns a crash into a failed task
                state, text = "TASK_STATE_FAILED", str(exc)
            finally:
                self.finished.set()
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"task": {
                "id": "task-1", "contextId": message["contextId"],
                "status": {"state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text}]}},
            }}}

        return app


@pytest.fixture
async def harness(monkeypatch):
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    monkeypatch.setattr(STATE, "active_port", 7871)

    async def build(*, fail_after_consume=False, scale=0.001):
        turn = Turn(fail_after_consume)
        monkeypatch.setattr(STATE, "graph", turn.graph)
        await turn.graph.ainvoke({}, turn.config)
        snapshot = await turn.graph.aget_state(turn.config)
        rec = store.create(SESSION, "Merge?", ["yes", "no"], thread_id=THREAD,
                           interrupt_id=snapshot.tasks[0].interrupts[0].id)
        transport = _ShrunkTimeoutASGI(turn.a2a_app(), scale)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: RealAsyncClient(transport=transport, **kw))
        app = FastAPI()
        decision_routes.register_decision_routes(app)
        client = RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        return turn, rec, client

    yield store, build


async def _until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_slow_turn_past_transport_timeout_answers_on_consume_and_logs_its_end(harness, caplog):
    store, build = harness
    turn, rec, client = await build()
    caplog.set_level(logging.INFO, logger="protoagent.decisions")
    async with client:
        r = await asyncio.wait_for(client.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "yes"}), 2)
        assert r.status_code == 200, r.text
        assert r.json()["resumed"] is True
        assert r.json()["decision"]["state"] == "answered"
        assert store.get(rec.id).state == "answered"
        assert (await client.get("/api/decisions?state=pending")).json()["decisions"] == []
        await asyncio.wait_for(turn.running.wait(), 2)
        # Let the shrunk 120s transport deadline pass while the turn is still running.
        await asyncio.sleep(0.3)
        assert not turn.finished.is_set()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
        turn.release.set()
        await asyncio.wait_for(turn.finished.wait(), 2)
        await _until(lambda: "finished" in caplog.text)
    assert store.get(rec.id).state == "answered"
    assert turn.a2a_calls == [rec.id]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


@pytest.mark.asyncio
async def test_turn_failing_after_consume_keeps_the_answer_and_is_logged(harness, caplog):
    store, build = harness
    turn, rec, client = await build(fail_after_consume=True)
    caplog.set_level(logging.INFO, logger="protoagent.decisions")
    async with client:
        r = await asyncio.wait_for(client.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "no"}), 2)
        assert r.status_code == 200 and r.json()["decision"]["state"] == "answered"
        turn.release.set()
        await asyncio.wait_for(turn.finished.wait(), 2)
        await _until(lambda: "model gateway went away" in caplog.text)
    failure = [r for r in caplog.records if "model gateway went away" in r.getMessage()]
    assert failure and failure[0].levelno == logging.WARNING
    assert rec.id in failure[0].getMessage()
    # The answer was consumed: the later failure never reopens the decision.
    assert store.get(rec.id).state == "answered"
    assert store.get(rec.id).answer == "no"


@pytest.mark.asyncio
async def test_answer_queued_behind_a_busy_session_is_on_its_way_not_failed(harness, monkeypatch):
    """The session is busy (another turn holds the lock) past the transport timeout. The answer
    is neither reported as failed nor sent twice: the route says it's on its way, retries join
    the delivery in flight, and once the lock frees the one delivery consumes it."""
    store, build = harness
    turn, rec, client = await build()
    monkeypatch.setattr(decision_routes, "ACCEPT_WAIT_S", 0.3, raising=False)
    lock = turn_control._thread_lock(THREAD)
    await lock.acquire()
    url = f"/api/decisions/{rec.id}/answer"
    try:
        async with client:
            first = await asyncio.wait_for(client.post(url, json={"option_id": "yes"}), 2)
            assert first.status_code == 202, first.text
            assert first.json()["resumed"] is False
            assert first.json()["delivering"] is True
            assert first.json()["decision"]["state"] == "pending"
            assert (await client.get(f"/api/decisions/{rec.id}")).json()["delivering"] is True
            # Try again while it's still queued: no second delivery, same honest answer.
            retry = await asyncio.wait_for(client.post(url, json={"option_id": "yes"}), 2)
            assert retry.status_code == 202, retry.text
            assert retry.json()["delivering"] is True
            assert turn.a2a_calls == [rec.id]
            lock.release()
            await _until(lambda: store.get(rec.id).state == "answered")
            after = await client.post(url, json={"option_id": "yes"})
            assert after.status_code == 409
            assert after.json()["detail"]["answer"] == "yes"
            assert (await client.get(f"/api/decisions/{rec.id}")).json()["delivering"] is False
            turn.release.set()
            await asyncio.wait_for(turn.finished.wait(), 2)
    finally:
        if lock.locked():
            lock.release()
    assert turn.a2a_calls == [rec.id]


@pytest.mark.asyncio
async def test_retry_after_consumption_returns_the_record_and_starts_no_turn(harness):
    store, build = harness
    turn, rec, client = await build()
    url = f"/api/decisions/{rec.id}/answer"
    async with client:
        assert (await asyncio.wait_for(client.post(url, json={"option_id": "yes"}), 2)).status_code == 200
        for option in ("yes", "no"):
            again = await client.post(url, json={"option_id": option})
            assert again.status_code == 409
            assert again.json()["detail"]["state"] == "answered"
            assert again.json()["detail"]["answer"] == "yes"
        turn.release.set()
        await asyncio.wait_for(turn.finished.wait(), 2)
    assert turn.a2a_calls == [rec.id]
