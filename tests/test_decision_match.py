"""Typed decisions require an exact option and a live verified operator."""

import importlib
from contextlib import nullcontext
from typing import Annotated, TypedDict

import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from graph.config import LangGraphConfig
from graph.decision_match import match_decision
from graph.decisions import DecisionStore
from graph.middleware.request_context import verified_principal_scope
from runtime.state import STATE
from server.turn_sync import _native_turn

OPTIONS = [{"id": "ship", "label": "Ship it"}, {"id": "wait", "label": "Wait"}]


@pytest.fixture
def store(monkeypatch):
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "decision_store", store)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    return store


@pytest.mark.parametrize("text,expected", [
    ("ship it", "ship"), ("Ship it.", "ship"), ("SHIP!", "ship"),
    ("ok", None), ("sounds good", None), ("ship it but wait until Monday", None),
    ("hold on, one question first", None), ("!!!", None),
])
def test_whole_message_option_match(store, text, expected):
    rec = store.create("s", "Release?", OPTIONS, thread_id="a2a:s")
    result = match_decision(text, [rec], principal="shared-operator-bearer")
    assert (result[1] if result else None) == expected


@pytest.mark.parametrize("case", ["two", "paused", "pick", "server", "agent", "unknown", "ambiguous"])
def test_ineligible_answers_stay_pending(store, case):
    rec = store.create("s", "Release?", OPTIONS, thread_id="a2a:s",
                       interrupt_id="pause" if case == "paused" else "", requires_pick=case == "pick")
    records = [rec]
    if case == "two":
        records.append(store.create("s", "Other?", OPTIONS, thread_id="a2a:s"))
    if case == "ambiguous":
        rec.options.append({"id": "other", "label": "Ship it!"})
    principal = {"agent": "agent-device", "unknown": None}.get(case, "device")
    assert match_decision("ship it", records, principal=principal, server_started=case == "server") is None
    assert store.get(rec.id).pending


class State(TypedDict):
    messages: Annotated[list, add_messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["sync", "stream"])
@pytest.mark.parametrize("text,origin,principal,open_mode,answered", [
    ("Ship it.", "", "shared-operator-bearer", False, True),
    ("hold on, one question first", "", "shared-operator-bearer", False, False),
    ("ship", "scheduler", "shared-operator-bearer", False, False),
    ("ship", "inbox", "shared-operator-bearer", False, False),
    ("ship", "background-resume", "shared-operator-bearer", False, False),
    ("ship", "", "agent-device", False, False),
    # Open-mode operators must have the same typed-answer path as button answers (#5678).
    ("Ship it.", "", "open/unknown", True, True),
    ("ship", "", "open/unknown", False, False),
    ("ship", "scheduler", "open/unknown", True, False),
    ("ship", "", "agent-device", True, False),
    ("ship", "", None, True, False),
])
async def test_drivers_save_before_delivery_or_run_normal_message(store, monkeypatch, driver,
                                                                text, origin, principal, open_mode, answered):
    monkeypatch.setattr("a2a_impl.auth.open_mode", lambda: open_mode)
    rec = store.create("s", "Release?", OPTIONS, thread_id="a2a:s")
    seen = []

    def agent(state):
        message = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        seen.append(message)
        saved = store.get(rec.id)
        assert (saved.state == "answered") == answered
        if answered:
            assert saved.answer == "ship" and saved.answered_by == "operator"
            assert saved.answer_kind == "text" and saved.delivered_at
        return {"messages": [AIMessage(content="Received.")]}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    monkeypatch.setattr(STATE, "graph", builder.compile(checkpointer=InMemorySaver()))
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig())
    monkeypatch.setattr(STATE, "goal_controller", None)
    with verified_principal_scope(principal) if principal is not None else nullcontext():
        if driver == "sync":
            await _native_turn(text, None, session_id="s", config={"configurable": {"thread_id": "a2a:s"}},
                               usage_cb=UsageMetadataCallbackHandler(), state_extra={}, tool_fence=None,
                               hitl_resume=False, incognito=False, origin=origin)
        else:
            chat = importlib.import_module("server.chat")
            frames = [frame async for frame in chat._chat_langgraph_stream_impl(
                text, "s", request_metadata={"origin": origin, "attended": True})]
            assert not [frame for frame in frames if frame[0] == "error"]
    assert len(seen) == 1
    if answered:
        assert "Question: Release?" in seen[0] and "Answer: ship" in seen[0]
    else:
        assert seen == [text]
        assert store.get(rec.id).pending


@pytest.mark.parametrize("case", ["closed", "missing", "other-session", "other-thread", "two"])
def test_store_rechecks_current_eligibility(store, case):
    rec = store.create("s", "Release?", OPTIONS, thread_id="a2a:s")
    if case == "closed":
        store.cancel(rec.id)
    if case == "two":
        store.create("s", "Another?", OPTIONS, thread_id="a2a:s")
    if case == "missing":
        store = DecisionStore(":memory:")
    result = store.answer_typed("other" if case == "other-session" else "s",
                                "other" if case == "other-thread" else "a2a:s", "ship",
                                principal="device")
    assert result is None
    current = store.get(rec.id)
    assert current is None or current.state != "answered"


@pytest.mark.asyncio
async def test_failed_ledger_read_leaves_message_and_decision_alone(store, monkeypatch):
    from server.turn_control import answer_typed_decision

    rec = store.create("s", "Release?", OPTIONS, thread_id="a2a:s")

    async def no_pause(config):
        return None

    def unavailable(**kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(importlib.import_module("server.chat"), "_pending_interrupt_value", no_pause)
    monkeypatch.setattr(store, "list", unavailable)
    with verified_principal_scope("device"):
        assert await answer_typed_decision({"configurable": {"thread_id": "a2a:s"}}, "ship", "s") is None
    assert store.get(rec.id).pending
