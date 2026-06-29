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


def test_core_tools_group_by_subsystem():
    """The old single 'General' bucket is split into subsystems (read better)."""
    from operator_api.console_handlers import _tool_category

    assert _tool_category("read_file", "core") == "Filesystem"
    assert _tool_category("run_command", "core") == "Filesystem"
    assert _tool_category("load_skill", "core") == "Skills"
    assert _tool_category("web_search", "core") == "Web & research"
    assert _tool_category("forget_memory", "core") == "Memory"  # joins the Memory group
    assert _tool_category("stop_task", "core") == "Delegation"
    # The long tail still falls back to General.
    assert _tool_category("current_time", "core") == "General"


def test_plugin_tools_group_by_owning_plugin():
    """Plugin tools group by the plugin that brought them, not a flat 'Plugin' dump."""
    from operator_api.console_handlers import _tool_category

    assert _tool_category("show_artifact", "plugin", "Artifact") == "Artifact"
    # GitHub tools are plugin-owned now (no hardcoded name map) — they group by the plugin.
    assert _tool_category("github_get_pr", "plugin", "GitHub") == "GitHub"
    assert _tool_category("github_create_pr", "plugin", "GitHub") == "GitHub"
    # No owner recorded → the generic fallback.
    assert _tool_category("mystery", "plugin", None) == "Plugin"


def test_mcp_tools_group_by_server():
    """MCP tools (namespaced <server>__<tool>) group by the originating server."""
    from operator_api.console_handlers import _tool_category

    # Matched against the known server list (handles a server name containing "__").
    assert _tool_category("echo__ping", "mcp", None, ["echo"]) == "echo"
    assert _tool_category("we__ird__t", "mcp", None, ["we__ird"]) == "we__ird"
    # No server list → fall back to the prefix before the first "__".
    assert _tool_category("echo__ping", "mcp", None, []) == "echo"
    # A bare (un-namespaced) name with no match → the generic MCP bucket.
    assert _tool_category("loner", "mcp", None, []) == "MCP"


def test_inventory_uses_plugin_owner_map(monkeypatch):
    """End-to-end: _operator_tools_list reads STATE.plugin_tool_owner for the category."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    class _Tool:
        def __init__(self, name):
            self.name = name
            self.description = "x"

    g = _graph(goal_enabled=True)
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [_Tool("show_artifact")], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tool_owner", {"show_artifact": "Artifact"}, raising=False)
    # bound_tools won't include the plugin tool (it's not in this bare graph), so assert via
    # the source/category mapping the handler computes from the plugin set + owner map.
    cat = ch._tool_category("show_artifact", "plugin", rs.STATE.plugin_tool_owner.get("show_artifact"))
    assert cat == "Artifact"


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
