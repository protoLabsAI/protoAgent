"""Mid-turn steering (spike) — fold queued user input into a RUNNING turn at the
next model call, so a user can redirect ongoing work without stopping the stream.

graph/steering.py (the per-session queue) + graph/middleware/steering.py
(SteeringMiddleware.before_model, which drains it).
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from graph import steering
from graph.middleware.steering import SteeringMiddleware


@pytest.fixture(autouse=True)
def _clear_queue():
    steering._QUEUES.clear()
    yield
    steering._QUEUES.clear()


# ── the queue ─────────────────────────────────────────────────────────────────


def test_enqueue_drain_roundtrip_fifo():
    steering.enqueue("s1", "first", msg_id="a")
    steering.enqueue("s1", "second", msg_id="b")
    assert steering.pending("s1") == 2
    assert steering.pending_items("s1") == [{"id": "a", "text": "first"}, {"id": "b", "text": "second"}]
    assert steering.drain("s1") == [{"id": "a", "text": "first"}, {"id": "b", "text": "second"}]
    assert steering.pending("s1") == 0
    assert steering.drain("s1") == []  # draining an empty queue is safe


def test_enqueue_returns_id_and_mints_one_when_absent():
    assert steering.enqueue("s1", "x", msg_id="given") == "given"
    minted = steering.enqueue("s1", "y")
    assert minted and minted != "given"


def test_enqueue_ignores_blanks_and_missing_session():
    assert steering.enqueue("s1", "   ") is None
    assert steering.enqueue("", "x") is None
    assert steering.pending("s1") == 0
    assert steering.drain("") == []


def test_queues_are_per_session():
    steering.enqueue("a", "for-a", msg_id="1")
    steering.enqueue("b", "for-b", msg_id="2")
    assert steering.drain("a") == [{"id": "1", "text": "for-a"}]
    assert steering.drain("b") == [{"id": "2", "text": "for-b"}]


# ── the middleware (unit) ──────────────────────────────────────────────────────


def test_middleware_injects_queued_then_clears():
    steering.enqueue("sess", "actually, focus on X")
    steering.enqueue("sess", "and skip the tests")
    out = SteeringMiddleware._inject({"session_id": "sess", "messages": []})
    assert out is not None
    msgs = out["messages"]
    # All queued text is combined into ONE framed interjection HumanMessage.
    assert len(msgs) == 1 and isinstance(msgs[0], HumanMessage)
    assert msgs[0].content.startswith("[User message received")  # advisory framing
    assert "actually, focus on X" in msgs[0].content
    assert "and skip the tests" in msgs[0].content
    # drained — a second pass on the same turn is a no-op
    assert SteeringMiddleware._inject({"session_id": "sess", "messages": []}) is None


def test_middleware_noop_without_session_or_queue():
    assert SteeringMiddleware._inject({"messages": []}) is None  # no session_id
    assert SteeringMiddleware._inject({"session_id": "sess", "messages": []}) is None  # empty queue


# ── end-to-end in a REAL graph ─────────────────────────────────────────────────
# Drive a real create_agent graph: model call 1 invokes a tool; while the tool
# "runs" the user steers (enqueue); SteeringMiddleware.before_model must fold that
# message into the thread before model call 2, so it lands AFTER the tool result
# and BEFORE the final answer — exactly the next-tool-call boundary.


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools (returns itself) so it drops into
    create_agent and replays preset AIMessages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_steering_message_injected_mid_turn():
    from unittest.mock import patch

    from langgraph.checkpoint.memory import MemorySaver

    from graph.config import LangGraphConfig

    # The tool simulates the user steering WHILE it runs (mid-turn injection).
    @tool
    def long_task() -> str:
        """A long step; the user redirects the agent while it runs."""
        steering.enqueue("sess-1", "actually, do X instead")
        return "long_task step done"

    fake = _ToolFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "long_task", "args": {}, "id": "c1", "type": "tool_call"}],
                ),
                AIMessage(content="Understood — switching to X."),
            ]
        )
    )

    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(
            LangGraphConfig(),
            include_subagents=False,
            checkpointer=MemorySaver(),
            extra_tools=[long_task],
        )

    result = await graph.ainvoke(
        {"messages": [HumanMessage("start the long task")], "session_id": "sess-1"},
        config={"configurable": {"thread_id": "t1"}},
    )
    msgs = result["messages"]

    # The steering message was injected into the live thread (not the queue) — framed
    # as a mid-task interjection that carries the user's text.
    steer = [m for m in msgs if isinstance(m, HumanMessage) and "actually, do X instead" in m.content]
    assert len(steer) == 1, "steering message should be folded into the running turn exactly once"

    # …and at the right point: after the tool result, before the final answer.
    idx_steer = msgs.index(steer[0])
    idx_tool = max(i for i, m in enumerate(msgs) if isinstance(m, ToolMessage))
    idx_final = max(i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and m.content)
    assert idx_tool < idx_steer < idx_final, "steering must land between the tool result and the final answer"

    # The queue is drained — nothing left to leak into a later turn.
    assert steering.pending("sess-1") == 0
