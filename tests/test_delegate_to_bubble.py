"""A foreground `delegate_to` renders as an authored room bubble, not a tool card (#3042).

The lead moderates multi-agent collaboration by calling `delegate_to` in a loop
(#3114). Those calls should read as the participants speaking — proto, reviewer — the
same as an operator `@`, rather than as machinery buried under the lead's one reply.
This drives the REAL `_run_turn_stream` with a fake tool-calling model and a fake
`delegate_to` tool, and asserts the wire: a `room_reply` frame authored by the target,
and NO tool card for it.
"""

from __future__ import annotations

import itertools
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _chunk(self):
        msg = next(self.messages)
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=msg.content,
                tool_call_chunks=[
                    {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                    for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])
                ],
            )
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield self._chunk()

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        await asyncio.sleep(0)
        yield self._chunk()


# Longer than _TOOL_PREVIEW_CHARS (800) so a preview-cap regression would show up as a
# truncated bubble — a delegate's review or proposal easily runs past 800 chars.
_LONG_REPLY = "REVIEW: " + ("this is a detailed point. " * 60) + "Do you agree with the tests?"


@tool
async def delegate_to(target: str, query: str) -> str:
    """Fake delegate_to for the test — a long authored-looking reply."""
    return f"{target.upper()} · {_LONG_REPLY}"


def _call(**args):
    return AIMessage(content="", tool_calls=[{"name": "delegate_to", "args": args, "id": "d1", "type": "tool_call"}])


async def _frames(session, monkeypatch):
    import runtime.state as rs
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_turn_stream

    stream = itertools.chain(
        [_call(target="proto", query="look at auth"), AIMessage(content="proto handled it")],
        itertools.repeat(AIMessage(content="<output>done</output>")),
    )
    fake = _ToolFake(messages=stream)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    g = create_agent_graph(LangGraphConfig(), include_subagents=False, extra_tools=[delegate_to], checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    frames = []
    async for kind, payload in _run_turn_stream("have proto look at auth", session, {"configurable": {"thread_id": session}}):
        frames.append((kind, payload))
    return frames


@pytest.mark.asyncio
async def test_delegate_to_emits_ask_then_reply(monkeypatch):
    """The delegation renders as a mini-conversation (#3042): the lead's outgoing ASK
    (addressed_to, no author) then the participant's REPLY (author), in that order."""
    frames = await _frames("dtb1", monkeypatch)
    rooms = [p for k, p in frames if k == "room_reply"]
    assert len(rooms) == 2, f"expected ask + reply; saw {[k for k, _ in frames]}"

    ask, reply = rooms
    # 1. the lead's outgoing ask — directed, no author (the lead is speaking)
    assert ask["addressed_to"] == "proto"
    assert "author" not in ask
    assert "look at auth" in ask["text"]
    # 2. the participant's reply — author-stamped, from the lead
    assert reply["author"] == "proto"
    assert reply["from"] == "assistant"
    assert reply["text"].endswith("Do you agree with the tests?")  # FULL reply, not preview-capped
    assert len(reply["text"]) > 800  # #3042: a room bubble is a message, not an 800-char card
    assert reply["ok"] is True


@pytest.mark.asyncio
async def test_delegate_to_emits_NO_tool_card(monkeypatch):
    """The whole point of #3042's bubble: the delegation is conversation, not a card."""
    frames = await _frames("dtb2", monkeypatch)
    delegate_cards = [
        p for k, p in frames if k in ("tool_start", "tool_end") and (p.get("name") == "delegate_to")
    ]
    assert delegate_cards == [], f"delegate_to must not render a tool card; saw {delegate_cards}"


@pytest.mark.asyncio
async def test_an_ordinary_tool_still_gets_its_card(monkeypatch):
    """Only delegate_to is special — everything else keeps its tool card."""
    import runtime.state as rs
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_turn_stream

    @tool
    async def current_time() -> str:
        """A plain tool."""
        return "12:00"

    stream = itertools.chain(
        [
            AIMessage(content="", tool_calls=[{"name": "current_time", "args": {}, "id": "c1", "type": "tool_call"}]),
            AIMessage(content="it's noon"),
        ],
        itertools.repeat(AIMessage(content="<output>done</output>")),
    )
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: _ToolFake(messages=stream))
    g = create_agent_graph(LangGraphConfig(), include_subagents=False, extra_tools=[current_time], checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    frames = [(k, p) async for k, p in _run_turn_stream("time?", "dtb3", {"configurable": {"thread_id": "dtb3"}})]
    cards = [p for k, p in frames if k == "tool_end" and p.get("name") == "current_time"]
    assert cards, "an ordinary tool must still render a card"
    assert not [p for k, p in frames if k == "room_reply"], "a plain tool is not a room bubble"


@pytest.mark.asyncio
async def test_wire_order_is_leadtext_then_ask_then_reply(monkeypatch):
    """The interleave the console relies on (#3042): within one model response the lead's
    TEXT streams before the tool executes, so the frames arrive text → ask → reply. The
    console's reveal.flush() then commits that text before splitting on the bubble — but
    only if the wire delivers it first, which this pins."""
    import runtime.state as rs
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_turn_stream

    # One model response: lead SAYS something, THEN calls delegate_to.
    lead_then_delegate = AIMessage(
        content="I'll coordinate this.",
        tool_calls=[{"name": "delegate_to", "args": {"target": "proto", "query": "look at auth"}, "id": "d1", "type": "tool_call"}],
    )
    stream = itertools.chain(
        [lead_then_delegate, AIMessage(content="proto handled it")],
        itertools.repeat(AIMessage(content="<output>done</output>")),
    )
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: _ToolFake(messages=stream))
    g = create_agent_graph(LangGraphConfig(), include_subagents=False, extra_tools=[delegate_to], checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    order = []
    async for kind, payload in _run_turn_stream("go", "wire1", {"configurable": {"thread_id": "wire1"}}):
        if kind == "text" and "coordinate" in str(payload):
            order.append("lead-text")
        elif kind == "room_reply" and payload.get("addressed_to"):
            order.append("ask")
        elif kind == "room_reply" and payload.get("author"):
            order.append("reply")

    # The lead's text must reach the client BEFORE the delegation bubbles, so the console
    # can commit it into the placeholder and split after it — not below.
    assert order == ["lead-text", "ask", "reply"], f"wire order was {order}"
