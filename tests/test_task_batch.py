"""Tests for concurrent sub-agent delegation (`task_batch`) — bd-pe2.3.

These exercise the orchestration layer (`_build_task_tools` / `task_batch`)
without invoking a real LLM: `graph.agent._run_subagent` is monkeypatched with
a fake so we can assert ordering, concurrency capping, truncation wiring, and
per-task error isolation deterministically.
"""

import asyncio

import pytest

import graph.agent as agent_mod
from graph.config import LangGraphConfig


@pytest.fixture(autouse=True)
def _gateway_creds(monkeypatch):
    # create_llm() builds a ChatOpenAI at tool-build time; it needs a key.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _build(monkeypatch, config=None, recorder=None):
    """Build [task, task_batch] with _run_subagent replaced by a fake."""
    cfg = config or LangGraphConfig()

    async def fake_run(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return f"OUT:{kwargs['description']}"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    tools = agent_mod._build_task_tools(cfg, [])
    return {t.name: t for t in tools}


def test_build_returns_task_and_batch(monkeypatch):
    tools = _build(monkeypatch)
    assert set(tools) == {"task", "task_batch"}


@pytest.mark.asyncio
async def test_single_task_is_unbounded(monkeypatch):
    rec = []
    tools = _build(monkeypatch, recorder=rec)
    out = await tools["task"].ainvoke(
        {"description": "d", "prompt": "p", "subagent_type": "researcher"}
    )
    assert out == "OUT:d"
    # single task must not truncate
    assert rec[0]["truncate"] is None


@pytest.mark.asyncio
async def test_batch_orders_results_by_index(monkeypatch):
    tools = _build(monkeypatch)
    out = await tools["task_batch"].ainvoke({"tasks": [
        {"description": "alpha", "prompt": "p1"},
        {"description": "beta", "prompt": "p2"},
        {"description": "gamma", "prompt": "p3"},
    ]})
    # ordered 1..3 regardless of completion order
    assert out.index("Task 1/3") < out.index("Task 2/3") < out.index("Task 3/3")
    assert "OUT:alpha" in out and "OUT:beta" in out and "OUT:gamma" in out


@pytest.mark.asyncio
async def test_batch_passes_truncate_from_config(monkeypatch):
    rec = []
    cfg = LangGraphConfig(subagent_output_truncate=1234)
    tools = _build(monkeypatch, config=cfg, recorder=rec)
    await tools["task_batch"].ainvoke({"tasks": [{"description": "d", "prompt": "p"}]})
    assert rec[0]["truncate"] == 1234


@pytest.mark.asyncio
async def test_batch_respects_concurrency_cap(monkeypatch):
    cfg = LangGraphConfig(subagent_max_concurrency=2)
    state = {"in_flight": 0, "peak": 0}

    async def fake_run(**kwargs):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.02)
        state["in_flight"] -= 1
        return f"OUT:{kwargs['description']}"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    tools = {t.name: t for t in agent_mod._build_task_tools(cfg, [])}
    await tools["task_batch"].ainvoke({"tasks": [
        {"description": f"t{i}", "prompt": "p"} for i in range(6)
    ]})
    assert state["peak"] <= 2


@pytest.mark.asyncio
async def test_batch_empty_list(monkeypatch):
    tools = _build(monkeypatch)
    out = await tools["task_batch"].ainvoke({"tasks": []})
    assert "empty task list" in out


@pytest.mark.asyncio
async def test_batch_missing_prompt_isolated(monkeypatch):
    tools = _build(monkeypatch)
    out = await tools["task_batch"].ainvoke({"tasks": [
        {"description": "good", "prompt": "p"},
        {"description": "bad"},  # no prompt
    ]})
    assert "OUT:good" in out
    assert "missing 'prompt'" in out


@pytest.mark.asyncio
async def test_batch_failure_isolated(monkeypatch):
    async def fake_run(**kwargs):
        if kwargs["description"] == "boom":
            raise RuntimeError("kaboom")
        return f"OUT:{kwargs['description']}"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [])}
    out = await tools["task_batch"].ainvoke({"tasks": [
        {"description": "ok", "prompt": "p"},
        {"description": "boom", "prompt": "p"},
    ]})
    assert "OUT:ok" in out
    assert "RuntimeError" in out and "kaboom" in out
