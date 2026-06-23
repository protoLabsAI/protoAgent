"""task-tool-rendering audit #4: does a subagent's OWN tool activity nest under the
`task` card in the live stream? Drives the real `_run_turn_stream` frame emitter with a
fake model scripting lead → task → subagent → current_time → done, and inspects the
interleaved frame order.

FINDING: the subagent's tool frames DO propagate into the parent stream (good), but the
`task` tool's on_tool_end is emitted BEFORE them (because the delegation is detached via
ensure_future for cancellation). The console nests by "last open task wins", which needs
the task still running when the child starts — so the child arrives too late and renders
as a top-level card, never nested. The nesting rail is effectively dead code for streamed
delegations until child frames carry an explicit parent-task id. This test pins that real
ordering so a future linkage fix trips it.
"""

from __future__ import annotations

import itertools
import json
from unittest.mock import patch

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
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
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
async def test_subagent_tool_calls_surface_but_do_not_nest_live(monkeypatch):
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

    # Track the interleaved frame order AND, per frame, which `task` cards are still
    # open — replaying the console's "last open task wins" nesting (ChatSurface): a
    # child nests only if a `task` is still running when its start arrives.
    seq: list[tuple[str, str]] = []
    open_tasks: set[str] = set()
    nested_under_task = False
    for_status: dict[str, str] = {}  # id → running/closed (dedupe the twin start frames)
    async for kind, payload in _run_turn_stream("ask the researcher the time", "s4", {"configurable": {"thread_id": "s4"}}):
        if kind not in ("tool_start", "tool_end"):
            continue
        name, tid = payload.get("name"), payload.get("id")
        if kind == "tool_start":
            seq.append(("start", name))
            if name == "task":
                open_tasks.add(tid)
            elif open_tasks and for_status.get(tid) is None:
                nested_under_task = True  # a non-task tool started while a task was open
            for_status.setdefault(tid, "running")
        elif kind == "tool_end":
            seq.append(("end", name))
            open_tasks.discard(tid)

    print(f"\n[#4] interleaved frames: {seq}")
    starts = [n for k, n in seq if k == "start"]
    ends = [n for k, n in seq if k == "end"]
    assert "task" in starts, f"the delegation itself should surface a card; saw {seq}"

    # Propagation works: the subagent's OWN tool call surfaces in the parent stream.
    assert "current_time" in starts, f"subagent's nested tool did not surface; saw {seq}"
    assert "current_time" in ends, f"subagent's nested tool never closed; saw {seq}"

    # KNOWN LIMITATION (audit #4) — the delegation runs its subagent via
    # asyncio.ensure_future + await (for Tier-2 cancellation), which DETACHES it, so in
    # astream_events the task's on_tool_end is emitted BEFORE the subagent's child
    # frames. The child therefore arrives after the task card has already closed, and
    # the console's "last open task wins" nesting can't attach it — subagent tools
    # render as top-level cards, NOT nested under the delegation. The nesting rail
    # (parentId / pl-toolcard__children) is effectively unreachable for streamed
    # delegations. The real fix is to tag child frames with their parent task's
    # tool_call_id (a delegation contextvar) so nesting is explicit, not timing-based.
    # These assertions pin the current ordering so that fix trips this test.
    task_end_i = seq.index(("end", "task"))
    child_start_i = next(i for i, f in enumerate(seq) if f == ("start", "current_time"))
    assert task_end_i < child_start_i, f"task should close before its child today; saw {seq}"
    assert not nested_under_task, f"nesting unexpectedly worked — update this test + audit; saw {seq}"
