"""Goal kickoff + headless-first, driven through the REAL streaming turn runner (#1910/#1911).

This is the end-to-end path the issues said had never run: a real ``/goal`` over the A2A
streaming entry (``server.chat._chat_langgraph_stream``), with a real ``GoalController``
wired into ``STATE``, a real ``create_agent_graph`` (fake chat model), and the real
checkpointer — asserting:

  #1910 — a ``/goal <text>`` SET KICKS a drive turn (not just an ack), and the graph's
          first HumanMessage carries the injected goal condition (kickoff), so the agent
          begins on the goal instead of asking "what goal?".
  #1911 — a goal-driven turn that hits a HITL form does NOT park at input_required; it
          auto-answers (no operator) and completes the drive.

Reuses the fake-model + graph harness shape from ``test_hitl_hold.py``.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from runtime.state import STATE

chat_mod = importlib.import_module("server.chat")


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools and emits tool calls (see test_hitl_hold)."""

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk

        message = next(self.messages)
        tool_call_chunks = [
            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i, "type": "tool_call_chunk"}
            for i, tc in enumerate(getattr(message, "tool_calls", []) or [])
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content or "", tool_call_chunks=tool_call_chunks)
        )


class _FakeJudge:
    """Goal-eval model stand-in — returns the llm verifier's judge JSON."""

    def __init__(self, met: bool) -> None:
        self._met = met

    async def ainvoke(self, _messages, config=None):  # noqa: ARG002 — signature parity
        return type("R", (), {"content": json.dumps({"met": self._met, "reason": "faked"})})()


def _form_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "request_user_input",
                "args": {"title": "Pick env", "steps": [{"schema": {"type": "object", "properties": {}}}]},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _install(monkeypatch, model_messages, *, judge_met: bool, max_iters: int = 1):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from langgraph.checkpoint.memory import MemorySaver

    cfg = LangGraphConfig(goal_max_iterations=max_iters)
    fake = _ToolFake(messages=iter(model_messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", GoalController(cfg, GoalStore(str(_tmp()))), raising=False)
    # The goal's llm verifier reaches out via graph.llm.create_llm — fake its verdict.
    monkeypatch.setattr("graph.llm.create_llm", lambda *a, **k: _FakeJudge(judge_met))


def _tmp():
    import tempfile

    return tempfile.mkdtemp()


async def _frames(message, session_id, *, request_metadata=None):
    return [
        frame
        async for frame in chat_mod._chat_langgraph_stream(message, session_id, request_metadata=request_metadata)
    ]


async def _history(session_id):
    snap = await STATE.graph.aget_state({"configurable": {"thread_id": f"a2a:{session_id}"}})
    return list(snap.values.get("messages", []))


@pytest.mark.asyncio
async def test_goal_set_kicks_and_injects_condition(monkeypatch):
    """#1910: /goal SET kicks a turn whose graph input carries the goal condition."""
    _install(monkeypatch, [AIMessage(content="On it — starting now.")], judge_met=True)

    frames = await _frames("/goal ship the release notes", "g1")

    kinds = [k for k, _ in frames]
    # Kicked a real turn (not a bare ack short-circuit): a terminal done frame exists.
    assert "done" in kinds
    # The ack was surfaced as a status frame before the drive.
    assert any(k == "tool_start" and "Goal set." in str(p) for k, p in frames)

    # The graph's first user message is the KICKOFF, carrying the goal condition — the agent
    # was told its goal instead of receiving a bare "/goal ..." it can't act on.
    history = await _history("g1")
    first_human = next(m for m in history if isinstance(m, HumanMessage))
    assert "ship the release notes" in str(first_human.content)
    assert "kickoff" in str(first_human.content).lower()
    assert "/goal ship the release notes" not in str(first_human.content)

    # Terminal goal outcome rode out on the final answer (drive actually ran + verified).
    done_text = next(p for k, p in frames if k == "done")
    assert "goal" in str(done_text).lower()


@pytest.mark.asyncio
async def test_goal_turn_does_not_park_on_hitl(monkeypatch):
    """#1911: a goal-driven turn hitting a HITL form auto-answers instead of parking."""
    # First model step opens a form (parks the graph); autonomous goal turn resumes past it
    # with the no-operator sentinel, then the second step completes the turn.
    _install(
        monkeypatch,
        [_form_call(), AIMessage(content="Done — release notes shipped.")],
        judge_met=True,
        max_iters=2,
    )

    frames = await _frames("/goal ship the release notes", "g2")
    kinds = [k for k, _ in frames]

    # Headless-first invariant: the turn must NOT end parked at input_required.
    assert kinds[-1] != "input_required"
    assert "done" in kinds
    # And the form interrupt must not be left dangling on the thread.
    assert await chat_mod._pending_interrupt_value({"configurable": {"thread_id": "a2a:g2"}}) is None
