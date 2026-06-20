"""Tests for programmatic tool calling (execute_code) — bd-pe2.6.

These run real child processes (the Docker CI image has python), exercising
the subprocess + fd-based tool-RPC bridge end to end with fake tools.
"""

import sys
from pathlib import Path

import pytest
from langchain_core.tools import tool

from graph.plugins.registry import PluginRegistry
from plugins.execute_code import register
from plugins.execute_code.engine import build_execute_code_tool, run_code


@tool
async def echo_tool(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


@tool
async def boom_tool() -> str:
    """Always raises."""
    raise ValueError("kaboom")


_TOOL_MAP = {"echo_tool": echo_tool, "boom_tool": boom_tool}


@pytest.mark.asyncio
async def test_plain_stdout_no_tools():
    out = await run_code("print('hello world')", {})
    assert out == "hello world"


@pytest.mark.asyncio
async def test_tool_bridge_roundtrip():
    out = await run_code("print(tools.echo_tool(text='abc'))", _TOOL_MAP)
    assert out == "ABC"


@pytest.mark.asyncio
async def test_tool_bridge_loop_collapses_chain():
    code = "vals = [tools.echo_tool(text=w) for w in ['a', 'b', 'c']]\nprint('-'.join(vals))"
    out = await run_code(code, _TOOL_MAP)
    assert out == "A-B-C"


@pytest.mark.asyncio
async def test_tool_error_propagates_to_script():
    # The tool raises; the proxy surfaces it as a RuntimeError the script can see.
    code = "try:\n    tools.boom_tool()\nexcept Exception as e:\n    print('caught:', e)"
    out = await run_code(code, _TOOL_MAP)
    assert "caught:" in out and "kaboom" in out


@pytest.mark.asyncio
async def test_unknown_tool_reported():
    code = "try:\n    tools.nope()\nexcept Exception as e:\n    print('err:', e)"
    out = await run_code(code, _TOOL_MAP)
    assert "not available" in out


@pytest.mark.asyncio
async def test_script_exception_reports_nonzero_exit():
    out = await run_code("raise ValueError('bad script')", {})
    assert "exited with code" in out
    assert "bad script" in out


@pytest.mark.asyncio
async def test_timeout_kills_process():
    out = await run_code("import time; time.sleep(5)", {}, timeout=0.5)
    assert "timed out" in out


@pytest.mark.asyncio
async def test_env_is_scrubbed(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    out = await run_code("import os; print(os.environ.get('SECRET_TOKEN', 'ABSENT'))", {})
    assert out == "ABSENT"


@pytest.mark.asyncio
async def test_output_truncation():
    out = await run_code("print('x' * 100)", {}, truncate=20)
    assert out.startswith("x" * 20)
    assert "truncated to 20 chars" in out


# --- tool-build wiring ------------------------------------------------------


def test_build_excludes_self_and_respects_allowlist():
    # include a decoy + a self-named tool to prove filtering
    ec = build_execute_code_tool([echo_tool, boom_tool], tools=["echo_tool"])
    assert ec.name == "execute_code"
    # allowlist limited to echo_tool; the docstring lists available tools
    assert "echo_tool" in ec.description
    assert "boom_tool" not in ec.description


@pytest.mark.asyncio
async def test_built_tool_runs():
    ec = build_execute_code_tool([echo_tool])
    out = await ec.ainvoke({"code": "print(tools.echo_tool(text='hi'))"})
    assert out == "HI"


@pytest.mark.asyncio
async def test_built_tool_rejects_empty():
    ec = build_execute_code_tool([echo_tool])
    out = await ec.ainvoke({"code": "  "})
    assert "empty code" in out


# --- plugin wiring ----------------------------------------------------------


def test_plugin_register_wires_a_late_factory_that_builds_the_tool():
    reg = PluginRegistry(
        "execute_code", Path("."), config={"timeout": 5, "output_truncate": 100, "tools": ["echo_tool"]}
    )
    register(reg)
    assert len(reg.late_tool_factories) == 1
    ec = reg.late_tool_factories[0]([echo_tool, boom_tool], None)
    assert ec.name == "execute_code"
    # allowlist from the plugin's config section is applied
    assert "echo_tool" in ec.description and "boom_tool" not in ec.description


def test_plugin_not_loaded_in_frozen_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    reg = PluginRegistry("execute_code", Path("."), config={})
    register(reg)
    assert reg.late_tool_factories == []  # no standalone Python in the packaged desktop build
