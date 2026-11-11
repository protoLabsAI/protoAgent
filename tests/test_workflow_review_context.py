"""Construction provenance through the real SDK/runner and durable workflow state."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import graph.agent as agent
import plugins.workflows as wf
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from plugins.workflows.run_state import WorkflowRunStore

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def runtime(monkeypatch):
    @tool
    def read_fixture() -> str:
        """Read fixture data."""
        return "fixture"

    config = LangGraphConfig(filesystem_enabled=False)
    monkeypatch.setattr(wf.sdk.STATE, "graph_config", config)
    monkeypatch.setattr(wf.sdk.STATE, "plugin_tools", [read_fixture])
    monkeypatch.setattr(wf.sdk.STATE, "mcp_tools", [])
    monkeypatch.setattr(wf.sdk.STATE, "plugin_late_tool_factories", [])
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "create_llm", lambda *a, **kw: object())
    monkeypatch.setitem(SUBAGENT_REGISTRY, "context-reader", SubagentConfig(
        name="context-reader", description="Read", system_prompt="Check the supplied evidence.",
        tools=["read_fixture"], max_turns=3,
    ))
    constructed = []

    class Graph:
        async def astream(self, inputs, **kwargs):
            # Interleave parallel runs; each must retain its own construction record.
            await asyncio.sleep(0)
            assert len(inputs["messages"]) == 1
            yield {"messages": [AIMessage(content="findings: " + inputs["messages"][0]["content"])]}

    def create(**kwargs):
        constructed.append(kwargs)
        return Graph()

    monkeypatch.setattr(agent, "create_agent", create)
    return constructed


def registry(*steps):
    spec = {"name": "context", "steps": [
        {"id": sid, "subagent": "context-reader", "prompt": sid, **opts} for sid, opts in steps
    ]}
    return SimpleNamespace(get=lambda name: spec)


async def test_real_construction_concurrent_results_and_readback(runtime, tmp_path):
    reg = registry(("a", {}), ("b", {"depends_on": ["a"], "prompt": "Prior: {{ steps.a.output }}"}))
    stores = [WorkflowRunStore(tmp_path) for _ in range(2)]
    results = await asyncio.gather(*(wf._execute(reg, "context", {}, run_store=s) for s in stores))
    identities = set()
    for store, result in zip(stores, results):
        context = result["review_context"]
        assert store.load(result["run_id"])["review_context"] == context
        for sid, step in context["steps"].items():
            assert step["schema_version"] == 1
            assert step["run_id"] == result["run_id"] and step["step_id"] == sid
            assert step["execution_id"] == result["tool_receipts"]["execution_id"]
            assert step["status"] == "observed"
            assert step["conversation"] == "fresh" and step["workflow_state"] == "fresh"
            assert step["parent_history"] == "not_supplied"
            assert step["instruction_sources"]["system_prompt"]["text"] == runtime[0]["system_prompt"]
            assert step["instruction_sources"]["external_sources"] == "unknown"
            assert step["tool_restrictions"]["bound_tools"] == ["read_fixture"]
            assert step["tool_restrictions"]["external_policy"] == "unknown"
            identities.add((step["run_id"], sid, step["execution_id"]))
        assert context["steps"]["b"]["inputs"]["prompt"]["text"] == "Prior: findings: a"
        context["steps"]["a"]["inputs"]["prompt"]["text"] = "mutated"
        assert store.load(result["run_id"])["review_context"]["steps"]["a"]["inputs"]["prompt"]["text"] == "a"
    assert len(identities) == 4


async def test_resume_seed_origin_and_fresh_conversation(runtime, tmp_path):
    reg = registry(("a", {}), ("b", {"depends_on": ["a"], "gate": "human"}))
    store = WorkflowRunStore(tmp_path)
    first = await wf._execute(reg, "context", {}, run_store=store)
    original = first["review_context"]["steps"]["a"]
    assert first["review_context"]["steps"]["b"]["status"] == "not_executed"
    second = await wf._resume(reg, first["run_id"], "approve", run_store=store)
    context = second["review_context"]
    assert context["steps"]["a"]["status"] == "not_executed"
    assert context["steps"]["a"]["workflow_state"] == "seeded"
    assert context["steps"]["a"]["origin"] == {k: original[k] for k in ("run_id", "step_id", "execution_id")}
    assert context["previous_executions"][0]["steps"]["a"] == original
    assert context["steps"]["b"]["workflow_state"] == "resumed"
    assert context["steps"]["b"]["conversation"] == "fresh"
    assert context["steps"]["b"]["execution_id"] != original["execution_id"]
    assert store.load(first["run_id"])["review_context"] == context


async def test_unobserved_and_external_seed_are_unknown(runtime, monkeypatch, tmp_path):
    async def unsupported(subagent_type, prompt, *, description, check_prompt_tools, session_id):
        return "I used a fresh conversation and read everything"

    monkeypatch.setattr(wf.sdk, "run_subagent", unsupported)
    store = WorkflowRunStore(tmp_path)
    result = await wf._execute(registry(("a", {}), ("b", {})), "context", {}, run_store=store,
                               seed_outputs={"a": "external output without provenance"})
    steps = result["review_context"]["steps"]
    assert steps["a"]["status"] == "not_executed" and steps["a"]["origin"] == "unknown"
    assert steps["b"]["status"] == "unknown" and steps["b"]["conversation"] == "unknown"
    assert result["output"] == "I used a fresh conversation and read everything"


async def test_construction_survives_cancellation(runtime, monkeypatch, tmp_path):
    entered = asyncio.Event()
    store = WorkflowRunStore(tmp_path)

    class Hanging:
        async def astream(self, inputs, **kwargs):
            assert store.load(store.run_id)["review_context"]["steps"]["a"]["status"] == "observed"
            entered.set()
            await asyncio.Event().wait()
            yield {}

    monkeypatch.setattr(agent, "create_agent", lambda **kw: Hanging())
    task = asyncio.create_task(wf._execute(registry(("a", {})), "context", {}, run_store=store))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.load(store.run_id)["review_context"]["steps"]["a"]["conversation"] == "fresh"


@pytest.mark.parametrize("source_kind", ["matching", "mismatch", "missing", "corrupt", "invalid"])
async def test_external_seed_explicit_origin(runtime, tmp_path, source_kind):
    reg = registry(("a", {}), ("b", {}))
    first = await wf._execute(reg, "context", {}, run_store=WorkflowRunStore(tmp_path))
    source_id = first["run_id"] if source_kind != "missing" else "missing"
    if source_kind == "corrupt":
        WorkflowRunStore(tmp_path).path(source_id).write_text("{", encoding="utf-8")
    elif source_kind == "invalid":
        source_id = "../../outside-store"
    output = first["steps"]["a"] if source_kind != "mismatch" else "changed output"
    second = await wf._execute(reg, "context", {}, run_store=WorkflowRunStore(tmp_path),
                               seed_outputs={"a": output}, seed_run_id=source_id)
    seed = second["review_context"]["steps"]["a"]
    assert seed["status"] == "not_executed"
    assert seed["run_id"] != first["run_id"]
    assert seed["origin"] == ({k: first["review_context"]["steps"]["a"][k]
                               for k in ("run_id", "step_id", "execution_id")}
                              if source_kind == "matching" else "unknown")


async def test_effective_fence_and_enforcement(runtime, tmp_path):
    from graph.fence_scope import fence_scope
    from graph.middleware.subagent_fence import FENCE_DENY_ALL

    config = wf.sdk.STATE.graph_config
    config.enforcement_enabled = True
    config.enforcement_disallowed_tools = ["read_fixture"]
    with fence_scope(["different_tool"]):
        result = await wf._execute(registry(("a", {})), "context", {}, run_store=WorkflowRunStore(tmp_path))
    restrictions = result["review_context"]["steps"]["a"]["tool_restrictions"]
    assert restrictions["bound_tools"] == ["read_fixture"]
    assert restrictions["inherited_fence"] == [FENCE_DENY_ALL]
    assert restrictions["enforcement"]["enabled"] is True
    assert restrictions["enforcement"]["disallowed_tools"] == ["read_fixture"]


async def test_text_bounds_redaction_and_nested_observation(runtime, monkeypatch, tmp_path):
    from graph.tool_receipts import observe_subagent_context

    secret = "sk-" + "a" * 48
    monkeypatch.setattr(agent, "build_subagent_prompt", lambda *a: secret + " " + "x" * 20000)

    class Nested:
        async def astream(self, inputs, **kwargs):
            observe_subagent_context(prompt="nested", system_prompt="nested", subagent_type="nested",
                                     tools=[], fence=[], enforcement={})
            yield {"messages": [AIMessage(content="done")]}

    monkeypatch.setattr(agent, "create_agent", lambda **kw: Nested())
    store = WorkflowRunStore(tmp_path)
    result = await wf._execute(registry(("a", {})), "context", {}, run_store=store)
    context = result["review_context"]["steps"]["a"]
    assert context["inputs"]["prompt"]["text"] == "a"
    instruction = context["instruction_sources"]["system_prompt"]
    assert len(instruction["text"]) <= 16384
    assert instruction["omitted_characters"] > 0
    assert secret not in store.path(result["run_id"]).read_text()


async def test_construction_failure_is_not_observed(runtime, monkeypatch, tmp_path):
    def fail(**kwargs):
        raise ValueError("cannot construct")

    monkeypatch.setattr(agent, "create_agent", fail)
    result = await wf._execute(registry(("a", {})), "context", {}, run_store=WorkflowRunStore(tmp_path))
    step = result["review_context"]["steps"]["a"]
    assert step["status"] == "unknown" and step["conversation"] == "unknown"


def test_legacy_readback_and_retention(tmp_path):
    store = WorkflowRunStore(tmp_path)
    run_id = store.start("context", steps=[{"id": "a"}])
    assert store.load(run_id)["review_context"] == {"schema_version": 1, "status": "unknown"}
    for _ in range(12):
        store.begin_receipts([{"id": "a"}])
    context = store.load(run_id)["review_context"]
    assert len(context["previous_executions"]) == 8
    assert context["dropped_execution_count"] == 3
    assert store.load("absent") is None
