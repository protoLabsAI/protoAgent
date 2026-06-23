"""Native reasoning streams on its own channel, BEFORE the answer. The gateway emits
``reasoning_content`` deltas (lifted into additional_kwargs by
``graph.llm._ReasoningChatOpenAI``), and ``server.chat`` streams them as ``reasoning``
frames ahead of the content ``text`` frames — so the console (which renders parts in
emission order) shows the model's real thinking above the answer. No ``<scratch_pad>`` /
``<output>`` text protocol is involved anymore.
"""

from __future__ import annotations

import itertools

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class _ReasoningFake(GenericFakeChatModel):
    """Streams a native-reasoning chunk (``reasoning_content``, no content) followed by the
    answer chunk — the shape ``_ReasoningChatOpenAI`` surfaces from a reasoning gateway."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = next(self.messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "Group the tools by domain."})
        )
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))


@pytest.mark.asyncio
async def test_native_reasoning_streams_before_the_answer(monkeypatch):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_turn_stream

    stream = itertools.chain(
        iter([AIMessage(content="Here are the tools.")]),
        itertools.repeat(AIMessage(content="done")),
    )
    fake = _ReasoningFake(messages=stream)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    frames: list[tuple[str, str]] = []
    async for kind, payload in _run_turn_stream("what tools do you have", "r1", {"configurable": {"thread_id": "r1"}}):
        if kind in ("reasoning", "text"):
            frames.append((kind, payload if isinstance(payload, str) else str(payload)))

    kinds = [k for k, _ in frames]
    assert "reasoning" in kinds, f"native reasoning_content must surface as a reasoning frame; saw {frames}"
    assert "text" in kinds, f"the answer must surface as a text frame; saw {frames}"
    # Reasoning is emitted before the answer → renders above it.
    assert kinds.index("reasoning") < kinds.index("text"), f"reasoning must precede the answer; saw {kinds}"
    # The reasoning frame carries the model's NATIVE reasoning (not a parsed scratch_pad).
    assert any(k == "reasoning" and "Group the tools" in v for k, v in frames), f"reasoning text missing; saw {frames}"
    # The answer is the model's content, streamed directly (no <output> wrapper).
    assert any(k == "text" and "Here are the tools" in v for k, v in frames), f"answer text missing; saw {frames}"
