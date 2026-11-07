"""Legacy allowlists migrate at resolution, never by exposing tool aliases (#4630)."""

from dataclasses import replace
import logging

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent
import graph.subagents.config as subagent_config
from graph.config import LangGraphConfig
from graph.plugins.registry import PluginRegistry
from graph.subagents.config import RESEARCHER_CONFIG, SUBAGENT_REGISTRY, SubagentConfig
from knowledge.store import KnowledgeStore
from server import agent_init
from tools.lg_tools import get_all_tools


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, caplog):
    monkeypatch.setattr(agent, "_SUBAGENT_TOOL_WARNINGS", set())
    monkeypatch.setattr(agent_init, "_plugin_subagent_names", set())
    # The startup helpers mutate the shared registry in place.
    monkeypatch.setattr("graph.subagents.config.SUBAGENT_REGISTRY", dict(SUBAGENT_REGISTRY))
    monkeypatch.setattr(agent, "SUBAGENT_REGISTRY", subagent_config.SUBAGENT_REGISTRY)
    caplog.set_level(logging.WARNING, logger="graph.agent")


@pytest.fixture
def tool_map(tmp_path):
    store = KnowledgeStore(db_path=str(tmp_path / "agent.db"))
    return {t.name: t for t in get_all_tools(knowledge_store=store)}


def spec(names, name="migration-probe"):
    return SubagentConfig(name=name, description="test", system_prompt="test", tools=names, inherit_read_tools=False)


def test_config_subagent(tmp_path, tool_map, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("subagents:\n  researcher:\n    tools: [memory_recall, memory_list]\n")
    config = LangGraphConfig.from_yaml(path)
    agent_init._apply_config_subagents(config)
    sub = replace(agent.SUBAGENT_REGISTRY["researcher"], inherit_read_tools=False)
    for _ in range(2):
        assert [t.name for t in agent._effective_subagent_tools(config, sub, tool_map)] == ["memory_search", "memory_list"]
    assert len(caplog.records) == 1
    assert all(word in caplog.text for word in ["researcher", "memory_recall", "memory_search", "Update", "source"])
    assert sub.tools == ["memory_recall", "memory_list"]


def test_plugin_registered_subagent(tmp_path, tool_map, caplog):
    registry = PluginRegistry("migration-plugin", tmp_path)
    registry.register_subagent(spec(["memory_recall"], "plugin-worker"))
    agent_init._register_plugin_subagents(registry.subagents)
    sub = agent.SUBAGENT_REGISTRY["plugin-worker"]
    assert [t.name for t in agent._effective_subagent_tools(LangGraphConfig(), sub, tool_map)] == ["memory_search"]
    assert "plugin-worker" in caplog.text


@pytest.mark.parametrize("old,new", [
    ("memory_ingest", "memory_save"), ("memory_recall", "memory_search"),
    ("forget_memory", "memory_forget"), ("memory_stats", "memory_list"),
])
def test_all_renames_deduplicate_and_warn_once(old, new, tool_map, caplog):
    sub = spec([old, new, old])
    for _ in range(2):
        assert [t.name for t in agent._subagent_tools(sub, tool_map)] == [new]
        assert agent._subagent_tool_names(sub, tool_map, lead_tools=tool_map.values()) == [new]
    assert len(caplog.records) == 1
    assert old in caplog.text and sub.name in caplog.text
    agent._subagent_tools(replace(sub, name="other-worker"), tool_map)
    assert len(caplog.records) == 2


def test_builtin_and_lead_tool_names(tool_map):
    before = dict(tool_map)
    sub = replace(RESEARCHER_CONFIG, tools=["memory_recall"], inherit_read_tools=False)
    assert [t.name for t in agent._effective_subagent_tools(LangGraphConfig(), sub, tool_map)] == ["memory_search"]
    assert tool_map == before
    assert set(tool_map).isdisjoint({"memory_ingest", "memory_recall", "forget_memory", "memory_stats"})
    assert {"memory_save", "memory_search", "memory_list", "memory_forget"} <= tool_map.keys()


async def test_unknown_tool_warns_once_and_subagent_builds(monkeypatch, tool_map, caplog):
    sub = spec(["missing_plugin_tool", "memory_list", "missing_plugin_tool"])
    monkeypatch.setitem(agent.SUBAGENT_REGISTRY, sub.name, sub)
    bound = []

    class FakeAgent:
        async def astream(self, *args, **kwargs):
            yield {"messages": [AIMessage(content="done")]}

    def create(**kwargs):
        bound.append([t.name for t in kwargs["tools"]])
        return FakeAgent()

    monkeypatch.setattr(agent, "create_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "create_agent", create)
    for _ in range(2):
        result = await agent._run_subagent(
            config=LangGraphConfig(), tool_map=tool_map, available_subagents=sub.name,
            description="test", prompt="test", subagent_type=sub.name,
        )
        assert result == "[migration-probe completed: test]\n\ndone"
    assert bound == [["memory_list"], ["memory_list"]]
    warnings = [r for r in caplog.records if "missing_plugin_tool" in r.message]
    assert len(warnings) == 1
    assert sub.name in warnings[0].message
