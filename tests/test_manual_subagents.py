from __future__ import annotations

from types import SimpleNamespace

import pytest

import graph.agent as agent_mod
from graph.config import LangGraphConfig


@pytest.mark.asyncio
async def test_run_manual_subagent_reuses_private_runner(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(agent_mod, "create_llm", lambda _config: object())
    monkeypatch.setattr(
        agent_mod,
        "get_all_tools",
        lambda _store=None, scheduler=None, **_kw: [SimpleNamespace(name="current_time")],
    )

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "manual result"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)

    out = await agent_mod.run_manual_subagent(
        LangGraphConfig(),
        knowledge_store=object(),
        scheduler=object(),
        description="Check docs",
        prompt="Read the docs",
        subagent_type="researcher",
        emit_skill=True,
        truncate=12,
    )

    assert out == "manual result"
    assert calls[0]["tool_map"].keys() == {"current_time"}
    assert calls[0]["description"] == "Check docs"
    assert calls[0]["prompt"] == "Read the docs"
    assert calls[0]["subagent_type"] == "researcher"
    assert calls[0]["emit_skill"] is True
    assert calls[0]["truncate"] == 12


@pytest.mark.asyncio
async def test_run_manual_subagent_merges_extra_tools(monkeypatch) -> None:
    """Plugin/MCP tools passed as ``extra_tools`` must reach the subagent's
    tool_map — without this the out-of-graph runner silently degrades a
    plugin-tool allowlist to "not a valid tool" (the lead graph exposes them via
    its own ``extra_tools``; this path has to mirror that surface)."""
    calls = []

    monkeypatch.setattr(agent_mod, "create_llm", lambda _config: object())
    monkeypatch.setattr(
        agent_mod,
        "get_all_tools",
        lambda _store=None, scheduler=None, **_kw: [SimpleNamespace(name="current_time")],
    )

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)

    out = await agent_mod.run_manual_subagent(
        LangGraphConfig(),
        knowledge_store=object(),
        scheduler=object(),
        description="Backtest it",
        prompt="Test the idea",
        subagent_type="researcher",
        extra_tools=[SimpleNamespace(name="backtest_strategy")],
    )

    assert out == "ok"
    # The core set AND the plugin tool are both visible to the subagent.
    assert calls[0]["tool_map"].keys() == {"current_time", "backtest_strategy"}


@pytest.mark.asyncio
async def test_run_manual_subagent_batch_orders_and_normalizes_type(monkeypatch) -> None:
    calls = []

    async def fake_manual(config, knowledge_store=None, scheduler=None, **kwargs):
        calls.append(kwargs)
        return f"OUT:{kwargs['description']}"

    monkeypatch.setattr(agent_mod, "run_manual_subagent", fake_manual)

    out = await agent_mod.run_manual_subagent_batch(
        LangGraphConfig(subagent_output_truncate=99),
        tasks=[
            {"description": "one", "prompt": "p1", "type": "researcher"},
            {"description": "two", "prompt": "p2", "subagent_type": "researcher"},
        ],
    )

    assert out.index("Task 1/2") < out.index("Task 2/2")
    assert "OUT:one" in out
    assert "OUT:two" in out
    assert [call["subagent_type"] for call in calls] == ["researcher", "researcher"]
    assert [call["truncate"] for call in calls] == [99, 99]


@pytest.mark.asyncio
async def test_run_manual_subagent_batch_rejects_empty_tasks() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await agent_mod.run_manual_subagent_batch(LangGraphConfig(), tasks=[])
