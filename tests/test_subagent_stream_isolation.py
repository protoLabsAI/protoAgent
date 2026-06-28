"""Regression: a subagent's CONTENT (and reasoning) tokens must NOT stream into the
lead turn's answer.

LangChain propagates the parent run's callbacks into the nested subagent ``ainvoke``,
so the subagent's ``on_chat_model_stream`` events bubble up onto the lead's
``astream_events`` loop in ``_run_turn_stream``. Without a guard the loop forwarded them
as ``("text"/"reasoning")`` frames — streaming the subagent's internals into the lead
answer (polluting ``accumulated_raw``) and, under ``task_batch``, interleaving every
concurrent subagent's tokens character-by-character (the garbled-output bug).

The fix suppresses forwarding for any chat-model-stream event carrying ``parent_task_id``
(a subagent run). The subagent's result still comes back via the ``task`` tool's
ToolMessage (a ``tool_end`` frame → the delegation card), which is the correct handoff —
so the secret below is expected on the tool card, just never on the lead's text stream.

Drives the real ``_run_turn_stream`` frame emitter with a fake model scripting
lead → task → subagent → lead-answer, mirroring ``test_subagent_nesting_stream``.
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
    into create_agent for BOTH the lead and the subagent (same patched create_llm). Emits
    one chunk carrying any tool calls as ``tool_call_chunks`` (the wire shape the agent
    re-aggregates) so an empty-content tool-call message still yields a stream event."""

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
        # Yield control like a real model's network I/O would, so astream_events flushes
        # the detached subagent's events in real-time order (not a sync burst).
        import asyncio

        await asyncio.sleep(0)
        chunk = self._chunk()
        await asyncio.sleep(0)
        yield chunk


def _install(monkeypatch, messages):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    # Pad with a no-tool-call finisher forever so an extra lead/subagent step ends cleanly.
    stream = itertools.chain(iter(messages), itertools.repeat(AIMessage(content="<output>done</output>")))
    fake = _ToolFake(messages=stream)
    # Persist the fake for the whole turn — the subagent builds ITS model lazily in
    # _run_subagent, so a patch that exits after construction would miss it.
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
async def test_subagent_content_does_not_leak_into_lead_stream(monkeypatch):
    from server.chat import _run_turn_stream

    sub_secret = "SUBAGENT_INTERNAL_DRAFT_XYZ"
    lead_answer = "LEAD_FINAL_ANSWER_ABC"
    _install(
        monkeypatch,
        [
            _delegate(description="research a topic", prompt="go research", subagent_type="researcher"),
            # subagent's only turn → produces content (its draft/answer). THIS is what
            # leaked into the lead view in the bug — it must stay out of the text stream.
            AIMessage(content=sub_secret),
            # lead's turn 2 → its real answer (streams as normal).
            AIMessage(content=lead_answer),
        ],
    )

    text_frames: list[str] = []
    reasoning_frames: list[str] = []
    tool_outputs: list[str] = []
    async for kind, payload in _run_turn_stream(
        "delegate then answer", "iso1", {"configurable": {"thread_id": "iso1"}}
    ):
        if kind == "text":
            text_frames.append(payload)
        elif kind == "reasoning":
            reasoning_frames.append(payload)
        elif kind == "tool_end":
            tool_outputs.append(str(payload.get("output", "")))

    streamed = "".join(text_frames)
    # The lead's own answer still streams to the user.
    assert lead_answer in streamed, f"lead answer should stream; saw {streamed!r}"
    # …but the subagent's internal content must NOT appear in the lead's text or
    # reasoning stream (this assertion fails on the pre-fix code).
    assert sub_secret not in streamed, f"subagent content leaked into the lead text stream: {streamed!r}"
    assert sub_secret not in "".join(reasoning_frames), "subagent content leaked into the lead reasoning stream"
    # The subagent's result is still delivered — as the `task` tool result (delegation
    # card), which is the correct handoff path, not the lead's answer stream.
    assert any(sub_secret in out for out in tool_outputs), (
        f"subagent result should return via the task tool card; tool outputs: {tool_outputs}"
    )
