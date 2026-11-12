"""Answer route acknowledgement is independent of resumed turn completion (#5099)."""
import asyncio
import importlib
from typing import TypedDict

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from graph.decisions import DecisionStore
from operator_api import decision_routes
from runtime.state import STATE

turn_control = importlib.import_module("server.turn_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_answer_returns_on_consumption_and_preserves_next_pause(monkeypatch, fail_first):
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    release = asyncio.Event()
    running = asyncio.Event()
    finished = asyncio.Event()

    class State(TypedDict):
        answer: str

    def choose(state):
        return {"answer": interrupt({"kind": "decision", "question": "Continue?", "options": ["yes", "no"]})}

    async def slow(state):
        running.set()
        await release.wait()  # Represents a 90s+ turn without slowing the suite.
        interrupt({"kind": "decision", "question": "Again?", "options": ["yes", "no"]})
        return {}

    builder = StateGraph(State)
    builder.add_node("choose", choose)
    builder.add_node("slow", slow)
    builder.add_edge(START, "choose")
    builder.add_edge("choose", "slow")
    builder.add_edge("slow", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(STATE, "graph", graph)
    config = {"configurable": {"thread_id": "test:consume"}}
    await graph.ainvoke({}, config)
    snapshot = await graph.aget_state(config)
    rec = store.create("session", "Continue?", ["yes", "no"], thread_id="test:consume", interrupt_id=snapshot.tasks[0].interrupts[0].id)
    attempts = 0

    async def deliver(session_id, answer, decision_id, *, resolution=None):
        nonlocal attempts
        attempts += 1
        cfg = await turn_control.validate_decision_resume(config, decision_id, session_id)
        cfg["configurable"]["decision_resolution"] = resolution
        try:
            async with turn_control.decision_resume(cfg, answer):
                if fail_first and attempts == 1:
                    raise RuntimeError("failed before consuming")
                await graph.ainvoke(Command(resume=answer), cfg)
            snap = await graph.aget_state(cfg)
            store.create("session", "Again?", ["yes", "no"], thread_id="test:consume", interrupt_id=snap.tasks[0].interrupts[0].id)
            return True
        except RuntimeError:
            return False
        finally:
            if release.is_set():
                finished.set()

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", deliver)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/decisions/{rec.id}/answer"
        if fail_first:
            failed = await client.post(url, json={"option_id": "yes"})
            assert failed.json()["resumed"] is False
            assert store.get(rec.id).pending
        try:
            responses = await asyncio.wait_for(asyncio.gather(
                client.post(url, json={"option_id": "yes"}),
                client.post(url, json={"option_id": "no"}),
            ), 2)
            assert sorted(r.status_code for r in responses) == [200, 409]
            winner = next(r.json() for r in responses if r.status_code == 200)
            loser = next(r.json()["detail"] for r in responses if r.status_code == 409)
            assert winner["resumed"] is True
            assert winner["decision"]["state"] == "answered"
            assert winner["decision"]["answered_surface"] == "console"
            assert winner["decision"]["answered_by"] == "operator"
            assert loser == winner["decision"]
            assert running.is_set() and not finished.is_set()
            assert (await client.get("/api/decisions?state=pending")).json()["decisions"] == []
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        assert store.get(rec.id).state == "answered"
        second, = store.list(state="pending")
        assert second.id != rec.id


@pytest.mark.asyncio
async def test_disconnected_answer_keeps_delivery_claim(monkeypatch):
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    rec = store.create("session", "Continue?", ["yes", "no"])
    entered = asyncio.Event()
    consume = asyncio.Event()
    calls = []

    async def deliver(session_id, answer, decision_id, *, resolution=None):
        calls.append(answer)
        entered.set()
        await consume.wait()
        store.answer(decision_id, resolution["answer"])
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", deliver)
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/decisions/{rec.id}/answer"
        first = asyncio.create_task(client.post(url, json={"option_id": "yes"}))
        await asyncio.wait_for(entered.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(client.post(url, json={"option_id": "no"}))
        consume.set()
        response = await asyncio.wait_for(second, 2)
        assert response.status_code == 409
        assert response.json()["detail"]["answer"] == "yes"
        assert len(calls) == 1
