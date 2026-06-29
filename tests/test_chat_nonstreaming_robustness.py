"""Non-streaming chat (/api/chat + OpenAI-compat) robustness — bd-2qy.

A turn can end with no assistant text: at an ask_human interrupt, after a `wait`
yield, or on a scratch-only turn. The non-streaming path used to return a silent
empty 200 in all three cases (the streaming/A2A path handled them). These drive
the REAL graph (a fake model emitting the relevant tool call / output) through
``server.chat.chat`` and assert it never returns a blank reply.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools (returns itself) so it can drop
    into create_agent and replay preset AIMessages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


class _FakeJob:
    def __init__(self, next_fire: str):
        self.id = "job-1"
        self.next_fire = next_fire


class _FakeScheduler:
    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        return _FakeJob(schedule)

    def list_jobs(self):
        return []

    def cancel_job(self, job_id):
        return True


def _install_graph(monkeypatch, messages, scheduler=None):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ToolFake(messages=iter(messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(
            LangGraphConfig(),
            scheduler=scheduler,
            include_subagents=False,
            checkpointer=MemorySaver(),
        )
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


@pytest.mark.asyncio
async def test_ask_human_interrupt_surfaces_the_question(monkeypatch):
    from server.chat import chat

    _install_graph(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_human",
                        "args": {"question": "What timezone are you in?"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
        ],
    )
    out = await chat("ask me my timezone", "sessA")
    content = out[0]["content"]
    assert content, "interrupt must not return an empty reply"
    assert "Input needed" in content and "timezone" in content.lower()


@pytest.mark.asyncio
async def test_wait_yield_turn_falls_back_to_tool_text(monkeypatch):
    from server.chat import chat

    _install_graph(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "wait", "args": {"seconds": 30, "then": "resume"}, "id": "c1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="unused"),
        ],
        scheduler=_FakeScheduler(),
    )
    out = await chat("wait a bit then resume", "sessC")
    content = out[0]["content"]
    assert content and "Yielding" in content  # not a blank reply
