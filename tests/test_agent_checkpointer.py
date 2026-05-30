"""Regression: the agent graph must be compiled WITH the checkpointer so
multi-turn chats keep their history instead of starting fresh each message.

A checkpointer passed only in the invoke `config` is ignored by LangGraph — it
must be bound at compile time. Missing this gave the agent amnesia: every turn
ran with just the new message, so it couldn't resolve references to prior turns
("update it") or recall earlier context.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from graph.agent import create_agent_graph
from graph.config import LangGraphConfig


def test_graph_binds_checkpointer_at_compile_time():
    g = create_agent_graph(LangGraphConfig(), checkpointer=MemorySaver())
    assert g.checkpointer is not None


def test_graph_has_no_checkpointer_when_none_passed():
    assert create_agent_graph(LangGraphConfig()).checkpointer is None
