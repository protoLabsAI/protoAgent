"""Decision needed (#4725, items 1 + 3).

The agent's ``request_decision`` tool parks a turn at a choice it may not make on its own —
even on an UNATTENDED autonomous turn, the one exception to the auto-answer policy — and the
server records a durable pending decision. The operator lists + answers it over
``/api/decisions``, which resumes the parked turn with the chosen option.

Driven with deterministic model nodes, real in-memory graph checkpoints, a fake clock,
and an in-memory ``DecisionStore``; stream fakes also cover the driver event seam.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.decisions import (
    STATE_ANSWERED,
    STATE_PENDING,
    DecisionClosed,
    DecisionStore,
    normalize_options,
)
from graph.plugins import host
from operator_api import decision_routes
from runtime.state import STATE

# ``server.chat`` the attribute is shadowed by the re-exported ``chat`` function in
# server/__init__.py; resolve the actual submodules from sys.modules (as test_hitl_forms does).
chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")
turn_stream = importlib.import_module("server.turn_stream")


def _fixed_clock():
    return datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(monkeypatch) -> DecisionStore:
    """A fresh in-memory decisions ledger, injected as the live ``STATE.decision_store`` so the
    turn-driver park path and the routes both resolve it."""
    from graph import steering

    monkeypatch.setattr(steering, "_QUEUES", {})
    monkeypatch.setattr(steering, "_QUEUE_TOUCHED", {})
    s = DecisionStore(":memory:", clock=_fixed_clock)
    monkeypatch.setattr(STATE, "decision_store", s, raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", None, raising=False)
    return s


async def _collect(agen):
    return [frame async for frame in agen]


async def _noop_resume(session_id, answer, decision_id=None, *, resolution=None):
    """A resume that does nothing (no real A2A self-POST in a unit test). Answers the record
    for the answer path; a cancel-path call (no decision_id) only has to not blow up."""
    if decision_id:
        STATE.decision_store.answer(decision_id, (resolution or {}).get("answer", answer))
    return True


class _FakeDecisionStream:
    """Stand-in for ``_run_turn_stream``: the fresh pass yields a ``kind: "decision"`` HITL
    interrupt; a resumed pass completes, echoing the resume value so the test can see the
    chosen option flow back to the tool. Records every pass's ``resume_value``."""

    def __init__(self, options):
        self.options = options
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield (
                    "input_required",
                    {"kind": "decision", "question": "Which environment?", "options": self.options},
                )
            else:
                yield ("text", f"Deploying to {resume_value}.")
                yield ("__raw__", f"Deploying to {resume_value}.")

        return _gen()


class _FakeAskHumanStream:
    """Stand-in that asks a plain (non-decision) ``ask_human`` question, then answers once
    resumed — the pre-#4725 auto-answer behaviour on an autonomous turn."""

    def __init__(self):
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield ("input_required", {"question": "Which environment?"})
            else:
                yield ("text", "Proceeding with staging.")
                yield ("__raw__", "Proceeding with staging.")

        return _gen()


def _run_turn(message, session_id, *, resume=False, origin):
    cfg = {"configurable": {"thread_id": f"t-{session_id}"}}
    return asyncio.run(
        _collect(
            chat_mod._run_native_turn(
                message,
                session_id,
                cfg,
                request_metadata={"origin": origin},
                resume=resume,
            )
        )
    )


def _client() -> TestClient:
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    return TestClient(app)


# ── store ─────────────────────────────────────────────────────────────────────


def test_create_records_question_options_and_session(store):
    rec = store.create("s-1", "Which environment?", ["staging", "prod"], agent="protoEngineer")
    assert rec.session_id == "s-1"
    assert rec.question == "Which environment?"
    assert rec.state == STATE_PENDING
    assert rec.agent == "protoEngineer"
    assert rec.option_ids() == ["staging", "prod"]
    assert rec.created_at == "2026-11-08T12:00:00+00:00"  # the fake clock
    # Persisted and queryable as pending.
    assert [d.id for d in store.list(state="pending")] == [rec.id]


def test_normalize_options_shapes_strings_and_dicts_and_dedupes():
    opts = normalize_options(["yes", {"id": "no", "label": "No", "description": "stop"}, "yes"])
    assert opts[0] == {"id": "yes", "label": "yes"}
    assert opts[1] == {"id": "no", "label": "No", "description": "stop"}
    assert opts[2]["id"] == "yes-2"  # the clashing id is made unique


def test_answering_twice_is_refused(store):
    rec = store.create("s-1", "Go?", ["yes", "no"])
    store.answer(rec.id, "yes")
    assert store.get(rec.id).state == STATE_ANSWERED
    with pytest.raises(DecisionClosed):
        store.answer(rec.id, "no")  # a non-pending record refuses a second answer


def test_decision_changed_published_on_create_and_answer(store, monkeypatch):
    events: list = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    rec = store.create("s-1", "Go?", ["yes", "no"])
    store.answer(rec.id, "yes")
    topics = [t for t, _ in events]
    assert topics == ["decision.changed", "decision.changed"]
    assert events[0][1] == {"id": rec.id, "session_id": "s-1", "state": STATE_PENDING}
    assert events[1][1] == {"id": rec.id, "session_id": "s-1", "state": STATE_ANSWERED}


@pytest.mark.platform_sensitive
def test_disk_store_survives_reopen_and_cancel_publishes_once(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    path = tmp_path / "decisions.db"
    first = DecisionStore(path, clock=_fixed_clock)
    rec = first.create(
        "s-durable", "Deploy?", ["yes", "no"], agent="engineer",
        thread_id="a2a:s-durable", interrupt_id="interrupt-1", context="Production",
    )
    reopened = DecisionStore(path, clock=_fixed_clock)
    assert reopened.get(rec.id) == rec
    assert reopened.for_interrupt(rec.thread_id, rec.interrupt_id) == rec
    reopened.cancel(rec.id)
    with pytest.raises(DecisionClosed):
        first.answer(rec.id, "yes")
    assert first.get(rec.id).state == "cancelled"
    assert [data["state"] for _, data in events] == ["pending", "cancelled"]


@pytest.mark.platform_sensitive
def test_independent_writers_cannot_both_close_decision(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    events = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    path = tmp_path / "decisions.db"
    stores = [DecisionStore(path, clock=_fixed_clock) for _ in range(2)]
    # Independent locks model separate processes sharing only SQLite coordination.
    for writer in stores:
        writer._lock = threading.RLock()
    rec = stores[0].create("s-race", "Deploy?", ["yes", "no"])
    start = threading.Barrier(2)

    def close(index):
        start.wait(timeout=10)
        try:
            return stores[index].answer(rec.id, "yes") if index == 0 else stores[index].cancel(rec.id)
        except DecisionClosed:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(close, range(2)))
    winner, = [result for result in results if result is not None]
    assert stores[0].get(rec.id) == winner
    assert [data["state"] for _, data in events] == ["pending", winner.state]


@pytest.mark.parametrize("ignored", [False, True])
def test_failed_transition_rolls_back_without_publishing(store, monkeypatch, ignored):
    import sqlite3

    rec = store.create("s-rollback", "Deploy?", ["yes", "no"])
    events = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    db = store._connect()
    rejection = "RAISE(IGNORE)" if ignored else "RAISE(ABORT, 'simulated write failure')"
    db.execute(f"""
        CREATE TRIGGER reject_answer BEFORE UPDATE ON decisions
        BEGIN SELECT {rejection}; END
    """)
    with pytest.raises(DecisionClosed if ignored else sqlite3.IntegrityError):
        store.answer(rec.id, "yes")
    assert store.get(rec.id).pending
    assert not db.in_transaction
    assert events == []
    db.execute("DROP TRIGGER reject_answer")
    assert store.answer(rec.id, "no").answer == "no"
    assert len(events) == 1


# ── turn driver: park vs auto-answer ────────────────────────────────────────────


def test_unattended_decision_turn_parks_and_records(store, monkeypatch):
    # An unattended scheduled turn calling request_decision must PARK (not auto-answer) and
    # leave a pending record holding the question, options and session link.
    fake = _FakeDecisionStream(["staging", "prod"])
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)

    frames = _run_turn("deploy the app", "s-dec", origin="scheduler")

    kinds = [k for k, _ in frames]
    assert "input_required" in kinds  # parked for a human
    assert "done" not in kinds  # a parked turn yields no terminal answer
    assert fake.resume_values == [None]  # never auto-resumed with the sentinel

    pending = store.list(state="pending")
    assert len(pending) == 1
    rec = pending[0]
    assert rec.session_id == "s-dec"
    assert rec.question == "Which environment?"
    assert rec.option_ids() == ["staging", "prod"]


def test_unattended_ask_human_still_auto_answers(store, monkeypatch):
    # Regression: a NON-blocking ask_human on the same unattended origin keeps the existing
    # no-deadlock auto-answer — only a kind: "decision" interrupt parks.
    fake = _FakeAskHumanStream()
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)

    frames = _run_turn("run the deploy", "s-ask", origin="scheduler")

    kinds = [k for k, _ in frames]
    assert "input_required" not in kinds  # never parks an autonomous ask_human
    assert ("done", "Proceeding with staging.") in frames
    assert turn_control._AUTONOMOUS_HITL_SENTINEL in fake.resume_values  # auto-answered
    assert store.list(state="pending") == []  # and it wrote no decision record


# ── routes: answer, resume, validation, listing ─────────────────────────────────


def test_answer_route_resumes_parked_turn_with_chosen_option(store, monkeypatch):
    fake = _FakeDecisionStream(["staging", "prod"])
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)
    # Park an unattended turn first.
    _run_turn("deploy the app", "s-dec", origin="scheduler")
    rec = store.list(state="pending")[0]

    # The answer route resumes through the HITL-resume entry point — re-drive the turn with
    # the chosen option so the tool sees it come back (the production path self-POSTs to A2A).
    async def _fake_resume(session_id, answer, decision_id, *, resolution=None):
        await _collect(
            chat_mod._run_native_turn(
                answer,
                session_id,
                {"configurable": {"thread_id": "t-s-dec"}},
                request_metadata={},
                resume=True,
            )
        )
        store.answer(decision_id, (resolution or {}).get("answer", answer))
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _fake_resume)

    r = _client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "staging"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["state"] == STATE_ANSWERED
    assert body["decision"]["answer"] == "staging"
    assert body["resumed"] is True
    # The turn resumed and the tool received the chosen option as its return value.
    assert "Operator chose: staging (staging)" in fake.resume_values
    assert store.get(rec.id).state == STATE_ANSWERED


def test_answer_route_rejects_an_unknown_option(store):
    # Resume is never reached on a validation failure, so no self-POST fires.
    rec = store.create("s-1", "Which environment?", ["staging", "prod"])

    r = _client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "qa"})
    assert r.status_code == 400
    assert "qa" in r.json()["detail"]
    assert store.get(rec.id).state == STATE_PENDING  # untouched


def test_answer_route_refuses_a_second_answer(store, monkeypatch):
    rec = store.create("s-1", "Go?", ["yes", "no"])
    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _noop_resume)
    c = _client()
    assert c.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "yes"}).status_code == 200
    assert c.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "no"}).status_code == 409


def test_list_route_returns_pending_decisions(store):
    a = store.create("s-1", "Deploy?", ["yes", "no"])
    b = store.create("s-2", "Merge?", ["yes", "no"])
    store.answer(b.id, "yes")  # answered → not pending

    r = _client().get("/api/decisions?state=pending")
    assert r.status_code == 200
    body = r.json()
    ids = [d["id"] for d in body["decisions"]]
    assert ids == [a.id]  # only the pending one
    assert body["pending"] == 1


@pytest.fixture
def decision_graph(store, monkeypatch):
    """A real checkpoint and tool with deterministic model output, no gateway."""
    from typing import TypedDict
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import InMemorySaver
    from langchain_core.messages import AIMessage
    from tools.lg_tools import request_decision

    class State(TypedDict):
        messages: list
        session_id: str

    def model(state):
        chosen = request_decision.invoke({"question": "Deploy?", "options": ["yes", "no"]})
        return {"messages": [AIMessage(content=f"Chosen: {chosen}")]}

    builder = StateGraph(State)
    builder.add_node("model", model)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(STATE, "graph", graph)
    return graph


async def _sync_turn(origin="scheduler", *, resume=False, message="deploy"):
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from server.turn_sync import _native_turn

    return await _native_turn(
        message, None, session_id="s-real", config={"configurable": {"thread_id": "a2a:s-real"}},
        usage_cb=UsageMetadataCallbackHandler(), state_extra={}, tool_fence=None,
        hitl_resume=resume, incognito=False, origin=origin,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["scheduler", "api-chat", "v1", "plugin"])
async def test_sync_decision_parks_and_console_answer_closes(store, decision_graph, origin):
    reply = await _sync_turn(origin)
    assert "Input needed" in reply[0]["content"]
    rec, = store.list(state="pending")
    assert rec.interrupt_id
    assert rec.thread_id == "a2a:s-real"
    reply = await _sync_turn("api-chat", resume=True, message="yes")
    assert "Chosen: yes" in reply[0]["content"]
    assert store.get(rec.id).answer == "yes"
    assert store.list(state="pending") == []


@pytest.mark.asyncio
async def test_failed_delivery_is_retryable_and_stale_answer_cannot_resume_next_pause(
    store, decision_graph, monkeypatch
):
    import httpx
    from langgraph.types import Command

    await _sync_turn()
    rec, = store.list(state="pending")
    config = {"configurable": {"thread_id": rec.thread_id}}
    attempts = 0

    async def deliver(session_id, answer, decision_id, *, resolution=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        await turn_control.validate_decision_resume(config, decision_id)
        async with turn_control.decision_resume(config, answer):
            await decision_graph.ainvoke(Command(resume=await chat_mod._resume_payload(config, answer)), config)
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", deliver)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/decisions/{rec.id}/answer"
        first = await client.post(url, json={"option_id": "yes"})
        assert first.json()["resumed"] is False
        assert store.get(rec.id).pending
        second = await client.post(url, json={"option_id": "yes"})
        assert second.json()["resumed"] is True
        await _sync_turn()
        next_rec, = store.list(state="pending")
        assert next_rec.interrupt_id != rec.interrupt_id
        assert (await client.post(url, json={"option_id": "no"})).status_code == 409
        with pytest.raises(DecisionClosed):
            await turn_control.validate_decision_resume(config, rec.id)
        assert store.get(next_rec.id).pending


@pytest.mark.asyncio
async def test_failed_graph_resume_keeps_decision_pending(store, decision_graph):
    await _sync_turn()
    rec, = store.list(state="pending")
    with pytest.raises(RuntimeError):
        async with turn_control.decision_resume({"configurable": {"thread_id": rec.thread_id}}, "yes"):
            raise RuntimeError("transport failed before graph invocation")
    assert store.get(rec.id).pending


@pytest.mark.asyncio
async def test_scheduler_message_does_not_abandon_decision(store, decision_graph):
    await _sync_turn()
    rec, = store.list(state="pending")
    reply = await _sync_turn(message="scheduled again")
    assert "Input needed first" in reply[0]["content"]
    assert store.get(rec.id).pending
    await turn_control.validate_decision_resume({"configurable": {"thread_id": rec.thread_id}}, rec.id)


@pytest.mark.asyncio
async def test_hold_stops_parking_autonomous_turns_once_decision_is_cancelled(store, monkeypatch):
    # The regression the reviewer caught: a CANCELLED decision's checkpoint interrupt lingers,
    # and the autonomous hold would keep parking every later scheduler/inbox/webhook turn —
    # wedging the session. The hold must park only while a pending record backs the interrupt.
    thread_id = "a2a:s-wedge"
    config = {"configurable": {"thread_id": thread_id}}
    payload = {"kind": "decision", "question": "Deploy?", "options": ["yes", "no"]}

    async def _fake_pending(_cfg):
        return ("interrupt-x", payload)  # the stale decision interrupt stays on the checkpoint

    monkeypatch.setattr(chat_mod, "_pending_interrupt", _fake_pending)
    rec = store.create("s-wedge", "Deploy?", ["yes", "no"], thread_id=thread_id, interrupt_id="interrupt-x")

    async def _hold():
        return await turn_control._hold_if_hitl_pending(
            "scheduled fire", "s-wedge", config, request_metadata={"origin": "scheduler"}
        )

    # While pending, the autonomous turn is HELD on the decision (parks for a human).
    assert await _hold() == payload

    # Once cancelled, the SAME autonomous turn is no longer held — it proceeds (returns None),
    # so the lingering interrupt can't block every subsequent scheduled turn.
    store.cancel(rec.id)
    assert await _hold() is None

    # An already-answered decision is just as stale — also no longer parks an autonomous turn.
    rec2 = store.create("s-wedge", "Deploy?", ["yes"], thread_id=thread_id, interrupt_id="interrupt-y")
    store.answer(rec2.id, "yes")

    async def _fake_pending_answered(_cfg):
        return ("interrupt-y", payload)

    monkeypatch.setattr(chat_mod, "_pending_interrupt", _fake_pending_answered)
    assert await _hold() is None


@pytest.mark.asyncio
async def test_cancel_route_closes_record_and_clears_parked_turn(store, monkeypatch):
    # The cancel route must mark the record cancelled AND resume the parked turn to clear the
    # checkpoint interrupt (no decision_id — the record is already closed, so it only unblocks).
    rec = store.create("s-cancel", "Deploy?", ["yes", "no"])
    cleared: dict = {}

    async def _fake_resume(session_id, answer, decision_id=None):
        cleared.update(session_id=session_id, answer=answer, decision_id=decision_id)
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _fake_resume)

    r = _client().post(f"/api/decisions/{rec.id}/cancel")
    assert r.status_code == 200
    assert r.json()["decision"]["state"] == "withdrawn"
    assert store.get(rec.id).state == "withdrawn"
    # It resumed the parked turn to clear the interrupt, with NO decision_id (skips the
    # validate_decision_resume gate, which would reject the now-cancelled record).
    assert cleared["session_id"] == "s-cancel"
    assert cleared["decision_id"] is None
    assert cleared["answer"] == decision_routes.CANCELLED_RESUME


def test_cancel_route_rejects_unknown_and_already_closed(store, monkeypatch):
    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _noop_resume)
    c = _client()
    assert c.post("/api/decisions/nope/cancel").status_code == 404
    rec = store.create("s-1", "Go?", ["yes", "no"])
    store.answer(rec.id, "yes")
    assert c.post(f"/api/decisions/{rec.id}/cancel").status_code == 409


@pytest.mark.asyncio
async def test_streaming_task_resume_closes_matching_record(store, decision_graph):
    await _sync_turn()
    rec, = store.list(state="pending")
    frames = await _collect(chat_mod._run_native_turn(
        "no", "s-real", {"configurable": {"thread_id": rec.thread_id}},
        request_metadata={}, resume=True,
    ))
    assert any(kind == "done" for kind, _ in frames)
    assert store.get(rec.id).answer == "no"
    assert store.list(state="pending") == []


@pytest.mark.asyncio
async def test_goal_driven_sync_decision_parks(store, decision_graph, monkeypatch):
    from types import SimpleNamespace
    from server import goal_loop

    monkeypatch.setattr(goal_loop, "active_goal", lambda sid: SimpleNamespace(iteration=1))
    reply = await _sync_turn("api-chat")
    assert "Input needed" in reply[0]["content"]
    rec, = store.list(state="pending")
    assert rec.interrupt_id


@pytest.mark.asyncio
async def test_decision_pause_is_idempotent_and_keeps_context(store, decision_graph):
    await _sync_turn()
    rec, = store.list(state="pending")
    config = {"configurable": {"thread_id": rec.thread_id}}
    payload = await chat_mod._pending_interrupt_value(config)
    again = await turn_control.record_decision_pause("s-real", payload, config)
    assert again.id == rec.id
    assert len(store.list()) == 1
    other = store.create("s-other", "Go?", ["yes"], context="Production deploy")
    assert store.get(other.id).context == "Production deploy"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no-port", "transport", "timeout", "rpc-error"])
async def test_resume_transport_failure_does_not_close_record(store, monkeypatch, failure):
    import httpx

    rec = store.create("s-1", "Go?", ["yes"])
    monkeypatch.setattr(STATE, "active_port", None if failure == "no-port" else 7871)

    async def post(*args, **kwargs):
        assert kwargs["json"]["params"]["message"]["metadata"]["decision_id"] == rec.id
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out")
        if failure == "transport":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"error": {"code": -32602, "message": "stale"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await decision_routes._resume_parked_turn(rec.session_id, "yes", rec.id) is False
    assert store.get(rec.id).pending


@pytest.mark.asyncio
async def test_sync_goal_continuation_parks_decision(store, decision_graph, monkeypatch):
    from server import goal_loop
    from langchain_core.messages import AIMessage

    class InitialReplyGraph:
        calls = 0

        async def aget_state(self, config):
            return await decision_graph.aget_state(config)

        async def ainvoke(self, value, config):
            self.calls += 1
            if self.calls == 1:
                return {"messages": [AIMessage(content="Working on it.")]}
            return await decision_graph.ainvoke(value, config)

    async def steps(self):
        yield goal_loop.GoalContinuation("Continue", self.config)
        pytest.fail("goal drive continued past a decision pause")

    monkeypatch.setattr(STATE, "graph", InitialReplyGraph())
    monkeypatch.setattr(goal_loop.GoalDrive, "steps", steps)
    reply = await _sync_turn()
    assert "Input needed" in reply[0]["content"]
    rec, = store.list(state="pending")
    assert rec.interrupt_id


@pytest.mark.asyncio
async def test_scoped_goal_decision_resolves_its_checkpoint(store, decision_graph):
    config = {"configurable": {"thread_id": "a2a:s-real:goal-iter-2"}}
    await decision_graph.ainvoke({"messages": [], "session_id": "s-real"}, config)
    payload = await chat_mod._pending_interrupt_value(config)
    rec = await turn_control.record_decision_pause("s-real", payload, config)
    resolved = await turn_control.validate_decision_resume(
        {"configurable": {"thread_id": "a2a:s-real"}}, rec.id, "s-real"
    )
    assert resolved == config
    with pytest.raises(DecisionClosed):
        await turn_control.validate_decision_resume(config, rec.id, "another-session")


@pytest.mark.asyncio
@pytest.mark.parametrize("new_pause", [False, True])
async def test_delayed_a2a_decision_answer_never_runs_as_fresh_or_later_input(
    store, decision_graph, monkeypatch, new_pause
):
    from graph.config import LangGraphConfig

    await _sync_turn()
    rec, = store.list(state="pending")
    await _sync_turn("api-chat", resume=True, message="yes")
    if new_pause:
        await _sync_turn()
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())

    async def unexpected_stream(*args, **kwargs):
        pytest.fail("a stale decision answer reached the graph")
        yield  # make this a stream with the graph's interface

    monkeypatch.setattr(decision_graph, "astream_events", unexpected_stream)
    frames = await _collect(chat_mod._chat_langgraph_stream(
        "no", "s-real", request_metadata={"hitl_resume": True, "decision_id": rec.id}
    ))
    assert any(kind == "error" for kind, _ in frames)
    assert store.get(rec.id).answer == "yes"
    assert len(store.list(state="pending")) == int(new_pause)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["api-chat", "scheduler"])
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize("fail_store", [False, True])
async def test_decision_payload_identity_across_pause_paths(
    store, decision_graph, monkeypatch, caplog, origin, continuation, driver, fail_store
):
    """Both drivers carry the committed id on initial and goal-continuation pauses."""
    from langchain_core.messages import AIMessage
    from server import goal_loop

    config = {"configurable": {"thread_id": "a2a:s-real"}}
    original_stream = turn_stream._run_turn_stream
    original_invoke = decision_graph.ainvoke
    calls = 0

    async def initial_reply_then_invoke(value, config):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"messages": [AIMessage(content="Working on it.")]}
        return await original_invoke(value, config)

    async def initial_reply_then_stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield ("text", "Working on it.")
            yield ("__raw__", "Working on it.")
        else:
            async for frame in original_stream(*args, **kwargs):
                yield frame

    async def steps(self):
        yield goal_loop.GoalContinuation("Continue", self.config)
        pytest.fail("goal drive continued after parking")

    if continuation:
        monkeypatch.setattr(goal_loop.GoalDrive, "steps", steps)
        monkeypatch.setattr(decision_graph, "ainvoke", initial_reply_then_invoke)
        monkeypatch.setattr(turn_stream, "_run_turn_stream", initial_reply_then_stream)

    create = store.create

    def record_under_lock(*args, **kwargs):
        assert turn_control._thread_lock(config["configurable"]["thread_id"]).locked()
        if fail_store:
            raise OSError("ledger unavailable")
        return create(*args, **kwargs)

    monkeypatch.setattr(store, "create", record_under_lock)
    if driver == "sync":
        reply = await _sync_turn(origin)
        assert "Input needed" in reply[0]["content"]
        payload = reply[0]["hitl"]
    else:
        async with turn_control._thread_lock(config["configurable"]["thread_id"]):
            frames = await _collect(chat_mod._run_native_turn(
                "deploy", "s-real", config, request_metadata={"origin": origin}
            ))
        assert not any(kind == "done" for kind, _ in frames)
        payload, = [value for kind, value in frames if kind == "input_required"]

    checkpoint = await chat_mod._pending_interrupt_value(config)
    assert payload["kind"] == "decision"
    assert "decision_id" not in checkpoint  # enrichment must not mutate graph state
    if fail_store:
        assert payload == checkpoint
        assert store.list() == []
        assert "could not record a parked decision" in caplog.text
    else:
        rec, = store.list(state="pending")
        assert payload == {**checkpoint, "decision_id": rec.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["api-chat", "scheduler"])
async def test_repeated_hold_preserves_decision_identity(store, decision_graph, monkeypatch, origin):
    from graph.config import LangGraphConfig

    first = await _sync_turn(origin)
    rec, = store.list(state="pending")
    assert first[0]["hitl"]["decision_id"] == rec.id
    repeated = await _sync_turn(origin, message="another message")
    assert repeated[0]["hitl"] == first[0]["hitl"]
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())
    frames = await _collect(chat_mod._chat_langgraph_stream(
        "another streamed message", "s-real", request_metadata={"origin": origin}
    ))
    payload, = [value for kind, value in frames if kind == "input_required"]
    assert payload == first[0]["hitl"]
    assert [row.id for row in store.list()] == [rec.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["Which environment?", {"question": "Go?"},
                                      {"kind": "form", "title": "Details", "steps": []},
                                      {"kind": "approval", "title": "Approve?"}])
async def test_identity_enrichment_preserves_generic_hitl(store, payload):
    assert await turn_control.decision_pause_payload("s", payload, {}) is payload
    assert store.list() == []


@pytest.mark.asyncio
async def test_executor_persists_decision_identity_for_replay(store, decision_graph, tmp_path):
    import httpx
    import protolabs_a2a as pa
    from a2a.server.tasks import DatabaseTaskStore
    from a2a.server.context import ServerCallContext
    from a2a_impl.executor import HITL_MIME
    from a2a_impl.stores import make_sqlite_engine
    from google.protobuf.json_format import MessageToDict
    from tests._a2a_app import build_app, drain_handlers, poll_terminal, send_msg

    emitted = []

    async def stream(text, session_id, **kwargs):
        config = {"configurable": {"thread_id": f"a2a:{session_id}"}}
        async with turn_control._thread_lock(config["configurable"]["thread_id"]):
            async for frame in chat_mod._run_native_turn(text, session_id, config, request_metadata={}):
                if frame[0] == "input_required":
                    emitted.append(frame[1])
                yield frame

    db = str(tmp_path / "tasks.db")
    engine = make_sqlite_engine(db)
    task_store = DatabaseTaskStore(engine)
    await task_store.initialize()
    try:
        app = build_app(stream, task_store=task_store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await send_msg(client)
            task = response.json()["result"]["task"]
            final = await poll_terminal(client, task["id"])
        assert final["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        rec, = store.list(state="pending")
        assert emitted[0]["decision_id"] == rec.id
    finally:
        await drain_handlers()
        await engine.dispose()

    engine = make_sqlite_engine(db)
    try:
        reopened = DatabaseTaskStore(engine)
        await reopened.initialize()
        saved = await reopened.get(task["id"], ServerCallContext())
        wire = MessageToDict(saved)
        assert wire["status"] == final["status"]
        payloads = [data for part in wire["status"]["message"]["parts"]
                    for mime, data in [pa.read_data(part)] if mime == HITL_MIME]
        assert payloads == emitted
        assert payloads[0]["decision_id"] == rec.id
    finally:
        await engine.dispose()



@pytest.mark.asyncio
async def test_simultaneous_answers_resume_once_and_the_loser_gets_409(store, monkeypatch):
    """Release acceptance bd-nfd6g.17 D3: two surfaces answering at the same moment both got
    200 and the loser's resume created a stray failed task. Now the second answer waits for the
    first to settle, sends no resume of its own, and gets 409 with the winning record."""
    import asyncio

    import httpx

    rec = store.create("s-race", "Colour?", ["red", "blue"])
    calls: list[str] = []

    async def slow_resume(session_id, answer, decision_id=None, *, resolution=None):
        calls.append((resolution or {}).get("answer", answer))
        await asyncio.sleep(0.3)  # the resumed run takes a moment before the record closes
        STATE.decision_store.answer(decision_id, (resolution or {}).get("answer", answer))
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", slow_resume)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/decisions/{rec.id}/answer"
        a, b = await asyncio.gather(
            client.post(url, json={"option_id": "red"}),
            client.post(url, json={"option_id": "blue"}),
        )
    codes = sorted([a.status_code, b.status_code])
    assert codes == [200, 409]
    assert len(calls) == 1  # exactly one resume was sent
    winner = a if a.status_code == 200 else b
    loser = b if winner is a else a
    assert loser.json()["detail"]["state"] == STATE_ANSWERED
    assert loser.json()["detail"]["answer"] == winner.json()["decision"]["answer"] == calls[0]


@pytest.mark.asyncio
async def test_a_waiting_answer_delivers_when_the_first_fails_delivery(store, monkeypatch):
    """If the answer in flight fails delivery (the record stays pending), the waiting one is
    not refused: it delivers its own answer."""
    import asyncio

    import httpx

    rec = store.create("s-retry", "Go?", ["yes", "no"])
    calls: list[str] = []

    async def flaky_resume(session_id, answer, decision_id=None, *, resolution=None):
        chosen = (resolution or {}).get("answer", answer)
        calls.append(chosen)
        await asyncio.sleep(0.2)
        if len(calls) == 1:
            return False  # transport failure: the record stays pending
        STATE.decision_store.answer(decision_id, chosen)
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", flaky_resume)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/decisions/{rec.id}/answer"
        first, second = await asyncio.gather(
            client.post(url, json={"option_id": "yes"}),
            client.post(url, json={"option_id": "no"}),
        )
    # Whichever went first failed delivery (still retryable, 200 resumed=false); the waiting
    # one then delivered its own answer.
    assert first.status_code == second.status_code == 200
    assert sorted([first.json()["resumed"], second.json()["resumed"]]) == [False, True]
    assert len(calls) == 2
    assert store.get(rec.id).answer == calls[1]


def test_in_memory_store_reads_are_safe_across_threads():
    """The :memory: store shares one connection across threads (routes read it via
    asyncio.to_thread). Concurrent unlocked reads raised sqlite3.InterfaceError ("bad parameter
    or other API misuse") and flaked test_simultaneous_answers_… on CI; reads now take the lock."""
    import threading

    s = DecisionStore(":memory:", clock=_fixed_clock)
    rec = s.create("s-threads", "Go?", ["yes", "no"])
    errors: list[BaseException] = []

    def hammer():
        try:
            for _ in range(400):
                assert s.get(rec.id) is not None
                s.list(state="pending")
                s.for_interrupt("t", "i")
        except BaseException as exc:  # noqa: BLE001 — collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
