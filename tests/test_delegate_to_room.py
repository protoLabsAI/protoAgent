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
    def __init__(self, dtype="acp"):
        self.type = dtype


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
    def __init__(self, dtype="acp"):
        self.calls = []
        self._dtype = dtype

    def get(self, name):
        return _Delegate(self._dtype) if name == "proto" else None

    def listing(self):
        return "proto"

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None, timeout=None, **_kwargs):
        self.calls.append(
            {"query": query, "conversation_key": conversation_key, "permissions": permissions, "timeout": timeout}
        )
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


@pytest.mark.asyncio
async def test_delegate_to_reads_the_operators_catchup_bounds(monkeypatch):
    """One `@` and one `delegate_to` must show a participant the SAME window. The bounds
    are the operator's config, not this call site's constant — a delegation that quietly
    used a different window would be a second, invisible room policy."""
    import runtime.state as rs
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    cfg.room_catchup_max_messages = 2
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)

    registry = _Registry()
    history = [HumanMessage(content=f"m{i}") for i in range(10)]
    await _dispatch_into_room(
        registry,
        "proto",
        "inspect auth",
        {"session_id": "room-caps", "messages": history},
        tool_call_id="call-caps",
    )

    query = registry.calls[0]["query"]
    assert "[operator] m9" in query and "[operator] m7" not in query
    assert "earlier messages omitted" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("dtype", ["acp", "a2a"])
async def test_delegate_to_shares_the_rooms_conversation_with_the_participant(dtype):
    """A foreground ``delegate_to`` IS a room address (#3102), so it carries the room's
    conversation key: the thread id. The lead's delegations and the operator's ``@``
    addresses to one participant are turns of ONE conversation on its side — an ACP
    session for a coding agent, the A2A ``contextId`` for an ``a2a`` peer (#3360).

    Pinned for both types because the two entry points share ``dispatch_into_room``: the
    PR that widened the room to ``a2a`` widened this path with it, and nothing else in the
    suite would have noticed."""
    registry = _Registry(dtype=dtype)
    await _dispatch_into_room(
        registry,
        "proto",
        "inspect auth",
        {"session_id": "room-key", "messages": [HumanMessage(content="please investigate auth")]},
        tool_call_id="call-key",
    )

    assert registry.calls[0]["conversation_key"] == "a2a:room-key"


@pytest.mark.asyncio
async def test_a_managed_git_claim_and_a_parked_resume_keep_their_own_conversation():
    """The two identities the room helper deliberately does not own fall back to
    ``plain()`` — no room record, and so no conversation key either. A managed-git claim
    is scoped to a work ITEM, and a resume answers ONE parked task in the context that task
    parked in; neither is the thread's continuing conversation."""
    for kwargs in ({"item_id": "issue-7"}, {"resume_task_id": "task-9"}):
        registry = _Registry(dtype="a2a")
        out = await _dispatch_into_room(
            registry,
            "proto",
            "carry on",
            {"session_id": "room-plain", "messages": [HumanMessage(content="hi")]},
            tool_call_id="call-plain",
            **kwargs,
        )
        assert isinstance(out, str), kwargs
        assert registry.calls[0]["conversation_key"] is None, kwargs
