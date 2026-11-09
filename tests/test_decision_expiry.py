"""Expiry and cross-surface decision contracts, with a fake clock and real checkpoints."""

import importlib
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.decision_expiry import sweep_expired_decisions
from graph.decisions import DecisionClosed, DecisionStore
from operator_api import decision_routes
from runtime.state import STATE
from tools.lg_tools import request_decision, withdraw_decision

turn_control = importlib.import_module("server.turn_control")
chat = importlib.import_module("server.chat")
OPTIONS = [{"id": "hold", "label": "Keep holding", "recommended": True}, {"id": "go", "label": "Release"}]


@pytest.fixture
def ledger(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 8, 12, tzinfo=UTC))
    store = DecisionStore(":memory:", clock=lambda: clock.now)
    monkeypatch.setattr(STATE, "decision_store", store)
    return store, clock


def client():
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.device_id = "device-josh"
        return await call_next(request)

    decision_routes.register_decision_routes(app)
    return TestClient(app)


@pytest.mark.parametrize(
    "fields,match",
    [
        ({"expires_in_minutes": 5}, "both"),
        ({"default_option": "hold"}, "both"),
        ({"default_action": "keep holding"}, "requires an expiry"),
        ({"expires_in_minutes": 0, "default_option": "hold"}, "positive"),
        ({"expires_in_minutes": 5, "default_option": "missing"}, "option id"),
        ({"options": [{"id": "a", "recommended": True}, {"id": "b", "recommended": True}]}, "at most one"),
    ],
)
def test_tool_validation(fields, match):
    result = request_decision.invoke({"question": "Release?", "options": OPTIONS, **fields})
    assert result.startswith("Error:")
    assert match in result


def test_get_fields_and_sort_order(ledger):
    store, clock = ledger
    oldest = store.create("s", "First?", OPTIONS)
    clock.now += timedelta(seconds=1)
    later = store.create("s", "Second?", OPTIONS)
    slow = store.create("s", "Slow?", OPTIONS, expires_in_minutes=10, default_option="hold")
    fast = store.create(
        "s", "Fast?", OPTIONS, expires_in_minutes=2, default_option="hold", default_action="keep holding the release"
    )
    c = client()
    for query in ("", "?state=pending"):
        rows = c.get("/api/decisions" + query).json()["decisions"]
        assert [r["id"] for r in rows] == [fast.id, slow.id, oldest.id, later.id]
    data = c.get(f"/api/decisions/{fast.id}").json()
    assert data["default_action"] == data["default_action_text"] == "keep holding the release"
    assert data["options"][0]["recommended"] is True
    assert data["answered_at"] is data["answered_by"] is data["answered_surface"] is None
    assert c.get(f"/api/decisions/{slow.id}").json()["default_action_text"] == "carry on without this step"


def test_withdraw_tool_and_cancel_reason(ledger, monkeypatch):
    store, _ = ledger
    rec = store.create("s", "Release?", OPTIONS)
    assert withdraw_decision.invoke({"decision_id": rec.id, "state": {"session_id": "other"}}).startswith("Error:")
    assert store.get(rec.id).pending
    assert "withdrawn" in withdraw_decision.invoke(
        {"decision_id": rec.id, "reason": "Build failed", "state": {"session_id": "s"}}
    )
    assert store.get(rec.id).withdraw_reason == "Build failed"
    assert withdraw_decision.invoke({"decision_id": rec.id, "state": {"session_id": "s"}}).startswith("Error:")
    calls = []

    async def resume(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", resume)
    rec = store.create("s", "Release?", OPTIONS)
    c = client()
    data = c.post(f"/api/decisions/{rec.id}/cancel", json={"reason": "Superseded"}).json()["decision"]
    assert data["state"] == "withdrawn"
    assert data["withdraw_reason"] == "Superseded"
    assert c.get(f"/api/decisions/{rec.id}").json()["withdraw_reason"] == "Superseded"
    assert len(calls) == 1
    assert c.post(f"/api/decisions/{rec.id}/cancel").status_code == 409


@pytest.fixture
def parked(ledger, monkeypatch):
    from typing import TypedDict
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    class GraphState(TypedDict):
        result: str

    def choose(state):
        return {
            "result": request_decision.invoke(
                {
                    "question": "Release?",
                    "options": OPTIONS,
                    "expires_in_minutes": 5,
                    "default_option": "hold",
                    "default_action": "keep holding",
                }
            )
        }

    builder = StateGraph(GraphState)
    builder.add_node("choose", choose)
    builder.add_edge(START, "choose")
    builder.add_edge("choose", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(STATE, "graph", graph)
    config = {"configurable": {"thread_id": "scoped:release"}}
    received = []

    async def resume(session_id, answer, decision_id, *, resolution=None):
        cfg = await turn_control.validate_decision_resume(config, decision_id, session_id)
        cfg["configurable"]["decision_resolution"] = resolution or {}
        async with turn_control.decision_resume(cfg, answer):
            received.append(answer)
            await graph.ainvoke(Command(resume=await chat._resume_payload(cfg, answer)), cfg)
        return True

    return graph, config, resume, received


async def park(graph, config):
    result = await graph.ainvoke({}, config)
    payload = result["__interrupt__"][0].value
    return await turn_control.record_decision_pause("s", payload, config)


@pytest.mark.asyncio
async def test_expiry_consumes_exact_checkpoint_and_retries_failed_delivery(ledger, parked):
    store, clock = ledger
    graph, config, resume, received = parked
    rec = await park(graph, config)
    forever = store.create("other", "Wait?", OPTIONS)
    assert rec.default_action == "keep holding"
    assert rec.expires_at == (clock.now + timedelta(minutes=5)).isoformat(timespec="seconds")
    assert await sweep_expired_decisions(store, resume) == 0
    clock.now += timedelta(minutes=5)

    async def offline(*args, **kwargs):
        return False

    assert await sweep_expired_decisions(store, offline) == 0
    assert store.get(rec.id).pending
    assert await sweep_expired_decisions(store, resume) == 1
    assert received == [f"No answer before {rec.expires_at}; applying the stated default: Keep holding (hold)"]
    assert (await graph.aget_state(config)).values["result"] == received[0]
    assert store.get(rec.id).state == "expired"
    assert store.get(rec.id).answer == "hold"
    assert store.get(rec.id).answered_at == clock.now.isoformat(timespec="seconds")
    clock.now += timedelta(days=365)
    assert await sweep_expired_decisions(store, resume) == 0
    assert store.get(forever.id).pending
    with pytest.raises(DecisionClosed):
        await resume("s", "late", rec.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [None, "chat", "slack"])
async def test_answer_attribution_label_and_conflict(ledger, parked, monkeypatch, surface):
    store, clock = ledger
    graph, config, resume, received = parked
    rec = await park(graph, config)
    monkeypatch.setattr(decision_routes, "_resume_parked_turn", resume)
    app = client().app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        body = {"option_id": "go"}
        if surface:
            body["surface"] = surface
        r = await c.post(f"/api/decisions/{rec.id}/answer", json=body)
        assert r.status_code == 200
        assert r.json()["resumed"] is True
        assert received == ["Operator chose: Release (go)"]
        assert (await graph.aget_state(config)).values["result"] == received[0]
        data = (await c.get(f"/api/decisions/{rec.id}")).json()
        assert data["answer"] == "go"
        assert data["answer_label"] == "Release"
        assert data["answered_by"] == "device-josh"
        assert data["answered_surface"] == (surface or "console")
        assert data["answered_at"] == clock.now.isoformat(timespec="seconds")
        # A closed record wins even over an invalid answer from a stale surface.
        r = await c.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "missing"})
        assert r.status_code == 409
        assert r.json()["detail"] == data
    clock.now += timedelta(days=1)
    assert await sweep_expired_decisions(store, resume) == 0


@pytest.mark.platform_sensitive
def test_idempotent_migration_preserves_legacy_record(tmp_path):
    path = tmp_path / "decisions.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE decisions (id TEXT PRIMARY KEY, session_id TEXT, agent TEXT, question TEXT, options TEXT, created_at TEXT, state TEXT, answer TEXT, answered_at TEXT, expires_at TEXT, default_option TEXT)"
        )
        db.execute(
            "INSERT INTO decisions (id, session_id, question, options, created_at, state) VALUES ('old', 's', 'Wait?', '[]', '2026-11-08', 'pending')"
        )
    for _ in range(2):
        store = DecisionStore(path)
        assert store.get("old").pending
        assert store.get("old").default_action is None
    rec = store.create("s", "Go?", OPTIONS, default_action="hold", expires_in_minutes=1, default_option="hold")
    store.answer(rec.id, "go", answered_by="Josh", answered_surface="slack")
    reopened = DecisionStore(path).get(rec.id)
    assert reopened.answered_by == "Josh"
    assert reopened.answered_surface == "slack"
    assert reopened.default_action == "hold"
    assert reopened.options[0]["recommended"] is True


@pytest.mark.asyncio
async def test_resume_transport_carries_attribution(ledger, monkeypatch):
    store, _ = ledger
    rec = store.create("s", "Release?", OPTIONS)
    monkeypatch.setattr(STATE, "active_port", 7871)
    posted = []

    class Transport:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            posted.append(kwargs["json"]["params"]["message"])
            return SimpleNamespace(status_code=200, json=lambda: {})

    monkeypatch.setattr(httpx, "AsyncClient", Transport)
    resolution = {"answer": "go", "answered_by": "device-josh", "answered_surface": "console"}
    assert await decision_routes._resume_parked_turn("s", "Operator chose: Release (go)", rec.id, resolution=resolution)
    assert posted[0]["metadata"] == {"hitl_resume": True, "decision_id": rec.id, "decision_resolution": resolution}
    assert posted[0]["parts"] == [{"text": "Operator chose: Release (go)"}]
    assert store.get(rec.id).pending  # transport success alone never closes a decision


def test_answer_race_returns_winning_attribution(ledger, monkeypatch):
    store, _ = ledger
    rec = store.create("s", "Release?", OPTIONS)

    async def lost_race(*args, **kwargs):
        store.answer(rec.id, "hold", answered_by="Josh", answered_surface="chat")
        return False

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", lost_race)
    response = client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "go"})
    assert response.status_code == 409
    assert response.json()["detail"]["answer_label"] == "Keep holding"
    assert response.json()["detail"]["answered_surface"] == "chat"
    assert response.json()["detail"]["answered_by"] == "Josh"


@pytest.mark.asyncio
async def test_existing_host_loop_runs_expiry_even_without_a2a_engine(ledger, monkeypatch):
    import asyncio
    from graph import decision_expiry

    loops = importlib.import_module("server.maintenance_loops")
    store, _ = ledger
    calls = []
    sleeps = []

    async def sweep(actual_store, resume):
        assert actual_store is store
        assert resume is decision_routes._resume_parked_turn
        calls.append(True)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    monkeypatch.setattr(decision_expiry, "sweep_expired_decisions", sweep)
    monkeypatch.setattr(loops.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await loops._a2a_reaper_loop()
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_stream_entrypoint_preserves_resolution_metadata(ledger, parked, monkeypatch, expired):
    from graph.config import LangGraphConfig

    store, clock = ledger
    graph, config, _, _received = parked
    rec = await park(graph, config)
    if expired:
        clock.now += timedelta(minutes=5)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())
    monkeypatch.setattr(STATE, "goal_controller", None)
    text = (
        f"No answer before {rec.expires_at}; applying the stated default: Keep holding (hold)"
        if expired
        else "Operator chose: Release (go)"
    )
    frames = [
        frame
        async for frame in chat._chat_langgraph_stream(
            text,
            "s",
            request_metadata={
                "hitl_resume": True,
                "decision_id": rec.id,
                "decision_resolution": {
                    "answer": "hold" if expired else "go",
                    "answered_by": "device-josh",
                    "answered_surface": "console",
                    "state": "expired" if expired else "answered",
                },
            },
        )
    ]
    assert not [payload for kind, payload in frames if kind == "error"]
    updated = store.get(rec.id)
    assert updated.state == ("expired" if expired else "answered")
    assert updated.answer == ("hold" if expired else "go")
    assert updated.answered_by == (None if expired else "device-josh")
    assert updated.answered_surface == (None if expired else "console")


def test_lost_response_after_consumption_still_reports_success(ledger, monkeypatch):
    store, _ = ledger
    rec = store.create("s", "Release?", OPTIONS)

    async def response_lost(session_id, answer, decision_id, *, resolution):
        store.answer(
            decision_id,
            resolution["answer"],
            answered_by=resolution["answered_by"],
            answered_surface=resolution["answered_surface"],
        )
        return False

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", response_lost)
    response = client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "go"})
    assert response.status_code == 200
    assert response.json()["resumed"] is True


def test_agent_can_withdraw_its_decision_from_another_session(ledger, monkeypatch):
    store, _ = ledger
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(identity_name="release-agent"))
    own = store.create("parked", "Release?", OPTIONS, agent="release-agent")
    other = store.create("parked-other", "Release?", OPTIONS, agent="other-agent")
    args = {"state": {"session_id": "working"}, "reason": "Release abandoned"}
    assert withdraw_decision.invoke({"decision_id": other.id, **args}).startswith("Error:")
    assert "withdrawn" in withdraw_decision.invoke({"decision_id": own.id, **args})
    assert store.get(own.id).withdraw_reason == "Release abandoned"
    assert store.get(other.id).pending
