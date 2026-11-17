"""Operator MCP selectors and on-demand metadata over both child protocols."""

import json
from pathlib import Path

import pytest
from langchain_core.tools import tool

from graph.plugins.registry import PluginRegistry
from plugins.execute_code import kernel, register
from plugins.execute_code.engine import build_execute_code_tool, run_code

# The engine and kernel modules branch on ``os.name``; the import pulls their
# Windows-only paths into this file, so the whole module is platform-sensitive.
pytestmark = pytest.mark.platform_sensitive


def _tools():
    def echo(value: str = "default") -> str:
        """Return the value.

        Full description with details beyond the signature summary.
        """
        return value

    return [tool(name)(echo) for name in ("srv__a", "srv__b", "other__c", "read_file", "task")]


def _factory(config):
    registry = PluginRegistry("execute_code", Path("."), config=config)
    register(registry)
    return registry.late_tool_factories[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("config, expected", [
    ({"extra_tools": ["mcp:srv/*"]}, ["read_file", "srv__a", "srv__b"]),
    ({"tools": ["srv__a"]}, ["srv__a"]),
    ({"tools": ["mcp:srv/*"]}, ["srv__a", "srv__b"]),
    ({"extra_tools": ["mcp:missing/*"]}, ["read_file"]),
    ({"tools": ["mcp:missing/*"]}, []),
    ({}, ["read_file"]),
    ({"tools": ["task", "mcp:srv/*"]}, ["srv__a", "srv__b"]),
])
async def test_operator_selectors(config, expected):
    ec = _factory(config)(_tools(), None)
    assert json.loads(await ec.ainvoke({"code": "result = tools.help()"})) == expected
    if "srv__a" in expected:
        assert await ec.ainvoke({"code": "print(tools.srv__a(value='filtered'))"}) == "filtered"


def test_unknown_server_warned_once(caplog, monkeypatch):
    from graph import tool_renames

    monkeypatch.setattr(tool_renames, "_WARNED", set())
    factory = _factory({"extra_tools": ["mcp:missing/*"]})
    factory(_tools(), None)
    factory(_tools(), None)
    assert sum("matches no registered tools" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["tools_disabled", "tools_subagent_only"])
async def test_glob_cannot_reintroduce_tools_excluded_by_lead_seam(restriction):
    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from tools.lg_tools import set_disabled_tools

    try:
        graph = create_agent_graph(
            LangGraphConfig(**{restriction: ["srv__b"]}),
            extra_tools=_tools()[:3],
            late_tool_factories=[_factory({"tools": ["mcp:srv/*"]})],
        )
        ec = next(t for t in graph.bound_tools if t.name == "execute_code")
        out = await ec.ainvoke({"code": """
assert tools.help() == ["srv__a"]
assert "not bridged" in tools.help("srv__b")
try:
    tools.srv__b()
except RuntimeError:
    print("blocked")
else:
    raise AssertionError("excluded tool ran")
"""})
        assert out == "blocked"
    finally:
        set_disabled_tools([])


@pytest.mark.asyncio
@pytest.mark.parametrize("session_state", [False, True])
async def test_help_is_metadata_only_in_both_runners(session_state, tmp_path, monkeypatch):
    from observability.audit import AuditLogger

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(audit))
    calls = []

    @tool
    async def srv__a(value: str = "default") -> str:
        """Short summary.

        Full details that are not included in the signature.
        """
        calls.append(value)
        return value

    def gate(name):
        raise AssertionError("help must not reach enforcement")

    async def run(code, tool_map):
        kwargs = dict(session_id="mcp-help", gate=gate, fence=frozenset(), timeout=30, truncate=6000)
        if session_state:
            return await kernel.run_in_kernel(code, tool_map, **kwargs)
        return await run_code(code, tool_map, **kwargs)

    try:
        out = await run("""
import json
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor() as pool:
    descriptions = list(pool.map(tools.help, ["srv__a", "nope"]))
print(json.dumps([tools.help(), descriptions]))
""", {"srv__a": srv__a})
        names, descriptions = json.loads(out.splitlines()[-1])
        assert names == ["srv__a"]
        assert "tools.srv__a(value='default')" in descriptions[0]
        assert srv__a.description in descriptions[0]
        assert "not bridged" in descriptions[1]
        # A reused kernel must consult this run's map, not retain old metadata.
        out = await run('print(tools.help("srv__a"))', {})
        assert "not bridged" in out
        assert calls == []
        rows = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        assert not any(row.get("tool", "").startswith("ptc:") for row in rows)
    finally:
        await kernel.stop_session("mcp-help")


def test_wide_bridge_description_points_to_help():
    def echo(value: str) -> str:
        """Echo value."""
        return value

    tools = [tool(f"srv__t{i}")(echo) for i in range(26)]
    ec = build_execute_code_tool(tools, tools=[t.name for t in tools])
    assert "tools.help(name)" in ec.description
