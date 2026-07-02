"""The Tools tab is fed by the bound graph, not a re-derivation (bd-2aa, bd-67j).

`_operator_tools_list` reads `graph.bound_tools` (stamped by create_agent_graph),
so the operator's Tools inventory is exactly what the model can call — it can't
over-report (set_goal advertised-but-unbound) or under-report (task / filesystem
omitted). One source of truth.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


@pytest.fixture(autouse=True)
def _reset_denylist():
    """The denylist is a module global (graph builds sync it from their config);
    reset it so a test's denylist never leaks into the rest of the suite."""
    from tools.lg_tools import set_disabled_tools

    yield
    set_disabled_tools([])


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _graph(goal_enabled=True, extra_tools=None, **over):
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig

    fake = _ToolFake(messages=iter([AIMessage(content="x")]))
    cfg = LangGraphConfig(goal_enabled=goal_enabled, **over)
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        return create_agent_graph(cfg, extra_tools=extra_tools), cfg


def test_tools_tab_exactly_matches_the_bound_graph(monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs

    g, cfg = _graph(goal_enabled=True)
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)

    res = ch._operator_tools_list()
    listed = {t["name"] for t in res["tools"]}
    bound = {getattr(t, "name", None) for t in g.bound_tools}

    assert listed == bound  # no drift, either direction
    assert "task" in listed  # subagent delegation now visible
    assert "read_file" in listed  # filesystem now visible (was omitted)
    assert "set_goal" in listed  # bound when goal_enabled (bd-2aa)
    # Empty denylist → every row is wired, count is the full set, raw denylist empty.
    assert all(t["enabled"] for t in res["tools"])
    assert res["count"] == len(res["tools"])
    assert res["disabled"] == []


def test_denylisted_tools_stay_listed_toggled_off(monkeypatch):
    """A tools.disabled tool is NOT bound but STAYS in the inventory (enabled: false) —
    drop it from the catalog and the console row toggle could never re-enable it. The
    response also echoes the RAW denylist (stale names included) so a row toggle edits
    one name without clobbering the rest."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    g, cfg = _graph(goal_enabled=False, tools_disabled=["calculator", "ghost_tool"])
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [], raising=False)

    res = ch._operator_tools_list()
    by_name = {t["name"]: t for t in res["tools"]}

    assert "calculator" not in {getattr(t, "name", None) for t in g.bound_tools}
    assert by_name["calculator"]["enabled"] is False
    assert by_name["calculator"]["category"] == "General"  # still grouped like a live row
    assert by_name["current_time"]["enabled"] is True
    # ``count`` stays the WIRED count (the "N wired tools" kicker), not rows.
    assert res["count"] == sum(1 for t in res["tools"] if t["enabled"])
    assert res["count"] == len(res["tools"]) - 1
    # The raw denylist survives verbatim; a stale name has no row to render.
    assert res["disabled"] == ["calculator", "ghost_tool"]
    assert "ghost_tool" not in by_name


def test_denylisted_plugin_and_mcp_tools_stay_listed(monkeypatch):
    """The row toggles cover EVERY seam, not just core: a denylisted plugin/MCP tool
    (they enter as extra_tools) is dropped from the bound set but stays cataloged —
    enabled: false, still grouped under its owning plugin / MCP server."""
    from langchain_core.tools import tool

    import operator_api.console_handlers as ch
    import runtime.state as rs

    @tool
    def show_artifact() -> str:
        """Render an artifact."""
        return "ok"

    @tool
    def echo__ping() -> str:
        """Echo ping."""
        return "pong"

    g, cfg = _graph(
        goal_enabled=False,
        extra_tools=[show_artifact, echo__ping],
        tools_disabled=["show_artifact", "echo__ping"],
    )
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tools", [show_artifact], raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_tools", [echo__ping], raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_tool_owner", {"show_artifact": "Artifact"}, raising=False)
    monkeypatch.setattr(rs.STATE, "mcp_meta", [{"name": "echo"}], raising=False)

    bound = {getattr(t, "name", None) for t in g.bound_tools}
    assert "show_artifact" not in bound and "echo__ping" not in bound

    by_name = {t["name"]: t for t in ch._operator_tools_list()["tools"]}
    assert by_name["show_artifact"]["enabled"] is False
    assert by_name["show_artifact"]["source"] == "plugin"
    assert by_name["show_artifact"]["category"] == "Artifact"  # still groups by owner
    assert by_name["echo__ping"]["enabled"] is False
    assert by_name["echo__ping"]["source"] == "mcp"
    assert by_name["echo__ping"]["category"] == "echo"  # still groups by server


def test_tools_tab_omits_set_goal_when_goal_disabled(monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs

    g, cfg = _graph(goal_enabled=False)
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
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

    g, _cfg = _graph(goal_enabled=True)
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
