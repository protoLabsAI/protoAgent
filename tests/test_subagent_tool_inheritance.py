"""Dispatch permission boundaries, without a live model (#4327)."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

import graph.agent as agent
from background.manager import _subagent_fence
from graph.config import LangGraphConfig
from graph.middleware.subagent_fence import FENCE_DENY_ALL, intersect_fences
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from graph.tool_access import NEVER_INHERITED
from runtime.state import STATE


def stub(name, read=False):
    @tool(name)
    def impl() -> str:
        """A test tool."""
        return "ok"
    impl.metadata = {"access": "read"} if read else None
    return impl


@pytest.fixture
def setup(monkeypatch):
    config = LangGraphConfig(tools_subagent_only=["held_read"])
    tools = [stub("explicit"), stub("read_file"), stub("plugin_read", True),
             stub("write_file"), stub("update_artifact"), stub("held_read", True)]
    tools += [stub(n, True) for n in sorted(NEVER_INHERITED)]
    spec = SubagentConfig(name="inheritance-test", description="test", system_prompt="test", tools=["explicit"])
    monkeypatch.setitem(SUBAGENT_REGISTRY, spec.name, spec)
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(STATE, "graph", SimpleNamespace(bound_tools=tools))
    monkeypatch.setattr(STATE, "plugin_late_tool_factories", [])
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **k: list(tools))
    monkeypatch.setattr("tools.fs_tools.build_fs_tools", lambda c: [])
    return config, tools, spec


@pytest.mark.parametrize("path", ["task", "task_batch", "background", "manual"])
@pytest.mark.parametrize("case", ["default", "write", "empty", "optout", "policy"])
def test_effective_permissions(monkeypatch, setup, path, case):
    config, tools, spec = setup
    expected = ["explicit", "read_file", "plugin_read"]
    if case == "write":
        spec = replace(spec, tools=["write_file", "explicit", "write_file"])
        expected = ["write_file", "explicit", "read_file", "plugin_read"]
    elif case == "empty":
        spec = replace(spec, tools=[])
        expected = []
    elif case == "optout":
        spec = replace(spec, inherit_read_tools=False)
        expected = ["explicit"]
    elif case == "policy":
        spec = replace(spec, name="self-improve")
        monkeypatch.setattr("graph.self_improvement.review_tool_names", lambda c: {"plugin_read"})
        expected = ["plugin_read"]
    monkeypatch.setitem(SUBAGENT_REGISTRY, spec.name, spec)
    seen = []

    async def fake_run(**kwargs):
        selected = agent._effective_subagent_tools(kwargs["config"], spec, kwargs["tool_map"])
        seen.append([t.name for t in selected])
        return "ran"

    monkeypatch.setattr(agent, "_run_subagent", fake_run)
    if path == "background":
        fence = _subagent_fence(spec.name)
        assert fence == (expected or [FENCE_DENY_ALL])
        assert intersect_fences(fence, ["read_file"]) == (["read_file"] if "read_file" in expected else [FENCE_DENY_ALL])
        return
    if path == "manual":
        asyncio.run(agent.run_manual_subagent(config, description="test", prompt="test", subagent_type=spec.name))
    else:
        # Filesystem and plugin tools arrive AFTER task closure creation.
        holder = []
        task_tools = {t.name: t for t in agent._build_task_tools(config, tools[:1], lead_tools=holder)}
        holder.extend(tools)
        args = dict(description="test", prompt="test", subagent_type=spec.name)
        if path == "task_batch":
            args = {"tasks": [args]}
        asyncio.run(task_tools[path].ainvoke({"name": path, "args": args, "id": "test", "type": "tool_call"}))
    assert seen == [expected]


@pytest.mark.parametrize("path", ["task", "task_batch", "manual"])
def test_prompt_guard_inherits_reads_but_requires_write_optin(monkeypatch, setup, path):
    config, tools, spec = setup
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ran"

    monkeypatch.setattr(agent, "_run_subagent", fake_run)
    bound = {t.name: t for t in agent._build_task_tools(config, tools)}
    def invoke(prompt):
        args = dict(description="test", prompt=prompt, subagent_type=spec.name)
        if path == "manual":
            return asyncio.run(agent.run_manual_subagent(config, **args))
        if path == "task_batch":
            args = {"tasks": [args]}
        result = asyncio.run(bound[path].ainvoke({"name": path, "args": args, "id": "test", "type": "tool_call"}))
        return result.content

    assert "does not have tool(s) write_file" in invoke("Use write_file now")
    assert f"subagents.{spec.name}.tools" in invoke("Use write_file now")
    assert calls == []
    assert "ran" in invoke("Use read_file and plugin_read now")
    assert len(calls) == 1


def test_unbound_allowlist_warns_without_refusal(setup, caplog):
    config, tools, spec = setup
    spec.tools.append("missing_write")
    tool_map = {t.name: t for t in tools}
    assert agent._dispatch_tool_gap(config, spec.name, "Use missing_write", tool_map, tool_map) is None
    assert "missing_write" in caplog.text and "not bound" in caplog.text


@pytest.mark.parametrize("background", [False, True])
def test_graph_publishes_filesystem_and_late_read_objects(monkeypatch, setup, background):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    config, tools, spec = setup
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **k: tools[:1])
    monkeypatch.setattr("tools.fs_tools.build_fs_tools", lambda c: [tools[1]])

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setattr(agent, "create_llm", lambda *a, **k: FakeModel(messages=iter([AIMessage(content="ok")])))
    calls = []

    async def fake_run(**kwargs):
        calls.append(agent._effective_subagent_tools(config, spec, kwargs["tool_map"]))
        return "ran"

    class Background:
        async def spawn(self, **kwargs):
            calls.append(_subagent_fence(kwargs["subagent_type"]))
            return "job"

    monkeypatch.setattr(agent, "_run_subagent", fake_run)
    graph = agent.create_agent_graph(config, background_mgr=Background(), late_tool_factories=[lambda ts, c: tools[2]])
    monkeypatch.setattr(STATE, "graph", graph)
    task = next(t for t in graph.bound_tools if t.name == "task")
    args = dict(description="test", prompt="Use read_file and plugin_read", subagent_type=spec.name, run_in_background=background)
    result = asyncio.run(task.ainvoke({"name": "task", "args": args, "id": "test", "type": "tool_call"}))
    assert "Not dispatched" not in result.content
    assert len(calls) == 1
    if background:
        assert calls[0] == ["explicit", "read_file", "plugin_read"]
    else:
        assert calls[0] == tools[:3]


def test_inner_runner_binds_inherited_reads_and_intersects_parent(monkeypatch, setup):
    from langchain_core.messages import AIMessage
    from graph.fence_scope import fence_scope

    config, tools, spec = setup
    bound = []
    inputs = []

    class Runner:
        async def astream(self, state, **kwargs):
            inputs.append(state)
            yield {"messages": [AIMessage(content="done")]}

    def create(**kwargs):
        bound.extend(kwargs["tools"])
        return Runner()

    monkeypatch.setattr(agent, "create_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "create_agent", create)
    with fence_scope(["read_file", "write_file"]):
        asyncio.run(agent._run_subagent_inner(
            config=config, tool_map={t.name: t for t in tools}, available_subagents=spec.name,
            description="test", prompt="test", subagent_type=spec.name,
        ))
    assert bound == tools[:3]
    assert inputs[0]["subagent_fence"] == ["read_file"]


@pytest.mark.parametrize("batch", [False, True])
def test_background_write_gap_never_spawns(monkeypatch, setup, batch):
    config, tools, spec = setup
    spawned = []

    class Background:
        async def spawn(self, **kwargs):
            spawned.append(kwargs)
            return "job"

    bound = {t.name: t for t in agent._build_task_tools(config, tools, background_mgr=Background())}
    name = "task_batch" if batch else "task"
    args = dict(description="test", prompt="Use write_file", subagent_type=spec.name)
    args = {"tasks": [args], "run_in_background": True} if batch else {**args, "run_in_background": True}
    result = asyncio.run(bound[name].ainvoke({"name": name, "args": args, "id": "test", "type": "tool_call"}))
    assert f"Not dispatched: subagent '{spec.name}' does not have tool(s) write_file" in result.content
    assert f"subagents.{spec.name}.tools" in result.content
    assert spawned == []


def test_manual_assembles_filesystem_and_late_reads(monkeypatch, setup):
    config, tools, spec = setup
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **k: tools[:1])
    monkeypatch.setattr("tools.fs_tools.build_fs_tools", lambda c: [tools[1]])
    monkeypatch.setattr(STATE, "plugin_late_tool_factories", [lambda ts, c: tools[2]])
    seen = []

    async def fake_run(**kwargs):
        seen.extend(agent._effective_subagent_tools(config, spec, kwargs["tool_map"]))
        return "ran"

    monkeypatch.setattr(agent, "_run_subagent", fake_run)
    result = asyncio.run(agent.run_manual_subagent(
        config, description="test", prompt="Use read_file and plugin_read", subagent_type=spec.name,
    ))
    assert result == "ran"
    assert seen == tools[:3]


def test_researcher_read_prompt_dispatches(monkeypatch):
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ran"

    monkeypatch.setattr(agent, "_run_subagent", fake_run)
    task = agent._build_task_tools(LangGraphConfig(), [stub("read_file")])[0]
    result = asyncio.run(task.ainvoke({
        "name": "task", "id": "test", "type": "tool_call",
        "args": {"description": "read", "prompt": "Use read_file", "subagent_type": "researcher"},
    }))
    assert result.content == "ran"
    assert len(calls) == 1
