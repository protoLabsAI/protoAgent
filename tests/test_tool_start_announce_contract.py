"""server/chat.py's `_run_turn_stream` legitimately yields `tool_start` TWICE per real
tool call for a streaming model: once early (the model's first streamed tool-call
token — empty args, so the console shows "running" immediately) and once more at
`on_chat_model_end` (the SAME tool_call id, now with full args, filling the card in).
This is by design (see the comments around both yield sites in `server/chat.py`).

`a2a_impl/executor.py` used to count every `tool_start` event as a distinct call,
silently doubling `TurnOutcome.tool_calls` (and therefore the durable telemetry
`tool_calls` column, the Telemetry dashboard, and `/perf`) for every real call made
by a streaming model. This test locks in the CONTRACT this fix depends on: exactly
two `tool_start` frames, sharing one id, per real tool call — so a future change to
the announce/finalize design gets caught here instead of silently reintroducing the
double-count. The executor's own dedup-by-id logic is covered separately in
`tests/test_telemetry_store.py::test_executor_counts_each_tool_call_once_despite_the_announce_then_finalize_pair`.
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
async def test_a_real_tool_call_announces_twice_by_the_same_id(monkeypatch):
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

    tool_starts = []
    async for kind, payload in _run_turn_stream(
        "delegate then answer", "announce1", {"configurable": {"thread_id": "announce1"}}
    ):
        if kind == "tool_start":
            tool_starts.append(payload)

    task_starts = [p for p in tool_starts if p.get("name") == "task"]
    assert len(task_starts) == 2, f"expected the announce+finalize pair; saw {tool_starts}"
    ids = {p["id"] for p in task_starts}
    assert len(ids) == 1, f"both announcements must share one tool_call id; saw {task_starts}"
    # Announce carries no args yet; finalize fills them in.
    inputs = [p.get("input") for p in task_starts]
    assert "" in inputs, f"expected an empty-args announce frame; saw {task_starts}"
    assert any(i for i in inputs if i), f"expected a full-args finalize frame; saw {task_starts}"
