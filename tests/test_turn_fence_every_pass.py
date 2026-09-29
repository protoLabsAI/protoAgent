"""A fenced turn is fenced on EVERY graph pass it runs — both turn drivers (#1639/#2972).

The per-turn tool allowlist rides the graph state as ``subagent_fence`` and
``SubagentFenceMiddleware`` enforces it. State channels persist per checkpointer
THREAD, so a pass that omits the key inherits whatever that thread last held — and a
fresh-context goal continuation runs on a NEW thread that holds nothing. These pin that
each pass of a fenced turn carries the fence in its own graph input:

* the initial pass, a HITL resume, an autonomous auto-answer resume, a same-thread and a
  fresh-context goal continuation, and the context-overflow retry;
* on the streaming driver (``_chat_langgraph_stream``, fence from request metadata) and
  the non-streaming one (``chat``, ``tool_fence``).

The unit tests fake only the graph (``tests/_turn_driver_fakes.ScriptedGraph``) and read
what reached it; the end-to-end ones drive the REAL graph with a scripted model and
assert the middleware blocks an out-of-fence tool on the continuation / resume pass.
"""

from __future__ import annotations

import importlib

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from graph.config import LangGraphConfig
from tests._turn_driver_fakes import (
    FakeGoals,
    Invoke,
    Raise,
    ScriptedGraph,
    TraceSpy,
    set_interrupt,
    text,
    turn_result,
)

chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")

_FENCE = ["discord_read"]
_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."


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


async def _stream(message="hello", session_id="s1", **kw):
    return [f async for f in chat_mod._chat_langgraph_stream(message, session_id, **kw)]


def _fence_of(graph_input):
    """The fence a pass's graph input stamps: the dict key on a fresh pass, the Command's
    state update on a resume. ``None`` when the pass stamps nothing."""
    if isinstance(graph_input, Command):
        return (graph_input.update or {}).get("subagent_fence")
    return graph_input.get("subagent_fence")


# ── streaming driver ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_fresh_context_goal_continuation_carries_the_fence(env, monkeypatch):
    goals = FakeGoals([("continue", "again", "iterate"), None], iteration=3, fresh=True)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[text("r1", "one")], [text("r2", "two")]])

    await _stream("go", request_metadata={"subagent_fence": _FENCE})

    (first, _), (cont, cont_cfg) = g.stream_calls
    # The continuation runs on its own thread — nothing there to inherit a fence from.
    assert cont_cfg["configurable"]["thread_id"] == "a2a:s1:goal-iter-4"
    assert _fence_of(first) == _FENCE
    assert _fence_of(cont) == _FENCE


@pytest.mark.asyncio
async def test_stream_same_thread_goal_continuation_carries_the_fence(env, monkeypatch):
    goals = FakeGoals([("continue", "not yet", "keep going"), ("done", "met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[text("r1", "draft")], [text("r2", "better")]])

    await _stream("ship it", request_metadata={"subagent_fence": _FENCE, "incognito": True})

    assert [_fence_of(c[0]) for c in g.stream_calls] == [_FENCE, _FENCE]
    assert [c[0]["incognito"] for c in g.stream_calls] == [True, True]


@pytest.mark.asyncio
async def test_stream_hitl_resume_carries_the_fence(env):
    g = env.install(streams=[[text("r1", "ok")]])
    g.pending.append({"question": "Which env?"})

    await _stream("staging", resume=True, request_metadata={"subagent_fence": _FENCE})

    ((graph_input, _),) = g.stream_calls
    assert isinstance(graph_input, Command)
    assert g.resumes == [{"int-0": "staging"}]
    assert _fence_of(graph_input) == _FENCE


@pytest.mark.asyncio
async def test_stream_autonomous_auto_answer_resume_carries_the_fence(env):
    g = env.install(streams=[[set_interrupt("which?")], [text("r1", "on it")]])

    await _stream(request_metadata={"subagent_fence": _FENCE, "origin": "background"})

    (first, _), (resumed, _) = g.stream_calls
    assert isinstance(resumed, Command)
    assert g.resumes == [{"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL}]
    assert [_fence_of(first), _fence_of(resumed)] == [_FENCE, _FENCE]


@pytest.mark.asyncio
async def test_stream_overflow_retry_carries_the_fence(env, monkeypatch):
    async def _compacted(exc, tid, sid):
        return True

    monkeypatch.setattr(chat_mod, "_overflow_compacted", _compacted)
    g = env.install(streams=[[Raise(RuntimeError(_OVERFLOW))], [text("r1", "recovered")]])

    await _stream(request_metadata={"subagent_fence": _FENCE})

    assert [_fence_of(c[0]) for c in g.stream_calls] == [_FENCE, _FENCE]


@pytest.mark.asyncio
async def test_stream_unfenced_resume_stamps_no_fence(env):
    """A resume continues the parked turn: unfenced, it neither adds nor clears a fence."""
    g = env.install(streams=[[text("r1", "ok")]])
    g.pending.append({"question": "Which env?"})

    await _stream("staging", resume=True)

    ((graph_input, _),) = g.stream_calls
    assert isinstance(graph_input, Command)
    assert graph_input.update is None


# ── non-streaming driver ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_hitl_resume_carries_the_fence(env):
    g = env.install(invokes=[turn_result(AIMessage(content="ok"))])
    g.pending.append({"question": "Which env?"})

    await chat_mod.chat("staging", "s1", hitl_resume=True, tool_fence=_FENCE)

    ((graph_input, _),) = g.invoke_calls
    assert isinstance(graph_input, Command)
    assert g.resumes == [{"int-0": "staging"}]
    assert _fence_of(graph_input) == _FENCE


@pytest.mark.asyncio
async def test_sync_fresh_context_goal_continuation_carries_the_fence(env, monkeypatch):
    goals = FakeGoals([("continue", "again", "iterate"), None], iteration=3, fresh=True)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(invokes=[turn_result(AIMessage(content="one")), turn_result(AIMessage(content="two"))])

    await chat_mod.chat("go", "s1", tool_fence=_FENCE)

    (first, _), (cont, cont_cfg) = g.invoke_calls
    assert cont_cfg["configurable"]["thread_id"] == "a2a:s1:goal-iter-4"
    assert [_fence_of(first), _fence_of(cont)] == [_FENCE, _FENCE]


@pytest.mark.asyncio
async def test_sync_overflow_retry_carries_the_fence(env, monkeypatch):
    async def _compacted(exc, tid, sid):
        return True

    monkeypatch.setattr(chat_mod, "_overflow_compacted", _compacted)
    g = env.install(invokes=[Invoke(raises=RuntimeError(_OVERFLOW)), turn_result(AIMessage(content="recovered"))])

    await chat_mod.chat("hello", "s1", tool_fence=_FENCE)

    assert [_fence_of(c[0]) for c in g.invoke_calls] == [_FENCE, _FENCE]


# ── end to end: the REAL graph + SubagentFenceMiddleware ─────────────────────


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _real_graph(monkeypatch, messages):
    from unittest.mock import patch

    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    # Streaming a tool-call-only message yields no chunks from the fake; the driver's
    # astream_events still sees the model's start/end events without it.
    fake = _ToolFake(messages=iter(messages), disable_streaming=True)
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    return g


def _call(name: str, cid: str, args: dict | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid, "type": "tool_call"}])


async def _tool_messages(graph, thread_id: str) -> list[ToolMessage]:
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return [m for m in snap.values.get("messages", []) if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_e2e_fresh_context_continuation_blocks_a_tool_outside_the_fence(env, monkeypatch):
    goals = FakeGoals([("continue", "again", "iterate"), None], iteration=3, fresh=True)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = _real_graph(monkeypatch, [AIMessage(content="one"), _call("current_time", "c1"), AIMessage(content="two")])

    frames = await _stream("go", "sE", request_metadata={"subagent_fence": _FENCE})

    assert frames[-1][0] == "done"
    (tool,) = await _tool_messages(g, "a2a:sE:goal-iter-4")
    assert tool.status == "error"
    assert "Blocked by policy" in tool.content and "current_time" in tool.content


@pytest.mark.asyncio
async def test_e2e_fenced_resume_blocks_a_tool_outside_the_fence(env, monkeypatch):
    """The parked turn was unfenced; the fenced answer that resumes it must not run the
    rest of that pass with the wider toolset."""
    g = _real_graph(
        monkeypatch,
        [
            _call("ask_human", "q1", {"question": "Which env?"}),
            _call("current_time", "c1"),
            AIMessage(content="done"),
        ],
    )

    parked = await _stream("deploy", "sR")
    assert parked[-1][0] == "input_required"

    await _stream("staging", "sR", resume=True, request_metadata={"subagent_fence": _FENCE})

    tools = await _tool_messages(g, "a2a:sR")
    assert [t.tool_call_id for t in tools] == ["q1", "c1"]
    assert tools[1].status == "error"
    assert "Blocked by policy" in tools[1].content and "current_time" in tools[1].content
