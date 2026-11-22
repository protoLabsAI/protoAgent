"""Completed turns expose only their own pending no-pause decisions (#5678)."""

import importlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypedDict

import httpx
import protolabs_a2a as pa
import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from a2a_impl.executor import HITL_MIME
from graph.config import LangGraphConfig
from graph.decisions import DecisionStore
from runtime.state import STATE
from server.turn_sync import _native_turn
from tests._a2a_app import build_app, drain_handlers, poll_terminal, send_msg

chat = importlib.import_module("server.chat")
control = importlib.import_module("server.turn_control")
OPTIONS = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]


class State(TypedDict):
    messages: Annotated[list, add_messages]


@pytest.fixture
def rig(monkeypatch):
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())
    monkeypatch.setattr(STATE, "goal_controller", None)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    created = []

    def agent(state, config):
        text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        thread = config["configurable"]["thread_id"]
        if text == "pause":
            interrupt({"kind": "decision", "question": "Deploy?", "options": OPTIONS, "context": "Release"})
        elif text == "ask":
            for question in ("Deploy?", "Notify?"):
                created.append(store.create("s", question, OPTIONS, thread_id=thread, context="Release"))
        return {"messages": [AIMessage(content="Finished.")]}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    monkeypatch.setattr(STATE, "graph", builder.compile(checkpointer=InMemorySaver()))
    return store, created


async def sync(text, sink):
    return await _native_turn(
        text, None, session_id="s", config={"configurable": {"thread_id": "a2a:s"}},
        usage_cb=UsageMetadataCallbackHandler(), state_extra={}, tool_fence=None,
        hitl_resume=False, incognito=False, telemetry_sink=sink,
    )


def continue_with_decisions(monkeypatch):
    from server.goal_loop import GoalContinuation, GoalDrive

    async def steps(self):
        step = GoalContinuation("ask", {"configurable": {"thread_id": "goal-scoped"}})
        yield step
        self.text = step.text

    monkeypatch.setattr(GoalDrive, "steps", steps)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "none", "pause", "continuation"])
async def test_sync_completion_identity_and_pause_compatibility(rig, mode, monkeypatch):
    if mode == "continuation":
        continue_with_decisions(monkeypatch)
    store, created = rig
    store.create("s", "Earlier ask", OPTIONS, thread_id="a2a:s")
    sink = {"state": "completed"}
    reply = await sync(mode, sink)
    assert sink["state"] == ("input_required" if mode == "pause" else "completed")
    if mode in {"ask", "continuation"}:
        assert reply[0]["decision_id"] == created[0].id
        assert reply[0]["hitl"] == reply[0]["decisions"][0]
        assert [card["decision_id"] for card in reply[0]["decisions"]] == [rec.id for rec in created]
        for card, rec in zip(reply[0]["decisions"], created):
            assert card["question"] == rec.question
            assert card["options"] == OPTIONS
            assert card["context"] == "Release"
        # Reattachment / the next turn cannot inherit a still-pending earlier card.
        if mode == "ask":
            assert all("decision_id" not in row and "hitl" not in row for row in await sync("none", {}))
    elif mode == "pause":
        rec = store.get(reply[0]["hitl"]["decision_id"])
        assert rec.interrupt_id
        assert "Input needed" in reply[0]["content"]
    else:
        assert "decision_id" not in reply[0] and "hitl" not in reply[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "none", "pause", "continuation"])
async def test_a2a_final_task_carries_cards_without_parking(rig, mode, monkeypatch):
    if mode == "continuation":
        continue_with_decisions(monkeypatch)
    store, created = rig

    async def stream(text, session_id, **kwargs):
        config = {"configurable": {"thread_id": f"a2a:{session_id}"}}
        store.create(session_id, "Earlier ask", OPTIONS, thread_id=config["configurable"]["thread_id"])
        async with control._thread_lock(config["configurable"]["thread_id"]):
            async for frame in chat._run_native_turn(mode, session_id, config, request_metadata={}):
                yield frame

    try:
        app = build_app(stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await send_msg(client)
            task = response.json()["result"]["task"]
            final = await poll_terminal(client, task["id"])
        assert final["status"]["state"] == (
            "TASK_STATE_INPUT_REQUIRED" if mode == "pause" else "TASK_STATE_COMPLETED"
        )
        parts = (final["status"].get("message", {}).get("parts", []) if mode == "pause" else
                 [p for artifact in final.get("artifacts", []) for p in artifact.get("parts", [])])
        cards = [data for part in parts for mime, data in [pa.read_data(part)] if mime == HITL_MIME]
        if mode == "pause":
            assert store.get(cards[0]["decision_id"]).interrupt_id
        else:
            assert [card["decision_id"] for card in cards] == [rec.id for rec in created]
            assert all(card["options"] == OPTIONS and card["context"] == "Release" for card in cards)
    finally:
        await drain_handlers()


def test_lookup_excludes_old_foreign_closed_and_paused_records(monkeypatch):
    now = datetime(2026, 11, 22, tzinfo=UTC)
    store = DecisionStore(":memory:", clock=lambda: now)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    store.create("s", "Earlier same second", [], thread_id="t")
    now += timedelta(microseconds=1)
    start = now.isoformat()
    store.create("s", "Foreign", [], thread_id="other")
    store.create("s", "Paused", [], thread_id="t", interrupt_id="pause")
    closed = store.create("s", "Closed", [], thread_id="t")
    store.cancel(closed.id)
    current = store.create("s", "Current", [], thread_id="t")
    assert [r.id for r in store.pending_unpaused_since("t", start)] == [current.id]


@pytest.mark.asyncio
async def test_failed_ledger_read_does_not_fail_completion(rig, monkeypatch):

    def unavailable(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(rig[0], "pending_unpaused_since", unavailable)
    sink = {"state": "completed"}
    reply = await sync("ask", sink)
    assert sink["state"] == "completed"
    assert reply[0]["content"] == "Finished."
    assert "decision_id" not in reply[0]
