"""roxy #58 — an MCP tool error must degrade into a recoverable tool result,
not fail the whole A2A turn.

`langchain-mcp-adapters` raises `ToolException` when an MCP server returns an
error (e.g. a board `404 Feature not found` from a stale id). `build_mcp_tools`
sets `_mcp_tool_error_handler` as each MCP tool's `handle_tool_error`, so the
exception is caught *inside* the tool (BaseTool.arun) and returned to the model —
never propagating out of the ToolNode to fail the turn.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool, ToolException

from tools.mcp_tools import _mcp_tool_error_handler


def _raising_tool() -> StructuredTool:
    """A stand-in for an MCP tool that errors the way the adapter does."""

    async def _boom(**kwargs) -> str:
        raise ToolException('API error 404: {"success":false,"error":"Feature not found"}')

    return StructuredTool.from_function(
        coroutine=_boom, name="automaker__get_feature", description="stub"
    )


def test_handler_returns_recoverable_message():
    msg = _mcp_tool_error_handler(ToolException("API error 404: Feature not found"))
    assert "Tool error:" in msg
    assert "Feature not found" in msg
    assert "fatal" in msg.lower()  # explicitly tells the model not to give up


@pytest.mark.asyncio
async def test_tool_error_degrades_instead_of_raising():
    tool = _raising_tool()
    # Without the handler, ainvoke would raise ToolException (the turn-killing path).
    with pytest.raises(ToolException):
        await tool.ainvoke({})

    # With the handler set (as build_mcp_tools does), the error becomes a result.
    tool.handle_tool_error = _mcp_tool_error_handler
    out = await tool.ainvoke({})
    assert isinstance(out, str)
    assert "Tool error:" in out and "Feature not found" in out


def test_build_mcp_tools_wires_the_handler(monkeypatch):
    """The kept MCP tools come back with handle_tool_error set."""
    import tools.mcp_tools as mt

    kept = _raising_tool()

    class _FakeClient:
        def get_tools(self):  # awaited via _run_blocking
            async def _coro():
                return [kept]
            return _coro()

    monkeypatch.setattr(mt, "MultiServerMCPClient", lambda *a, **k: _FakeClient(), raising=False)
    # patch the imported symbol path used inside build_mcp_tools
    import langchain_mcp_adapters.client as mcp_client
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", lambda *a, **k: _FakeClient())

    class _Cfg:
        mcp_enabled = True
        mcp_timeout_seconds = 5.0
        mcp_denylist = []
        mcp_servers = [{"name": "automaker", "command": "x", "args": []}]

    _clients, tools_out, _meta = mt.build_mcp_tools(_Cfg())
    assert tools_out, "expected the stub MCP tool to be kept"
    assert tools_out[0].handle_tool_error is _mcp_tool_error_handler
