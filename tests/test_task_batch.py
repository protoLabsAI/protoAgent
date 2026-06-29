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


async def _batch(tool, tasks):
    """Invoke task_batch the way the ToolNode does — a full model ToolCall (it carries
    an InjectedToolCallId now, the parent id for nesting) — and return the string body."""
    out = await tool.ainvoke({"name": "task_batch", "args": {"tasks": tasks}, "id": "tb-test", "type": "tool_call"})
    return getattr(out, "content", out)


def test_build_returns_task_and_batch(monkeypatch):
    tools = _build(monkeypatch)
    assert set(tools) == {"task", "task_batch"}


@pytest.mark.asyncio
async def test_single_task_is_unbounded(monkeypatch):
    rec = []
    tools = _build(monkeypatch, recorder=rec)
    # `task` carries an InjectedToolCallId (the cancellable-delegation key, Tier 2),
    # so it must be invoked with a full model ToolCall — exactly how the graph's
    # ToolNode calls it in production. Result comes back as a ToolMessage.
    out = await tools["task"].ainvoke(
        {
            "name": "task",
            "args": {"description": "d", "prompt": "p", "subagent_type": "researcher"},
            "id": "tc-test",
            "type": "tool_call",
        }
    )
    assert out.content == "OUT:d"
    # single task must not truncate
    assert rec[0]["truncate"] is None


@pytest.mark.asyncio
async def test_batch_orders_results_by_index(monkeypatch):
    tools = _build(monkeypatch)
    out = await _batch(
        tools["task_batch"],
        [
            {"description": "alpha", "prompt": "p1"},
            {"description": "beta", "prompt": "p2"},
            {"description": "gamma", "prompt": "p3"},
        ],
    )
    # ordered 1..3 regardless of completion order
    assert out.index("Task 1/3") < out.index("Task 2/3") < out.index("Task 3/3")
    assert "OUT:alpha" in out and "OUT:beta" in out and "OUT:gamma" in out


@pytest.mark.asyncio
async def test_batch_passes_truncate_from_config(monkeypatch):
    rec = []
    cfg = LangGraphConfig(subagent_output_truncate=1234)
    tools = _build(monkeypatch, config=cfg, recorder=rec)
    await _batch(tools["task_batch"], [{"description": "d", "prompt": "p"}])
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
    await _batch(tools["task_batch"], [{"description": f"t{i}", "prompt": "p"} for i in range(6)])
    assert state["peak"] <= 2


@pytest.mark.asyncio
async def test_batch_empty_list(monkeypatch):
    tools = _build(monkeypatch)
    out = await _batch(tools["task_batch"], [])
    assert "empty task list" in out


@pytest.mark.asyncio
async def test_batch_missing_prompt_isolated(monkeypatch):
    tools = _build(monkeypatch)
    out = await _batch(
        tools["task_batch"],
        [
            {"description": "good", "prompt": "p"},
            {"description": "bad"},  # no prompt
        ],
    )
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
    out = await _batch(
        tools["task_batch"],
        [
            {"description": "ok", "prompt": "p"},
            {"description": "boom", "prompt": "p"},
        ],
    )
    assert "OUT:ok" in out
    assert "RuntimeError" in out and "kaboom" in out


# ── background fan-out: task_batch(run_in_background=True) (ADR 0050) ──────────


class _RecordingBG:
    """Stands in for BackgroundManager: records each spawn and hands back a job id."""

    def __init__(self):
        self.calls: list[dict] = []

    async def spawn(self, *, origin_session, subagent_type, description, prompt):
        self.calls.append(
            {
                "origin": origin_session,
                "subagent_type": subagent_type,
                "description": description,
                "prompt": prompt,
            }
        )
        return f"bg-{len(self.calls)}"


def test_task_batch_exposes_run_in_background():
    """The batch tool advertises the background switch in its JSON schema so the model
    can fan a whole batch out detached (parity with `task`'s run_in_background)."""
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [])}
    props = tools["task_batch"].args_schema.model_json_schema()["properties"]
    assert "run_in_background" in props


@pytest.mark.asyncio
async def test_batch_background_spawns_each_spec(monkeypatch):
    """run_in_background=True spawns one background job per spec (not a blocking
    foreground run) and returns the started job ids. _run_subagent must NOT be called."""
    called = {"foreground": 0}

    async def fake_run(**kwargs):
        called["foreground"] += 1
        return "should-not-run"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    rec = _RecordingBG()
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [], background_mgr=rec)}
    out = await tools["task_batch"].ainvoke(
        {
            "name": "task_batch",
            "args": {
                "tasks": [
                    {"description": "alpha", "prompt": "p1", "subagent_type": "researcher"},
                    {"description": "beta", "prompt": "p2"},  # subagent_type defaults
                ],
                "run_in_background": True,
            },
            "id": "tb-bg",
            "type": "tool_call",
        }
    )
    body = getattr(out, "content", out)
    assert called["foreground"] == 0, "background batch must not run subagents inline"
    assert len(rec.calls) == 2
    assert {c["description"] for c in rec.calls} == {"alpha", "beta"}
    assert rec.calls[1]["subagent_type"] == "researcher"  # default applied
    assert "bg-1" in body and "bg-2" in body
    assert "Started 2 background" in body


@pytest.mark.asyncio
async def test_batch_background_isolates_bad_specs(monkeypatch):
    """A bad spec (missing prompt / unknown subagent) is skipped inline; the good ones
    still spawn — the batch is not aborted."""
    rec = _RecordingBG()
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [], background_mgr=rec)}
    out = await tools["task_batch"].ainvoke(
        {
            "name": "task_batch",
            "args": {
                "tasks": [
                    {"description": "good", "prompt": "p"},
                    {"description": "noprompt"},  # missing prompt → skipped
                    {"description": "weird", "prompt": "p", "subagent_type": "does-not-exist"},
                ],
                "run_in_background": True,
            },
            "id": "tb-bg2",
            "type": "tool_call",
        }
    )
    body = getattr(out, "content", out)
    assert len(rec.calls) == 1 and rec.calls[0]["description"] == "good"
    assert "missing 'prompt'" in body
    assert "unknown subagent" in body
    assert "Started 1 background" in body


@pytest.mark.asyncio
async def test_batch_background_degrades_without_manager(monkeypatch):
    """With no background manager, run_in_background falls back to the FOREGROUND batch
    (runs the subagents) rather than silently dropping the work."""
    rec = []

    async def fake_run(**kwargs):
        rec.append(kwargs)
        return f"OUT:{kwargs['description']}"

    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [])}  # no background_mgr
    out = await tools["task_batch"].ainvoke(
        {
            "name": "task_batch",
            "args": {"tasks": [{"description": "a", "prompt": "p"}], "run_in_background": True},
            "id": "tb-bg3",
            "type": "tool_call",
        }
    )
    body = getattr(out, "content", out)
    assert len(rec) == 1 and "OUT:a" in body  # ran in the foreground
