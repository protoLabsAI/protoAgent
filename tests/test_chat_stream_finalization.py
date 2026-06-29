"""Streaming-turn finalization invariants (server.chat._run_native_turn):

1. Never end a turn on a SILENT empty answer — a native-reasoning model that emits only
   reasoning (no content) and no tool call must still surface a placeholder, matching the
   non-streaming path's _last_tool_text-or-placeholder fallback.
2. Same-context turns serialize on a per-thread_id lock (lost-update history guard).
"""

from __future__ import annotations

import itertools

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class _EmptyAnswerFake(GenericFakeChatModel):
    """Emits a native-reasoning chunk (reasoning_content) and NO answer content — a
    dropped/empty turn under native reasoning."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        next(self.messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking, nothing committed"})
        )


@pytest.mark.asyncio
async def test_empty_turn_yields_placeholder_not_silent(monkeypatch):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_native_turn

    fake = _EmptyAnswerFake(messages=itertools.repeat(AIMessage(content="")))
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    done = None
    async for kind, payload in _run_native_turn(
        "hi", "empty1", {"configurable": {"thread_id": "empty1"}}, request_metadata={}
    ):
        if kind == "done":
            done = payload
    assert done and "without a textual reply" in done  # never a silent empty answer


def test_thread_lock_is_per_thread_id():
    from server.chat import _thread_lock

    a1 = _thread_lock("ctx-a")
    a2 = _thread_lock("ctx-a")
    b = _thread_lock("ctx-b")
    assert a1 is a2  # same context_id → same lock (serializes concurrent same-context turns)
    assert a1 is not b  # different context_id → independent locks
