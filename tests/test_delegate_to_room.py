"""Foreground ``delegate_to`` records its exchange through the active turn (#3102)."""

from __future__ import annotations

import itertools

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from graph.agent import create_agent_graph
from graph.config import LangGraphConfig
from plugins.delegates import _build_delegate_to, _dispatch_into_room


class _Delegate:
    type = "acp"


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _chunk(self):
        return ChatGenerationChunk(message=next(self.messages))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield self._chunk()

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield self._chunk()


class _Registry:
    def __init__(self):
        self.calls = []

    def get(self, name):
        return _Delegate() if name == "proto" else None

    def listing(self):
        return "proto"

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None, timeout=None, **_kwargs):
        self.calls.append({"conversation_key": conversation_key, "permissions": permissions, "timeout": timeout})
        return "the token expires before refresh"


@pytest.mark.asyncio
async def test_command_carries_authored_room_messages_and_the_tool_terminator():
    registry = _Registry()
    out = await _dispatch_into_room(
        registry,
        "proto",
        "inspect auth",
        {"session_id": "room-command", "messages": [HumanMessage(content="please investigate auth")]},
        tool_call_id="call-1",
    )

    assert isinstance(out, Command)
    messages = out.update["messages"]
    assert [message.additional_kwargs["room"] for message in messages[:2]] == [
        {"from": "assistant", "to": "proto"},
        {"from": "proto"},
    ]
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "call-1"
    assert messages[2].content == "the token expires before refresh"
    assert registry.calls[0]["permissions"] is None  # preserve foreground delegate_to access


@pytest.mark.asyncio
async def test_real_toolnode_checkpoints_the_room_with_its_turn(monkeypatch):
    """This is the lost-update regression: ToolNode must accept and reduce the Command."""
    fake = _ToolFake(
        messages=itertools.chain(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "delegate_to", "args": {"target": "proto", "query": "inspect auth"}, "id": "call-1"}],
                ),
                AIMessage(content="<output>I will use that finding.</output>"),
            ],
            itertools.repeat(AIMessage(content="<output>done</output>")),
        )
    )
    monkeypatch.setattr("graph.agent.create_llm", lambda *args, **kwargs: fake)
    graph = create_agent_graph(
        LangGraphConfig(),
        include_subagents=False,
        extra_tools=[_build_delegate_to(_Registry())],
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "room-command"}}
    await graph.ainvoke({"messages": [HumanMessage(content="look into auth")], "session_id": "room-command"}, config)

    messages = (await graph.aget_state(config)).values["messages"]
    room = [message for message in messages if (message.additional_kwargs or {}).get("lc_source") == "room"]
    assert [message.additional_kwargs["room"] for message in room] == [
        {"from": "assistant", "to": "proto"},
        {"from": "proto"},
    ]
    assert any(isinstance(message, ToolMessage) and message.tool_call_id == "call-1" for message in messages)
