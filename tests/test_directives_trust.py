"""Operator provenance survives graph injection without trusting peer or automated turns."""

import importlib
from contextlib import contextmanager

import pytest
from langgraph.types import Command

from graph.config import LangGraphConfig
from graph.directives import DirectiveStore, directives_command_reply
from observability.tracing import reset_trust_tier, set_trust_tier
from tests._turn_driver_fakes import Invoke, ScriptedGraph, TraceSpy, text, turn_result
from tools.directive_tools import build_directive_tools


@pytest.fixture
def env(monkeypatch):
    import runtime.state as rs
    from observability import metrics, pricing

    class Env:
        pass

    e = Env()
    TraceSpy().install(monkeypatch)
    monkeypatch.setattr(metrics, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "record_overflow_recovery", lambda: None)
    monkeypatch.setattr(pricing, "cost_usd", lambda model, usage: 0.0)
    for attr, val in {
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def install(streams=(), invokes=()):
        e.graph = ScriptedGraph(streams, invokes)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    e.state = rs.STATE
    yield e
    graph = getattr(e, "graph", None)
    assert graph is None or not graph.overrun, "driver made an unscripted graph call"


@contextmanager
def tier(value):
    token = set_trust_tier(value)
    try:
        yield
    finally:
        reset_trust_tier(token)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from runtime.state import STATE

    store = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(STATE, "directive_store", store)
    return store


def record(state):
    tool = next(t for t in build_directive_tools() if t.name == "record_directive")
    # This exercises the tool at call time, even when it remains bound to the graph.
    return tool.invoke({"quote": "hold production", "state": state})


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["stream", "sync"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "trust,origin,allowed",
    [
        ("operator", "console", True),
        ("operator", "api-chat", True),
        ("operator", "", True),  # established console streaming protocol
        ("federation", "console", False),
        ("operator", "a2a", False),
        ("operator", "scheduler", False),
        ("operator", "inbox", False),
        ("operator", "background", False),
        ("operator", "background-resume", False),
        ("operator", "delegate-result", False),
        ("operator", "unknown", False),
        ("operator", None, False),
        ("operator", False, False),
        ("operator", 0, False),
        ("", "console", False),
    ],
)
async def test_chat_drivers_carry_authenticated_origin(env, ledger, driver, resume, trust, origin, allowed):
    chat = importlib.import_module("server.chat")

    graph = env.install(streams=[[text("r1", "ok")]], invokes=[Invoke(turn_result("ok"))])
    if resume:
        graph.pending.append({"question": "Which environment?"})
    with tier(trust):
        if driver == "stream":
            _ = [f async for f in chat._chat_langgraph_stream(
                "production", "s1", resume=resume, request_metadata={"origin": origin}
            )]
            state = graph.stream_calls[0][0]
        else:
            await chat._chat_langgraph_impl("production", "s1", origin=origin, hitl_resume=resume)
            state = graph.invoke_calls[0][0]
    if isinstance(state, Command):
        state = {"session_id": "s1", **state.update}
    # Request contextvars are gone by tool execution; injected state is sufficient.
    with tier(""):
        result = record(state)
    assert result.startswith("Recorded directive") is allowed
    assert bool(ledger.in_force("s1")) is allowed
    if not allowed:
        assert "this turn did not come from the operator" in result


@pytest.mark.parametrize("extra", [{}, {"operator_chat_turn": False}, {"operator_chat_turn": "true"},
                                   {"operator_chat_turn": True, "subagent_fence": ["record_directive"]}])
def test_unclassified_and_subagent_tools_fail_closed(ledger, extra):
    # A subagent's fresh input has a session but no operator provenance; even inheriting
    # an operator contextvar must not authorize it. A fenced run also refuses explicitly.
    with tier("operator"):
        assert "did not come from the operator" in record({"session_id": "s1", **extra})
    assert ledger.list("s1") == []


@pytest.mark.parametrize("trust", ["operator", "federation", ""])
@pytest.mark.parametrize("verb", ["revoke", "cancel"])
def test_text_revoke_authenticates_sender(ledger, trust, verb):
    from server.chat_dispatch import _directives_command_reply

    directive = ledger.record("s1", "hold production")
    with tier(trust):
        reply = _directives_command_reply(f"/directives {verb} {directive.id} withdrawn", "s1")
    got = ledger.get("s1", directive.id)
    if trust == "operator":
        assert reply.startswith("Revoked")
        assert got.status == "revoked" and got.closed_by == "operator"
    else:
        assert reply == "Only the operator can revoke a directive."
        assert got.in_force and got.closed_by is None and got.closed_at is None
        with tier(trust):
            assert directives_command_reply("s1", f"{verb} {directive.id}") == reply


@pytest.mark.parametrize("trust", ["federation", ""])
def test_other_tools_remain_available(ledger, trust):
    tools = {t.name: t for t in build_directive_tools()}
    state = {"session_id": "s1"}
    first = ledger.record("s1", "one")
    second = ledger.record("s1", "two")
    with tier(trust):
        assert first.id in tools["list_directives"].invoke({"state": state})
        assert second.id in directives_command_reply("s1", "")
        for name, directive in [("fulfil_directive", first), ("revoke_directive", second)]:
            tools[name].invoke({"directive_id": directive.id, "reason": "done", "state": state})
            assert ledger.get("s1", directive.id).closed_by == "agent"
    assert ledger.get("s1", first.id).status == "fulfilled"
    assert ledger.get("s1", second.id).status == "revoked"


def test_real_tool_node_injects_origin_and_does_not_keep_previous_authority(ledger):
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode

    from graph.state import ProtoAgentState

    builder = StateGraph(ProtoAgentState)
    builder.add_node("tools", ToolNode(build_directive_tools()))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "same-chat"}}
    for index, allowed in enumerate([True, False, False]):
        with tier(""):
            result = graph.invoke({
                "session_id": "s1",
                "operator_chat_turn": allowed,
                "messages": [AIMessage(content="", tool_calls=[{
                    "name": "record_directive", "args": {"quote": f"rule {index}"},
                    "id": f"call-{index}", "type": "tool_call",
                }])],
            }, config)
        reply = result["messages"][-1]
        assert isinstance(reply, ToolMessage)
        assert reply.content.startswith("Recorded directive") is allowed
    assert [d.quote for d in ledger.in_force("s1")] == ["rule 0"]


def test_operator_stamp_folds_concurrent_writes_in_one_superstep():
    """A resume that drains several parallel HITL interrupts feeds the operator stamp
    into each pending branch, so ``operator_chat_turn`` takes more than one write in a
    single superstep. A bare LastValue channel raises ``InvalidUpdateError`` there — it
    broke the parallel-interrupt drive (test_hitl_hold) — so the field carries the
    ``graph.state._take_last`` reducer. This pins that: two parallel nodes writing the
    same authenticated value must fold to it, not raise."""
    from langgraph.graph import END, START, StateGraph

    from graph.state import ProtoAgentState

    builder = StateGraph(ProtoAgentState)
    builder.add_node("fan", lambda _s: {"session_id": "s1"})
    builder.add_node("a", lambda _s: {"operator_chat_turn": True})
    builder.add_node("b", lambda _s: {"operator_chat_turn": True})
    builder.add_edge(START, "fan")
    builder.add_edge("fan", "a")  # a and b run together — two writes, one superstep
    builder.add_edge("fan", "b")
    builder.add_edge("a", END)
    builder.add_edge("b", END)
    graph = builder.compile()
    assert graph.invoke({"session_id": "s1"})["operator_chat_turn"] is True
