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


def test_manager_resolves_the_registry_allowlist(monkeypatch):
    """Without a runtime override or inherited lead tools, the detached fence is
    exactly the researcher's registry allowlist; an unknown type is unfenced.

    This runs in a shared process, and the value it asserts on —
    ``SUBAGENT_REGISTRY["researcher"].tools`` — is process-global state. A sibling
    test can leave the registry entry replaced (``_apply_config_subagents`` rebuilds
    it, a YAML ``subagents.researcher.tools`` override trims it). The autouse
    ``_isolate_subagent_registry`` fixture (tests/conftest.py) restores the registry
    dict per test, but this test does not lean on that: it PINS every input the
    resolution reads — no config overlay, no live graph (so no inherited lead
    tools), and the registry entry pinned back to the canonical ``RESEARCHER_CONFIG``
    — so no predecessor's leak can flip the result. The one divergence that could
    survive both guards would be a registry whose ``tools`` carries a duplicate or a
    HITL name: ``_subagent_fence`` dedups + HITL-filters, the raw list does not, so
    they'd differ by one entry (#4617). The assertion prints BOTH full lists with
    their lengths so such a mismatch names the lists, not pytest's truncated
    ``[current_tim…] == [current_tim…]``.
    """
    from background.manager import _subagent_fence
    from graph.subagents.config import RESEARCHER_CONFIG, SUBAGENT_REGISTRY
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", None)
    monkeypatch.setattr(STATE, "graph", None)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "researcher", RESEARCHER_CONFIG)

    fence = _subagent_fence("researcher")
    expected = list(SUBAGENT_REGISTRY["researcher"].tools)
    assert fence == expected, (
        "detached researcher fence diverged from its registry allowlist\n"
        f"  fence    ({len(fence)}): {fence}\n"
        f"  expected ({len(expected)}): {expected}"
    )
    assert _subagent_fence("not-a-registry-type") == []


def test_manager_resolves_the_config_allowlist(monkeypatch):
    from background.manager import _subagent_fence
    from graph.subagents.config import RESEARCHER_CONFIG, SUBAGENT_REGISTRY
    from runtime.state import STATE

    # Pin the registry entry too (same reasoning as the test above): the config
    # override is what should win here, and the restoration check at the end must
    # compare against the canonical tools, not a value a prior test left behind.
    monkeypatch.setitem(SUBAGENT_REGISTRY, "researcher", RESEARCHER_CONFIG)
    registry_tools = list(SUBAGENT_REGISTRY["researcher"].tools)
    monkeypatch.setattr(STATE, "graph", None)
    monkeypatch.setattr(
        STATE, "graph_config",
        SimpleNamespace(researcher=SimpleNamespace(tools=["current_time"])),
    )

    assert _subagent_fence("researcher") == ["current_time"]
    assert SUBAGENT_REGISTRY["researcher"].tools == registry_tools


# ── the model is only SHOWN the fence (schemas trimmed at wrap_model_call) ─────────


def _model_request(tool_names, fence):
    """A real langchain ModelRequest — ``override`` must return a new request."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool as make_tool

    def _mk(name):
        @make_tool(name)
        def _t(x: str = "") -> str:
            """A bound tool."""
            return x

        return _t

    state = {"messages": []}
    if fence is not None:
        state["subagent_fence"] = fence
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([])),
        messages=[],
        tools=[_mk(n) for n in tool_names],
        state=state,
    )


_LEAD_TOOLS = ["web_search", "fetch_url", "execute_code", "load_skill", "browser_open", "campaign_create"]


def _bound(request):
    return [t.name for t in request.tools]


def test_fenced_call_binds_only_the_fenced_tools():
    """The brandLaunch case: a detached social_researcher (6-tool allowlist) was shown
    every lead schema and kept calling execute_code / load_skill into the call-time block."""
    seen = []
    SubagentFenceMiddleware().wrap_model_call(
        _model_request(_LEAD_TOOLS, ["web_search", "fetch_url", "social_brand_kit"]), seen.append
    )
    assert _bound(seen[0]) == ["web_search", "fetch_url"]


@pytest.mark.asyncio
async def test_async_model_call_is_trimmed_too():
    seen = []

    async def handler(request):
        seen.append(request)

    await SubagentFenceMiddleware().awrap_model_call(_model_request(_LEAD_TOOLS, ["fetch_url"]), handler)
    assert _bound(seen[0]) == ["fetch_url"]


@pytest.mark.parametrize(
    "fence",
    [
        None,  # unfenced: an ordinary chat turn
        [],  # an empty fence means "no fence"
        ["<no tools>"],  # deny-all: keep schemas (a tool-call history needs `tools`); calls stay blocked
        ["not_a_bound_tool"],  # nothing to bind → don't send an empty tool list
    ],
)
def test_unfenced_deny_all_and_disjoint_fences_leave_the_schemas(fence):
    req = _model_request(_LEAD_TOOLS, fence)
    seen = []
    SubagentFenceMiddleware().wrap_model_call(req, seen.append)
    assert seen[0] is req
    assert _bound(seen[0]) == _LEAD_TOOLS


def test_provider_dict_tool_specs_are_trimmed_and_unnamed_entries_kept():
    from graph.middleware.subagent_fence import fence_tools

    unnamed = {"type": "web_search_20250305"}
    req = _model_request([], ["fetch_url"])
    req = req.override(
        tools=[
            {"type": "function", "function": {"name": "fetch_url"}},
            {"name": "execute_code", "input_schema": {}},
            unnamed,
        ]
    )
    out = fence_tools(req)
    assert out.tools == [{"type": "function", "function": {"name": "fetch_url"}}, unnamed]
