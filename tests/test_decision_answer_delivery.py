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
import threading
import uuid
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
# Each test parks on its own thread id. The per-thread turn lock registry is process-global
# and its locks bind to the first event loop that contends on them, so a lock still alive from
# an earlier test's loop must never be handed to the next test (CI saw exactly that: "Lock …
# is bound to a different event loop" → failed delivery → 200 instead of 202).
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

    def __init__(self, fail_after_consume: bool = False, thread: str = THREAD):
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
        self.thread = thread
        self.config = {"configurable": {"thread_id": thread}}

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
                async with turn_control._thread_lock(self.thread):
                    cfg = self.config
                    if meta.get("decision_id"):  # the answer path; a dismissal only clears
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
        turn = Turn(fail_after_consume, thread=f"{THREAD}:{uuid.uuid4().hex}")
        monkeypatch.setattr(STATE, "graph", turn.graph)
        await turn.graph.ainvoke({}, turn.config)
        snapshot = await turn.graph.aget_state(turn.config)
        rec = store.create(SESSION, "Merge?", ["yes", "no"], thread_id=turn.thread,
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
    lock = turn_control._thread_lock(turn.thread)
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


@pytest.mark.asyncio
async def test_blank_option_id_is_rejected_not_sent(harness):
    store, build = harness
    turn, rec, client = await build()
    async with client:
        for body in ({"option_id": "   "}, {"option_id": "  ", "text": "ship it"}):
            r = await client.post(f"/api/decisions/{rec.id}/answer", json=body)
            assert r.status_code == 400, r.text
    assert turn.a2a_calls == []
    assert store.get(rec.id).pending


@pytest.mark.asyncio
async def test_cancel_waits_out_an_answer_in_flight(harness, monkeypatch):
    """A cancel arriving while an answer is mid-delivery must not withdraw the record under it."""
    store, build = harness
    turn, rec, client = await build()
    monkeypatch.setattr(decision_routes, "ACCEPT_WAIT_S", 0.2)
    lock = turn_control._thread_lock(turn.thread)
    await lock.acquire()  # the session is busy: the answer queues
    try:
        async with client:
            first = await client.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "yes"})
            assert first.status_code == 202
            cancel = await client.post(f"/api/decisions/{rec.id}/cancel")
            assert cancel.status_code == 409, cancel.text
            assert cancel.json()["detail"]["state"] == "pending"
            assert cancel.json()["detail"]["delivering"] is True
            assert store.get(rec.id).pending
            lock.release()
            await _until(lambda: store.get(rec.id).state == "answered")
            turn.release.set()
            await asyncio.wait_for(turn.finished.wait(), 2)
    finally:
        if lock.locked():
            lock.release()
    assert store.get(rec.id).answer == "yes"
    assert turn.a2a_calls == [rec.id]


@pytest.mark.asyncio
async def test_an_answer_arriving_mid_withdraw_waits_and_loses(harness, monkeypatch):
    """Interleave at the cancel route's one yield point: the withdraw is parked in its worker
    thread while an answer arrives and gets as far as it can. The answer must not claim and
    deliver to a decision being withdrawn; it waits for the dismissal and gets 409.

    Deterministic either way: the withdraw stays parked until the answer has either sent a
    resume to the agent (the bug) or come back to re-read the record while still waiting on
    the dismissal's claim (the fix) — never on a timer."""
    store, build = harness
    turn, rec, client = await build()
    loop = asyncio.get_running_loop()
    in_withdraw, progressed, go = asyncio.Event(), asyncio.Event(), threading.Event()
    reads = {"n": 0, "counting": False}
    real_withdraw, real_get = store.withdraw, store.get

    def paused_withdraw(*args, **kwargs):  # runs in asyncio.to_thread's worker
        loop.call_soon_threadsafe(in_withdraw.set)
        assert go.wait(5)
        return real_withdraw(*args, **kwargs)

    def counted_get(*args, **kwargs):
        row = real_get(*args, **kwargs)
        if reads["counting"]:
            reads["n"] += 1
            # 1: the answer route's first read. 2: the post-claim re-read (bug) or the first
            # re-read while waiting (fix). 3: only the waiting answer reads again.
            if reads["n"] >= 3:
                loop.call_soon_threadsafe(progressed.set)
        return row

    monkeypatch.setattr(store, "withdraw", paused_withdraw)
    monkeypatch.setattr(store, "get", counted_get)
    async with client:
        cancel = asyncio.create_task(client.post(f"/api/decisions/{rec.id}/cancel"))
        await asyncio.wait_for(in_withdraw.wait(), 2)
        reads["counting"] = True
        answer = asyncio.create_task(client.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "yes"}))
        await _until(lambda: progressed.is_set() or rec.id in turn.a2a_calls)
        turn.release.set()  # let the dismissal's clearing turn run to its end once it starts
        go.set()
        cancelled, answered = await asyncio.wait_for(asyncio.gather(cancel, answer), 3)
        await asyncio.wait_for(turn.finished.wait(), 2)
    assert rec.id not in turn.a2a_calls  # no answer was delivered to a decision being withdrawn
    assert cancelled.status_code == 200
    assert answered.status_code == 409, answered.text
    assert answered.json()["detail"]["state"] == "withdrawn"
    assert store.get(rec.id).state == "withdrawn"
    assert turn.a2a_calls == [None]  # only the dismissal's clearing resume reached the agent


# ── stranded records (#5107) ─────────────────────────────────────────────────────────────
# A record still `pending` whose checkpoint no longer holds its interrupt (the answer was
# consumed, then the process died before the record closed) must close itself, never sit
# answerable-but-dead in the console.


async def _strand(turn):
    """Consume the parked interrupt behind the ledger's back: the state a restart left."""
    turn.release.set()
    await turn.graph.ainvoke(Command(resume="Operator chose: yes (yes)"), turn.config)
    turn.finished.clear()
    turn.release.clear()


@pytest.mark.asyncio
async def test_answering_a_stranded_record_closes_it_and_returns_409(harness):
    store, build = harness
    turn, rec, client = await build()
    await _strand(turn)
    url = f"/api/decisions/{rec.id}/answer"
    async with client:
        r = await asyncio.wait_for(client.post(url, json={"option_id": "yes"}), 2)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["state"] == "withdrawn"
        assert "already moved past this question" in detail["withdraw_reason"]
        assert store.get(rec.id).state == "withdrawn"
        assert (await client.get("/api/decisions?state=pending")).json()["decisions"] == []
        again = await client.post(url, json={"option_id": "yes"})
        assert again.status_code == 409
    assert turn.a2a_calls == [rec.id]  # the retry never reached the agent


@pytest.mark.asyncio
async def test_dismiss_returns_without_waiting_for_the_clearing_turn(harness):
    """#5099's freeze must not come back through Dismiss: the clearing resume runs a turn
    (here it blocks in the slow step), and the cancel route answers before it ends."""
    store, build = harness
    turn, rec, client = await build()
    async with client:
        r = await asyncio.wait_for(client.post(f"/api/decisions/{rec.id}/cancel", json={"reason": ""}), 1)
        assert r.status_code == 200, r.text
        assert r.json()["decision"]["state"] == "withdrawn"
        await asyncio.wait_for(turn.running.wait(), 2)  # the clearing turn is under way
        assert not turn.finished.is_set()
        turn.release.set()
        await asyncio.wait_for(turn.finished.wait(), 2)
        again = await client.post(f"/api/decisions/{rec.id}/cancel")
        assert again.status_code == 409
        assert again.json()["detail"]["state"] == "withdrawn"
    assert len(turn.a2a_calls) == 1


@pytest.mark.asyncio
async def test_dismissing_a_stranded_record_sends_no_resume(harness):
    store, build = harness
    turn, rec, client = await build()
    await _strand(turn)
    async with client:
        r = await client.post(f"/api/decisions/{rec.id}/cancel")
        assert r.status_code == 200 and r.json()["decision"]["state"] == "withdrawn"
        await asyncio.sleep(0.05)
    assert turn.a2a_calls == []  # nothing parked → nothing to clear, no stray fresh turn


@pytest.mark.asyncio
async def test_boot_reconcile_closes_stranded_and_keeps_parked(tmp_path, monkeypatch):
    """Restart mid-resumed-turn: process A consumed one decision's answer and died before the
    record closed; another decision is genuinely still parked. Process B boots on the same
    durable checkpoint + ledger files: the maintenance loop's boot reconcile closes the first,
    leaves the second answerable."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    maintenance = importlib.import_module("server.maintenance_loops")
    ckpt, ledger = str(tmp_path / "checkpoints.db"), tmp_path / "decisions.db"

    class State(TypedDict):
        answer: str

    def choose(state):
        return {"answer": interrupt({"kind": "decision", "question": "Merge?", "options": ["yes", "no"]})}

    def compile_graph(saver):
        builder = StateGraph(State)
        builder.add_node("choose", choose)
        builder.add_edge(START, "choose")
        builder.add_edge("choose", END)
        return builder.compile(checkpointer=saver)

    # ── process A ──
    async with AsyncSqliteSaver.from_conn_string(ckpt) as saver:
        graph = compile_graph(saver)
        store = DecisionStore(ledger)
        recs = {}
        for name in ("stranded", "parked"):
            cfg = {"configurable": {"thread_id": f"t-{name}"}}
            await graph.ainvoke({}, cfg)
            iid = (await graph.aget_state(cfg)).tasks[0].interrupts[0].id
            recs[name] = store.create(f"s-{name}", "Merge?", ["yes", "no"], thread_id=f"t-{name}", interrupt_id=iid)
        # The answer is consumed... and the process dies before the ledger hears of it.
        await graph.ainvoke(Command(resume="yes"), {"configurable": {"thread_id": "t-stranded"}})
    assert store.get(recs["stranded"].id).pending

    # ── process B ──
    async with AsyncSqliteSaver.from_conn_string(ckpt) as saver:
        monkeypatch.setattr(STATE, "graph", compile_graph(saver))
        monkeypatch.setattr(STATE, "decision_store", DecisionStore(ledger))
        loop_task = asyncio.create_task(maintenance._decision_expiry_loop())
        try:
            await _until(lambda: not STATE.decision_store.get(recs["stranded"].id).pending)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task
        assert STATE.decision_store.get(recs["stranded"].id).state == "withdrawn"
        assert STATE.decision_store.get(recs["parked"].id).pending
        assert [d.id for d in STATE.decision_store.list(state="pending")] == [recs["parked"].id]


@pytest.mark.asyncio
async def test_unreadable_checkpoint_closes_nothing():
    from graph.decision_reconcile import interrupt_parked, reconcile_pending_decisions

    class Broken:
        async def aget_state(self, config):
            raise RuntimeError("checkpointer offline")

    store = DecisionStore(":memory:")
    rec = store.create("s", "Merge?", ["yes", "no"], thread_id="t", interrupt_id="i-1")
    legacy = store.create("s", "Old?", ["yes", "no"])  # no interrupt id: can't be judged
    assert await interrupt_parked(Broken(), rec) is None
    assert await reconcile_pending_decisions(store, Broken()) == 0
    assert await reconcile_pending_decisions(store, None) == 0
    assert store.get(rec.id).pending and store.get(legacy.id).pending
