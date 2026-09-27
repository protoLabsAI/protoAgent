"""A workflow step's subagent trace carries the keys that join it to its run (#3565).

Before: a ``subagent:<type>`` span held only ``description`` + an empty
``parent_task_id`` — no run id, no step, no inputs, no session — so the only join to
``.runs/<run_id>.json`` was a timestamp, which breaks as soon as two panels overlap.

Driven end to end through the plugin: ``wf._execute`` → the engine → the REAL
``graph.agent._run_subagent`` (only the model graph is faked), with the Langfuse client
and ``propagate_attributes`` faked at the SDK boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from observability import tracing
from tests.test_workflows import _GatedReg, _patch_sdk

SECRET = "sk-" + "Q" * 40


class _FakeGraph:
    async def astream(self, _inputs, config=None, stream_mode=None):
        yield {"messages": [AIMessage(content="lane done.")]}


@pytest.fixture
def boundary(monkeypatch):
    """Real tracing module; fake Langfuse client + ``propagate_attributes``. Records every
    observation opened (name + metadata), every span update, and every propagation."""
    opened: list[dict] = []
    propagated: list[dict] = []
    span = MagicMock()
    span.trace_id = "f" * 32

    def start(**kw):
        opened.append(kw)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    def propagate(**kw):
        propagated.append(kw)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    fake = MagicMock()
    fake.start_as_current_observation.side_effect = start
    fake.get_current_trace_id.return_value = None
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    import langfuse

    monkeypatch.setattr(langfuse, "propagate_attributes", propagate)
    return opened, propagated, span


@pytest.fixture
def real_subagent(monkeypatch):
    """``sdk.run_subagent`` → the real ``_run_subagent`` (the code that opens the span)."""
    cfg = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"], max_turns=40)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", cfg)
    monkeypatch.setattr(agent_mod, "_subagent_tools", lambda *_a, **_k: [object()])
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "build_subagent_prompt", lambda *_a, **_k: "sys")
    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: _FakeGraph())

    async def run_subagent(subagent_type, prompt, description=""):
        return await agent_mod._run_subagent(
            config=LangGraphConfig(),
            tool_map={},
            available_subagents="stub",
            description=description,
            prompt=prompt,
            subagent_type="stub",
        )

    _patch_sdk(monkeypatch, run_subagent)


def _subagent_meta(opened: list[dict]) -> dict:
    spans = [o for o in opened if o["name"] == "subagent:stub"]
    assert len(spans) == 1, [o["name"] for o in opened]
    return spans[0]["metadata"]


async def test_a_studio_run_stamps_run_step_and_inputs_on_the_subagent_span(tmp_path, boundary, real_subagent):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, propagated, _span = boundary
    store = WorkflowRunStore(tmp_path)
    inputs = {"topic": "ai", "pr": 42, "note": f"token {SECRET}", "api_key": "plain-value", "files": ["a", "b"]}

    result = await wf._execute(_GatedReg(), "gated", inputs, run_store=store)
    run_id = result["run_id"]

    meta = _subagent_meta(opened)
    # The pre-existing keys survive; the join keys are added.
    assert meta["description"] == "workflow gated:gather"
    assert meta["run_id"] == run_id
    assert meta["workflow"] == "gated"
    assert meta["step_id"] == "gather"
    assert meta["input_topic"] == "ai" and meta["input_pr"] == 42
    # Redacted (by value AND by sensitive key), and structured inputs are not join keys.
    assert SECRET not in str(meta) and "plain-value" not in str(meta)
    assert "input_files" not in meta
    # No turn around a Studio run: its own root trace, session = run_id.
    assert opened[0]["name"] == "workflow:gated" and opened[0]["metadata"]["run_id"] == run_id
    assert "parent_session_id" not in meta
    sessions = [p["session_id"] for p in propagated if p.get("session_id")]
    assert sessions == [run_id]
    # Trace-level: tags + short scalar dims, so the trace is filterable by them.
    tags = [t for p in propagated for t in p.get("tags") or []]
    assert {"workflow:gated", f"run:{run_id}", "step:gather"} <= set(tags)
    dims = {k: v for p in propagated for k, v in (p.get("metadata") or {}).items()}
    assert dims["run_id"] == run_id and dims["workflow"] == "gated" and dims["input_pr"] == "42"
    # A trace holds the whole run: a per-step key there would be last-write-wins.
    assert "step_id" not in dims
    assert SECRET not in str(propagated)


async def test_a_run_from_a_chat_turn_groups_under_the_operators_session(tmp_path, boundary, real_subagent):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, propagated, _span = boundary

    async with tracing.trace_session("chat-77", name="chat", input="run gated"):
        result = await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=WorkflowRunStore(tmp_path))

    meta = _subagent_meta(opened)
    assert meta["parent_session_id"] == "chat-77"
    assert meta["run_id"] == result["run_id"] and meta["step_id"] == "gather"
    # A child of the turn's trace (one root), in the turn's session — never re-sessioned.
    assert [o["name"] for o in opened] == ["chat", "workflow:gated", "subagent:stub"]
    assert [p["session_id"] for p in propagated if p.get("session_id")] == ["chat-77"]


async def test_an_incognito_turn_sends_ids_but_no_inputs(tmp_path, boundary, real_subagent):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, propagated, span = boundary

    async with tracing.trace_session("chat-9", name="chat", input="secret ask", incognito=True):
        result = await wf._execute(
            _GatedReg(), "gated", {"topic": "private-topic"}, run_store=WorkflowRunStore(tmp_path)
        )

    meta = _subagent_meta(opened)
    assert meta["run_id"] == result["run_id"] and meta["step_id"] == "gather"
    assert not [k for o in opened for k in o["metadata"] if k.startswith("input_")]
    assert "private-topic" not in str(opened) + str(propagated)
    sent = [c.kwargs for c in span.update.call_args_list]
    assert not [c for c in sent if "input" in c or "output" in c], sent


async def test_a_resumed_segment_joins_by_the_same_run_id(tmp_path, boundary, real_subagent):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, _propagated, _span = boundary
    store = WorkflowRunStore(tmp_path)
    first = await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store)
    opened.clear()

    await wf._resume(_GatedReg(), first["run_id"], "approve", run_store=store)

    meta = _subagent_meta(opened)
    assert meta["run_id"] == first["run_id"] and meta["step_id"] == "analyze"
    assert meta["input_topic"] == "ai" and meta["resumed_step"] == "analyze"


async def test_step_keys_reach_generations_and_tool_calls_under_the_step(boundary):
    fake = tracing._langfuse
    with tracing.trace_attributes({"step_id": "a"}, trace_level=False):
        tracing.trace_generation("subagent-turn", model="m")
        tracing.trace_tool_call("read_file", {}, "ok", 1, True)
    metas = [c.kwargs["metadata"] for c in fake.start_observation.call_args_list]
    assert [m["step_id"] for m in metas] == ["a", "a"]


async def test_step_attributes_do_not_leak_past_the_step(boundary):
    with tracing.trace_attributes({"step_id": "a"}):
        pass
    with tracing.trace_span("subagent:x"):
        pass
    opened, _p, _s = boundary
    assert "step_id" not in opened[-1]["metadata"]


async def test_trace_attributes_with_tracing_off_still_runs_the_block(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_langfuse", None)
    ran = []
    with tracing.trace_attributes({"run_id": "r"}, tags=["t"]):
        ran.append(1)
    assert ran == [1]
