"""The Tools tab is fed by the bound graph, not a re-derivation (bd-2aa, bd-67j).

`_operator_tools_list` reads `graph.bound_tools` (stamped by create_agent_graph),
so the operator's Tools inventory is exactly what the model can call — it can't
over-report (set_goal advertised-but-unbound) or under-report (task / filesystem
omitted). One source of truth.
"""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _graph(goal_enabled=True):
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig

    fake = _ToolFake(messages=iter([AIMessage(content="x")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        return create_agent_graph(LangGraphConfig(goal_enabled=goal_enabled))


def test_tools_tab_exactly_matches_the_bound_graph(monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs

    g = _graph(goal_enabled=True)
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)

    listed = {t["name"] for t in ch._operator_tools_list()["tools"]}
    bound = {getattr(t, "name", None) for t in g.bound_tools}

    assert listed == bound  # no drift, either direction
    assert "task" in listed  # subagent delegation now visible
    assert "read_file" in listed  # filesystem now visible (was omitted)
    assert "set_goal" in listed  # bound when goal_enabled (bd-2aa)


def test_tools_tab_omits_set_goal_when_goal_disabled(monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs

    g = _graph(goal_enabled=False)
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)

    listed = {t["name"] for t in ch._operator_tools_list()["tools"]}
    assert "set_goal" not in listed


def test_pre_setup_fallback_without_a_graph(monkeypatch):
    # Before the graph is compiled, degrade to the shared base instead of erroring.
    import operator_api.console_handlers as ch
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(rs.STATE, "knowledge_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "tasks_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)

    names = {t["name"] for t in ch._operator_tools_list()["tools"]}
    assert "current_time" in names  # the keyless base is always present
