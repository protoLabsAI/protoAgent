"""Operator MCP server (ADR 0033 slice 1) — allowlist-gated tool exposure."""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from graph.config import LangGraphConfig
from runtime.state import STATE
from server.operator_mcp import build_server, operator_tools


def _cfg(tools):
    c = LangGraphConfig()
    c.operator_mcp_tools = list(tools)
    c.goal_enabled = False
    return c


@pytest.fixture(autouse=True)
def _bare_state(monkeypatch):
    # No stores → get_all_tools returns just the keyless core tools; no plugin tools.
    for attr in ("knowledge_store", "scheduler", "inbox_store", "beads_store"):
        monkeypatch.setattr(STATE, attr, None, raising=False)
    monkeypatch.setattr(STATE, "plugin_tools", [], raising=False)


def test_allowlist_filters_to_named_tools():
    names = {t.name for t in operator_tools(_cfg(["calculator", "current_time"]))}
    assert names == {"calculator", "current_time"}


def test_empty_allowlist_exposes_nothing():
    assert operator_tools(_cfg([])) == []


def test_plugin_tools_ride_the_same_bridge(monkeypatch):
    @tool
    def my_plugin_tool(x: str) -> str:
        """A plugin-contributed tool."""
        return x

    monkeypatch.setattr(STATE, "plugin_tools", [my_plugin_tool], raising=False)
    names = {t.name for t in operator_tools(_cfg(["my_plugin_tool", "calculator"]))}
    assert names == {"my_plugin_tool", "calculator"}  # core + plugin, one allowlist


def test_build_server_exposes_allowlisted_as_mcp():
    server, exposed = build_server(_cfg(["calculator"]))
    assert exposed == ["calculator"]
    assert server is not None


def test_star_exposes_all_except_execute_code(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def execute_code(code: str) -> str:
        """run code"""
        return code

    @tool
    def plugin_thing(x: str) -> str:
        """a plugin tool"""
        return x

    monkeypatch.setattr(STATE, "plugin_tools", [execute_code, plugin_thing], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*"]))}
    assert "calculator" in names and "plugin_thing" in names   # core + plugin all flow
    assert "execute_code" not in names                          # excluded from the wildcard


def test_star_plus_explicit_name_still_includes_it(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def execute_code(code: str) -> str:
        """run code"""
        return code

    monkeypatch.setattr(STATE, "plugin_tools", [execute_code], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*", "execute_code"]))}
    assert "execute_code" in names   # naming it explicitly overrides the wildcard exclusion
