"""SubagentFenceMiddleware (#1639) — per-turn tool fence for detached subagent runs.

A detached background job runs the full lead graph; the fence rides the turn's state
(stamped from the fire metadata) and blocks any tool call outside the subagent's
allowlist with the enforcement-style ToolMessage block. No fence on the state → no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from graph.middleware.subagent_fence import SubagentFenceMiddleware


def _request(tool_name: str, fence: list[str] | None):
    state = {"messages": []}
    if fence is not None:
        state["subagent_fence"] = fence
    return SimpleNamespace(tool_call={"name": tool_name, "args": {}, "id": "c1"}, state=state)


def _handler(request):
    return ToolMessage(content="ran", tool_call_id="c1")


def test_no_fence_is_a_noop():
    mw = SubagentFenceMiddleware()
    out = mw.wrap_tool_call(_request("st_purchase", None), _handler)
    assert out.content == "ran"


def test_allowlisted_tool_passes():
    mw = SubagentFenceMiddleware()
    out = mw.wrap_tool_call(_request("web_search", ["web_search", "fetch_url"]), _handler)
    assert out.content == "ran"


def test_foreign_tool_is_blocked_with_a_readable_toolmessage():
    """The explorer-buys-a-ship case: allowlist chart/scan/travel, model calls a
    purchase tool — blocked before execution, with the allowlist in the denial so
    the model can adapt."""
    mw = SubagentFenceMiddleware()
    called = []

    def handler(request):
        called.append(request)
        return ToolMessage(content="ran", tool_call_id="c1")

    out = mw.wrap_tool_call(_request("st_purchase", ["st_chart", "st_scan"]), handler)
    assert called == []  # never executed
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "st_purchase" in out.content and "st_chart" in out.content


@pytest.mark.asyncio
async def test_async_path_blocks_too():
    mw = SubagentFenceMiddleware()

    async def handler(request):
        return ToolMessage(content="ran", tool_call_id="c1")

    out = await mw.awrap_tool_call(_request("run_command", ["web_search"]), handler)
    assert out.status == "error"
    ok = await mw.awrap_tool_call(_request("web_search", ["web_search"]), handler)
    assert ok.content == "ran"


def test_manager_resolves_the_registry_allowlist():
    """_subagent_fence mirrors the in-graph task path's resolution: registry tools
    (plus any config override — covered by the getattr fallback), [] for unknowns."""
    from background.manager import _subagent_fence
    from graph.subagents.config import SUBAGENT_REGISTRY

    assert _subagent_fence("researcher") == list(SUBAGENT_REGISTRY["researcher"].tools)
    assert _subagent_fence("not-a-registry-type") == []
