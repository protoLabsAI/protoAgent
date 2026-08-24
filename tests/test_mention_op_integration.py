"""`@<name>` against a REAL compiled graph + checkpointer, not a fake (#3042).

`tests/test_mention_op.py` proves the logic against a stand-in graph. This proves the
one thing a stand-in cannot: that `aupdate_state({"messages": […]})` actually lands on
the real `ProtoAgentState` schema through a real checkpointer, and that reading the
thread back yields the room. A reducer mismatch, an "ambiguous update — specify
as_node", or a channel the compiled graph doesn't accept would all pass the unit tests
and fail the moment an operator typed `@proto`.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import START

import graph.mention_op as mop
from graph.agent import create_agent_graph
from graph.config import LangGraphConfig


class _Delegate:
    type = "acp"


class _Registry:
    def __init__(self, reply="found it — line 40"):
        self.reply = reply
        self.calls = []

    def get(self, name):
        return _Delegate()

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append({"name": name, "query": query, "conversation_key": conversation_key})
        return self.reply


@pytest.fixture
def real_graph():
    return create_agent_graph(LangGraphConfig(), checkpointer=MemorySaver())


async def _thread_messages(graph, thread_id):
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return list((getattr(snapshot, "values", None) or {}).get("messages") or [])


@pytest.mark.asyncio
async def test_the_exchange_lands_on_a_real_checkpointer_thread(real_graph):
    reg = _Registry()
    out = await mop.run_mention(real_graph, reg, "room-1", "proto", "the auth bug?")
    assert out["ok"], out

    messages = await _thread_messages(real_graph, "room-1")
    rooms = [m for m in messages if (m.additional_kwargs or {}).get("lc_source") == "room"]
    assert [m.additional_kwargs["room"] for m in rooms] == [
        {"from": "operator", "to": "proto"},
        {"from": "proto"},
    ]
    assert "found it — line 40" in rooms[1].content


@pytest.mark.asyncio
async def test_the_write_appends_rather_than_replacing_existing_history(real_graph):
    """`messages` uses the add_messages reducer — an update must not clobber the thread."""
    cfg = {"configurable": {"thread_id": "room-2"}}
    await real_graph.aupdate_state(cfg, {"messages": [HumanMessage(content="here's the plan")]}, as_node=START)
    await real_graph.aupdate_state(cfg, {"messages": [AIMessage(content="I'd split slice 2")]}, as_node=START)

    await mop.run_mention(real_graph, _Registry(), "room-2", "proto", "thoughts?")

    texts = [str(m.content) for m in await _thread_messages(real_graph, "room-2")]
    assert "here's the plan" in texts[0]
    assert "I'd split slice 2" in texts[1]
    assert len(texts) == 4  # the two seeded turns + both halves of the exchange


@pytest.mark.asyncio
async def test_a_second_address_catches_the_delegate_up_on_the_real_thread(real_graph):
    """The round trip end to end: two addresses on one thread, and the second delegate
    sees the first one's words — read back off a real checkpointer, not a list."""
    cfg = {"configurable": {"thread_id": "room-3"}}
    await real_graph.aupdate_state(cfg, {"messages": [HumanMessage(content="here's the plan")]}, as_node=START)

    await mop.run_mention(real_graph, _Registry(reply="I'd split slice 2"), "room-3", "claude-code", "thoughts?")
    second = _Registry(reply="agreed")
    out = await mop.run_mention(real_graph, second, "room-3", "proto", "and you?")

    assert out["ok"] and out["catchup"] > 0
    query = second.calls[0]["query"]
    assert "[operator] here's the plan" in query
    assert "[claude-code] I'd split slice 2" in query
    # The envelope must NOT survive into another participant's catch-up.
    assert "<room-message" not in query
    assert query.endswith("and you?")


@pytest.mark.asyncio
async def test_a_delegate_is_not_caught_up_on_what_it_already_said(real_graph):
    reg = _Registry(reply="first answer")
    await mop.run_mention(real_graph, reg, "room-4", "proto", "one")
    again = _Registry(reply="second answer")
    await mop.run_mention(real_graph, again, "room-4", "proto", "two")

    query = again.calls[0]["query"]
    assert "first answer" not in query  # it said that; the window starts after it
    assert query == "two"  # nothing new from anyone else ⇒ no preface at all
