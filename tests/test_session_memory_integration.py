"""End-to-end: the session-memory middlewares actually FIRE in a real graph turn.

The other ``test_memory_*`` suites call the hooks directly (and monkeypatch the
loader), so they don't prove the middleware is wired and fires when the real
graph runs. These drive the REAL ``create_agent_graph`` (fake model, no gateway)
and assert:

  1. ``SessionSummaryMiddleware`` persists a session summary to disk after a turn
     (its ``after_agent`` hook runs in the real loop).
  2. ``KnowledgeMiddleware`` reads that summary back and injects a
     ``<prior_sessions>`` block on a NEW session (its ``before_model`` fires and
     writes the turn ``context``).

Regression guard for the #1247/#1249 consolidation: ``SessionSummaryMiddleware``
is write-only and ``KnowledgeMiddleware`` solely owns the prior-sessions
read/inject path.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage


def _make_fake(replies):
    class _Fake(GenericFakeChatModel):
        # create_agent binds tools onto the model; return self so the fake stays in place.
        def bind_tools(self, tools, **kwargs):
            return self

    return _Fake(messages=iter(replies))


def _build_graph(monkeypatch, memory_dir, store, replies):
    """Build the real agent graph with a fake model + a temp MEMORY_PATH."""
    import graph.agent as agentmod
    import graph.middleware.memory as memmod
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    # test_memory_persistence importlib.reload()s this module under various env,
    # which can leave the module in a polluted state for whatever runs after it:
    #  - _PERSISTENCE_DISABLED left True (a reload with PROTOAGENT_DISABLE_MEMORY=1),
    #  - graph.agent's imported class pointing at the pre-reload version.
    # Pin both, and route memory_path() at the temp dir (MEMORY_PATH env wins, read
    # lazily), so this test is independent of run order and CI-vs-local env.
    monkeypatch.setenv("MEMORY_PATH", str(memory_dir))
    monkeypatch.setattr(memmod, "_PERSISTENCE_DISABLED", False, raising=False)
    monkeypatch.setattr(agentmod, "SessionSummaryMiddleware", memmod.SessionSummaryMiddleware, raising=False)

    with patch("graph.agent.create_llm", lambda *a, **k: _make_fake(replies)):
        from graph.agent import create_agent_graph

        return create_agent_graph(
            LangGraphConfig(),
            knowledge_store=store,  # required to wire KnowledgeMiddleware
            include_subagents=False,
            checkpointer=MemorySaver(),
        )


@pytest.mark.asyncio
async def test_session_memory_persists_and_injects_in_a_real_turn(tmp_path, monkeypatch):
    from knowledge.store import KnowledgeStore

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    store = KnowledgeStore(str(tmp_path / "kb.db"))

    # --- Session A: a normal turn. SessionSummaryMiddleware should persist it. ---
    g_a = _build_graph(monkeypatch, memory_dir, store, [AIMessage(content="Noting that the sky is teal today.")])
    await g_a.ainvoke(
        {"messages": [HumanMessage(content="remember the sky is teal")], "session_id": "sessA"},
        config={"configurable": {"thread_id": "sessA"}},
    )
    summaries = [f for f in os.listdir(memory_dir) if f.endswith(".json")]
    assert summaries, "SessionSummaryMiddleware did not persist a summary — it never fired in the real loop"
    assert any("sessA" in f for f in summaries), f"expected a sessA summary, got {summaries}"

    # --- Session B (fresh graph → fresh prior-sessions cache): KnowledgeMiddleware
    #     should read A's summary and inject it as <prior_sessions> turn context. ---
    g_b = _build_graph(monkeypatch, memory_dir, store, [AIMessage(content="ok")])
    result = await g_b.ainvoke(
        {"messages": [HumanMessage(content="hi again")], "session_id": "sessB"},
        config={"configurable": {"thread_id": "sessB"}},
    )
    context = result.get("context") or ""
    assert "<prior_sessions>" in context, "KnowledgeMiddleware did not inject <prior_sessions> — it never fired"
    assert "teal" in context, "prior session content (from sessA) was not carried into the new session"
