"""#2697 — per-tool execution latency. `_run_turn_stream`'s on_tool_start/on_tool_end
handling stamps a monotonic start on `tool_start` and computes `duration_ms` on the
matching `tool_end`, mirroring the existing `_llm_started` idiom for LLM-call latency.
This is the one hop in the by-tool pipeline (chat.py → executor.py → telemetry_store.py)
not already covered by an aggregation-level test — those live in test_telemetry_store.py.

Drives the real `_run_turn_stream` frame emitter with a fake model that delegates via the
`task` tool (a real, already-registered tool), the same harness as
test_subagent_stream_isolation.py, so `on_tool_start`/`on_tool_end` fire for real.
"""

from __future__ import annotations

import itertools
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


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
        chunk = self._chunk()
        await asyncio.sleep(0)
        yield chunk


def _delegate(**args):
    return AIMessage(
        content="",
        tool_calls=[{"name": "task", "args": args, "id": "t1", "type": "tool_call"}],
    )


@pytest.mark.asyncio
async def test_tool_end_frame_carries_a_positive_duration(monkeypatch):
    import runtime.state as rs
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver
    from server.chat import _run_turn_stream

    stream = itertools.chain(
        [
            _delegate(description="research a topic", prompt="go research", subagent_type="researcher"),
            AIMessage(content="subagent draft"),
            AIMessage(content="lead final answer"),
        ],
        itertools.repeat(AIMessage(content="<output>done</output>")),
    )
    fake = _ToolFake(messages=stream)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)

    g = create_agent_graph(LangGraphConfig(), include_subagents=True, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    tool_ends = []
    async for kind, payload in _run_turn_stream(
        "delegate then answer", "lat1", {"configurable": {"thread_id": "lat1"}}
    ):
        if kind == "tool_end":
            tool_ends.append(payload)

    task_ends = [p for p in tool_ends if p.get("name") == "task"]
    assert task_ends, f"expected a tool_end frame for the task tool; saw {tool_ends}"
    # A real tool call always takes measurable time — never exactly 0, which is
    # reserved for "unmeasured" (see a2a_impl/executor.py's tool_durations guard).
    assert all(isinstance(p.get("duration_ms"), int) and p["duration_ms"] > 0 for p in task_ends), (
        f"tool_end frames must carry a positive duration_ms; saw {task_ends}"
    )
