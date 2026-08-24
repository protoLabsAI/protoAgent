"""A room is real turns and addresses INTERLEAVED (#3042) — the sequence that ships.

`run_mention` writes to the thread with `as_node=START`, which leaves the checkpoint in a
pending state. These prove that costs nothing: an ordinary turn still runs correctly
afterwards, the lead agent can SEE what the addressed delegate said, and nothing is
replayed or lost. Uses the real compiled graph with a fake model — the interaction
between a hand-written checkpoint update and the graph's own run loop is precisely what
neither a fake graph nor a unit test can tell you about.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

import graph.mention_op as mop


class _Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _Delegate:
    type = "acp"


class _Registry:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def get(self, name):
        return _Delegate()

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append(query)
        return self.reply


def _graph(replies):
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig

    fake = _Fake(messages=iter([AIMessage(content=r) for r in replies]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        return create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())


async def _turn(graph, thread_id, text):
    cfg = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke({"messages": [HumanMessage(content=text)], "session_id": thread_id}, config=cfg)


async def _messages(graph, thread_id):
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return list((getattr(snap, "values", None) or {}).get("messages") or [])


@pytest.mark.asyncio
async def test_an_ordinary_turn_still_runs_after_an_address():
    """The pending checkpoint `as_node=START` leaves behind must not wedge the next turn."""
    g = _graph(["first answer", "second answer"])
    await _turn(g, "r1", "hello")
    await mop.run_mention(g, _Registry("line 40"), "r1", "proto", "the auth bug?")
    result = await _turn(g, "r1", "and now?")

    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "second answer"


@pytest.mark.asyncio
async def test_the_lead_agent_sees_what_the_addressed_delegate_said():
    """The whole reason the exchange is written to the thread: the operator's NEXT bare
    message goes to the lead, and the lead must not be blind to the room."""
    g = _graph(["first answer", "second answer"])
    await _turn(g, "r2", "hello")
    await mop.run_mention(g, _Registry("the bug is on line 40"), "r2", "proto", "the auth bug?")
    result = await _turn(g, "r2", "and now?")

    transcript = "\n".join(str(m.content) for m in result["messages"])
    assert "the bug is on line 40" in transcript
    assert 'room-message from="proto"' in transcript


@pytest.mark.asyncio
async def test_the_address_is_not_replayed_as_a_turn():
    """A pending checkpoint could plausibly re-run the seeded input. It must not: the
    room messages are a RECORD, and re-running them would double the conversation."""
    g = _graph(["first answer", "second answer"])
    await _turn(g, "r3", "hello")
    await mop.run_mention(g, _Registry("line 40"), "r3", "proto", "the auth bug?")
    before = [str(m.content) for m in await _messages(g, "r3")]
    await _turn(g, "r3", "and now?")
    after = [str(m.content) for m in await _messages(g, "r3")]

    # Exactly the new human turn + the new AI answer — nothing re-emitted.
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2


@pytest.mark.asyncio
async def test_repeated_addresses_between_turns_all_land():
    """The failure mode the fake graph hid: only the FIRST write to a thread succeeded."""
    g = _graph(["first answer", "second answer"])
    await _turn(g, "r4", "hello")
    await mop.run_mention(g, _Registry("a"), "r4", "proto", "one")
    await mop.run_mention(g, _Registry("b"), "r4", "claude-code", "two")
    await mop.run_mention(g, _Registry("c"), "r4", "proto", "three")

    rooms = [m for m in await _messages(g, "r4") if (m.additional_kwargs or {}).get("lc_source") == "room"]
    assert [m.additional_kwargs["room"] for m in rooms] == [
        {"from": "operator", "to": "proto"},
        {"from": "proto"},
        {"from": "operator", "to": "claude-code"},
        {"from": "claude-code"},
        {"from": "operator", "to": "proto"},
        {"from": "proto"},
    ]


@pytest.mark.asyncio
async def test_an_address_before_any_turn_works_on_a_virgin_thread():
    g = _graph(["only answer"])
    out = await mop.run_mention(g, _Registry("line 40"), "r5", "proto", "the auth bug?")
    assert out["ok"]
    assert len([m for m in await _messages(g, "r5") if (m.additional_kwargs or {}).get("lc_source") == "room"]) == 2
