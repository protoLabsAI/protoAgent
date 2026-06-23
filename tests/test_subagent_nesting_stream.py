"""task-tool-rendering audit #4 (FIXED): a subagent's OWN tool activity nests under the
`task` card in the live stream. Drives the real `_run_turn_stream` frame emitter with a
fake model scripting lead → task → subagent → current_time → done.

The delegation runs detached (asyncio.ensure_future, for Tier-2 cancellation), so the
task's on_tool_end can race ahead of the subagent's child frames — defeating the console's
old "last open task wins" heuristic (which also mis-attributes concurrent task_batch
delegations). The fix tags the subagent's run with the delegating task's tool-call id
(`_run_subagent` sets it as run metadata), so every child frame carries `parentId` and the
console nests by id, order-independent. This test asserts the linkage on the wire frames.
"""

from __future__ import annotations

import itertools
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class _ToolFake(GenericFakeChatModel):
    """Replays preset AIMessages (incl. tool calls) over the STREAMING path so it drops
    into create_agent for BOTH the lead and the subagent (same patched create_llm). The
    stock GenericFakeChatModel chunks `content` and yields nothing for an empty-content
    tool-call message — which breaks astream_events — so we emit one chunk carrying the
    tool calls as `tool_call_chunks` (the wire shape the agent re-aggregates)."""

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
        # Yield control like a real model's network I/O would, so astream_events can
        # flush the detached subagent's events in real-time order (not a sync burst).
        import asyncio

        await asyncio.sleep(0)
        chunk = self._chunk()
        await asyncio.sleep(0)
        yield chunk


def _install(monkeypatch, messages):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    # Pad with a no-tool-call finisher forever so an extra lead/subagent step (a retry,
    # a structured-output kicker) ends cleanly instead of exhausting the script.
    stream = itertools.chain(iter(messages), itertools.repeat(AIMessage(content="<output>done</output>")))
    fake = _ToolFake(messages=stream)
    # Persist the fake for the whole turn — the subagent builds ITS model lazily at
    # runtime (in _run_subagent), so a patch that exits after construction would leave the
    # subagent on the real gateway model (and miss the parent-id metadata propagation).
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    g = create_agent_graph(LangGraphConfig(), include_subagents=True, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


def _delegate(**args):
    return AIMessage(
        content="",
        tool_calls=[{"name": "task", "args": args, "id": "t1", "type": "tool_call"}],
    )


@pytest.mark.asyncio
async def test_subagent_tool_calls_nest_via_explicit_parent_id(monkeypatch):
    from server.chat import _run_turn_stream

    _install(
        monkeypatch,
        [
            _delegate(description="check the time", prompt="what time is it", subagent_type="researcher"),
            # subagent's turn 1 → call a nested tool (current_time is in researcher's allowlist)
            AIMessage(content="", tool_calls=[{"name": "current_time", "args": {}, "id": "s1", "type": "tool_call"}]),
            # subagent's turn 2 → finish
            AIMessage(content="it is noon"),
            # lead's turn 2 → finish
            AIMessage(content="<output>the subagent says it is noon</output>"),
        ],
    )

    # Capture every tool frame with its parent linkage. The fix tags a subagent's own
    # frames with the delegating task's tool-call id (server-side), so the console nests
    # them BY ID rather than by timing.
    frames: list[tuple[str, str, str | None]] = []  # (kind, name, parentId)
    seq: list[tuple[str, str]] = []
    async for kind, payload in _run_turn_stream("ask the researcher the time", "s4", {"configurable": {"thread_id": "s4"}}):
        if kind not in ("tool_start", "tool_end"):
            continue
        frames.append((kind, payload.get("name"), payload.get("parentId")))
        seq.append(("start" if kind == "tool_start" else "end", payload.get("name")))

    print(f"\n[#4] frames: {frames}")
    starts = [n for k, n in seq if k == "start"]
    ends = [n for k, n in seq if k == "end"]
    assert "task" in starts, f"the delegation itself should surface a card; saw {frames}"
    # The subagent's OWN tool call surfaces in the parent stream (propagation works)…
    assert "current_time" in starts and "current_time" in ends, f"subagent tool didn't surface; {frames}"

    # …and EVERY child frame carries the parent delegation's tool-call id ("t1"), so the
    # console nests it under the `task` card BY ID — order-independent. (The delegation
    # runs detached via ensure_future, so the task's on_tool_end can race ahead of the
    # child frames; the old "last open task wins" heuristic broke on that and on
    # concurrent task_batch delegations. Explicit linkage fixes both.)
    child_parents = {p for k, n, p in frames if n == "current_time"}
    assert child_parents == {"t1"}, f"subagent tool must carry parentId=t1; saw {child_parents} in {frames}"
    # The delegation card itself stays top-level (no parent).
    task_parents = {p for k, n, p in frames if n == "task"}
    assert task_parents == {None}, f"the task card must not be nested; saw {task_parents}"
