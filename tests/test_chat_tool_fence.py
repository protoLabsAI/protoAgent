"""Per-turn tool fence on the non-streaming chat path + the plugin host invoke (#2972).

A plugin surface relaying a message from an UNTRUSTED party (another operator's agent
in a shared Discord channel) must be able to fence that turn to an allowlist. The
mechanism is the existing ``subagent_fence`` state channel (#1639) — these drive the
REAL graph (fake model emitting a tool call) through ``server.chat.chat`` and assert
the middleware blocks the foreign call, and — because state channels persist in the
checkpointer — that the NEXT unfenced turn on the same session is NOT fenced.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _install_graph(monkeypatch, messages):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ToolFake(messages=iter(messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


def _time_call(cid: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "current_time", "args": {}, "id": cid, "type": "tool_call"}],
    )


async def _snapshot(graph, session_id: str):
    from server.chat import _resolve_thread_id

    return await graph.aget_state({"configurable": {"thread_id": _resolve_thread_id(None, session_id)}})


async def _tool_messages(graph, session_id: str) -> list[ToolMessage]:
    snap = await _snapshot(graph, session_id)
    return [m for m in snap.values["messages"] if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_fenced_turn_blocks_a_tool_outside_the_allowlist(monkeypatch):
    from server.chat import chat

    g = _install_graph(monkeypatch, [_time_call("c1"), AIMessage(content="done")])
    out = await chat("what time is it?", "sessF", tool_fence=["discord_read"])
    assert out[0]["content"] == "done"
    tools = await _tool_messages(g, "sessF")
    assert len(tools) == 1
    assert tools[0].status == "error"
    assert "Blocked by policy" in tools[0].content and "current_time" in tools[0].content


@pytest.mark.asyncio
async def test_allowlisted_tool_runs_under_the_fence(monkeypatch):
    from server.chat import chat

    g = _install_graph(monkeypatch, [_time_call("c1"), AIMessage(content="done")])
    await chat("what time is it?", "sessA", tool_fence=["current_time"])
    tools = await _tool_messages(g, "sessA")
    assert len(tools) == 1
    assert tools[0].status != "error"
    assert "Blocked by policy" not in tools[0].content


@pytest.mark.asyncio
async def test_the_next_unfenced_turn_on_the_session_is_not_fenced(monkeypatch):
    """State channels persist in the checkpointer: an omitted key would inherit the
    previous turn's fence. The fence is stamped every turn ([] = none), so a fenced
    peer turn never leaves the operator's next turn on that session fenced."""
    from server.chat import chat

    g = _install_graph(
        monkeypatch,
        [_time_call("c1"), AIMessage(content="t1"), _time_call("c2"), AIMessage(content="t2")],
    )
    await chat("time?", "sessN", tool_fence=["discord_read"])
    await chat("time again?", "sessN")
    tools = await _tool_messages(g, "sessN")
    assert [t.status == "error" for t in tools] == [True, False]
    snap = await _snapshot(g, "sessN")
    assert snap.values.get("subagent_fence") == []


@pytest.mark.asyncio
async def test_plugin_host_invoke_forwards_the_fence(monkeypatch):
    """The ADR 0018 host seam: ``HOST.invoke(prompt, session_id, tool_fence=…)`` reaches
    ``chat()``; the positional 2-arg form every existing surface uses still works."""
    import server.agent_init as ai

    seen = []

    async def _fake_chat(prompt, session_id, *, tool_fence=None, **_kw):
        seen.append((prompt, session_id, tool_fence))
        return [{"role": "assistant", "content": "ok"}]

    monkeypatch.setattr(ai, "chat", _fake_chat)
    assert await ai._plugin_agent_invoke("hi", "s1") == "ok"
    assert await ai._plugin_agent_invoke("hi", "s1", tool_fence=["discord_read"]) == "ok"
    assert seen == [("hi", "s1", None), ("hi", "s1", ["discord_read"])]
