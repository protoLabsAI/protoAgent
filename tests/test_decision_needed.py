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


async def _noop_resume(session_id, answer, decision_id):
    """A resume that does nothing (no real A2A self-POST in a unit test)."""
    STATE.decision_store.answer(decision_id, answer)
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
    async def _fake_resume(session_id, answer, decision_id):
        await _collect(
            chat_mod._run_native_turn(
                answer,
                session_id,
                {"configurable": {"thread_id": "t-s-dec"}},
                request_metadata={},
                resume=True,
            )
        )
        store.answer(decision_id, answer)
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _fake_resume)

    r = _client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "staging"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["state"] == STATE_ANSWERED
    assert body["decision"]["answer"] == "staging"
    assert body["resumed"] is True
    # The turn resumed and the tool received the chosen option as its return value.
    assert "staging" in fake.resume_values
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

    async def deliver(session_id, answer, decision_id):
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
