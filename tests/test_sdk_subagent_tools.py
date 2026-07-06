"""sdk.run_subagent must expose the host's plugin + MCP tools by default.

The workflows engine drives every recipe step through sdk.run_subagent with no
extra_tools argument. Roles whose allowlists name PLUGIN tools (review-finder's
github_pr_diff, ADR 0077) resolved to "No tools available" because the SDK
didn't forward STATE.plugin_tools — caught live by the code-review acceptance
run. These pin the default + the explicit-override escape hatch.
"""

from __future__ import annotations

import pytest

from graph import sdk
from runtime.state import STATE


@pytest.fixture()
def capture(monkeypatch):
    """Stub run_manual_subagent and record the kwargs the SDK forwards."""
    calls = {}

    async def fake_run(config, knowledge_store=None, scheduler=None, **kw):
        calls.update(kw)
        return "ok"

    import graph.agent as agent_mod

    monkeypatch.setattr(agent_mod, "run_manual_subagent", fake_run)
    monkeypatch.setattr(STATE, "graph_config", object(), raising=False)
    return calls


async def test_defaults_to_plugin_and_mcp_tools(monkeypatch, capture):
    plugin_tool, mcp_tool = object(), object()
    monkeypatch.setattr(STATE, "plugin_tools", [plugin_tool], raising=False)
    monkeypatch.setattr(STATE, "mcp_tools", [mcp_tool], raising=False)
    await sdk.run_subagent("researcher", "p", description="d")
    assert capture["extra_tools"] == [plugin_tool, mcp_tool]


async def test_explicit_empty_list_disables_the_default(monkeypatch, capture):
    monkeypatch.setattr(STATE, "plugin_tools", [object()], raising=False)
    await sdk.run_subagent("researcher", "p", description="d", extra_tools=[])
    assert capture["extra_tools"] == []


async def test_tolerates_missing_state_fields(monkeypatch, capture):
    monkeypatch.setattr(STATE, "plugin_tools", None, raising=False)
    monkeypatch.setattr(STATE, "mcp_tools", None, raising=False)
    await sdk.run_subagent("researcher", "p", description="d")
    assert capture["extra_tools"] == []
