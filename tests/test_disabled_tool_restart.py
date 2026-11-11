"""Loaded config exclusions are visible and cannot be repaired by a restart."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.tools import StructuredTool

from graph.agent import create_agent_graph
from graph.config import LangGraphConfig
from graph.prompts import build_system_prompt, build_system_prompt_parts
from tools.lg_tools import set_disabled_tools
from tools.restart_tools import disabled_restart_reason, disabled_tool_groups, disabled_tools_explanation


def fake_tool(name, metadata=None):
    return StructuredTool.from_function(lambda: "ok", name=name, description="Fake tool.", metadata=metadata)


@pytest.fixture(autouse=True)
def reset_denylist():
    yield
    set_disabled_tools([])


def test_groups_are_sorted_deduplicated_and_use_metadata_then_prefix_or_name():
    tools = [
        fake_tool("gmail_search", {"category": "Google Mail"}),
        fake_tool("gmail_send", {"category": "Google Mail"}),
        fake_tool("calendar__list"),
        fake_tool("calculator"),
    ]
    groups = disabled_tool_groups(tools + tools, [t.name for t in tools])
    assert groups == (
        ("Google Mail", ("gmail_search", "gmail_send")),
        ("calculator", ("calculator",)),
        ("calendar", ("calendar__list",)),
    )
    assert disabled_tool_groups(list(reversed(tools)), [t.name for t in tools]) == groups
    assert "Settings > Tools" in disabled_restart_reason("load Google Mail tools", groups)


def test_only_loaded_public_exclusions_are_described():
    tools = [fake_tool("gmail_search"), fake_tool("secret_search"), fake_tool("held_tool")]
    groups = disabled_tool_groups(tools, ["gmail_search", "secret_search", "unloaded"], ["secret_search"])
    assert groups == (("gmail", ("gmail_search",)),)
    assert disabled_tool_groups(tools, [], ["secret_search"]) == ()
    assert disabled_tool_groups(tools, [" gmail_search ", "secret_search"], [" secret_search "]) == groups
    assert disabled_tools_explanation(()) == ""


def test_prompt_join_preserves_labeled_notice():
    notice = disabled_tools_explanation((("gmail", ("gmail_search",)),))
    kwargs = {"include_subagents": False, "disabled_tools_notice": notice}
    parts = build_system_prompt_parts(**kwargs)
    assert dict(parts)["Config-disabled tools"] == notice
    assert build_system_prompt(**kwargs) == "\n\n".join(text for _, text in parts)
    assert "Config-disabled tools" not in dict(build_system_prompt_parts())


@pytest.mark.parametrize("reason", [
    "load gmail_search", "Restart to enable gmail tools", "Load `gmail_search`.",
    "Please restart the agent to activate gmail integration",
])
def test_explicit_capability_requests_match(reason):
    assert "Restart cannot enable them" in disabled_restart_reason(reason, (("gmail", ("gmail_search",)),))


@pytest.mark.parametrize("reason", [
    "Plugin updated", "Restart after gmail plugin update", "Reload gmail plugin code",
    "Do not enable gmail", "load gmail_search_backup", "load another plugin",
    "load gmail and apply a security update", "Update plugin so it can load gmail later",
])
def test_ambiguous_or_unrelated_reasons_keep_consent(reason):
    assert disabled_restart_reason(reason, (("gmail", ("gmail_search",)),)) is None


@pytest.fixture
def build_graph(monkeypatch):
    # Exercise real assembly/get_all_tools; avoid any provider or credential setup.
    monkeypatch.setattr("graph.agent.create_llm", lambda config: object())
    monkeypatch.setattr("graph.agent._build_middleware", lambda *args, **kwargs: [])

    def compile_graph(**kwargs):
        graph = SimpleNamespace(prompt=kwargs["system_prompt"])
        graph.with_config = lambda config: graph
        return graph

    monkeypatch.setattr("graph.agent.create_agent", compile_graph)

    def build(**overrides):
        return create_agent_graph(
            LangGraphConfig(**overrides), include_subagents=False,
            extra_tools=[fake_tool("gmail_search"), fake_tool("secret_search"), fake_tool("held_tool")],
            restart_scheduler=scheduler,
        )

    scheduler = Mock(return_value={"scheduled": True})
    return build, scheduler


def restart_tool(graph):
    return next(t for t in graph.bound_tools if t.name == "self_restart")


async def test_assembled_prompt_and_guard_refresh_on_rebuild(build_graph, monkeypatch):
    build, scheduler = build_graph
    approval = Mock(return_value="approve")
    monkeypatch.setattr("tools.restart_tools.interrupt", approval)
    disabled = build(tools_disabled=["gmail_search", "secret_search"], tools_hidden=["secret_search"],
                     tools_subagent_only=["held_tool"])
    notice = dict(disabled.system_prompt_parts)["Config-disabled tools"]
    assert "gmail_search" in notice and "tools.disabled" in notice and "Settings > Tools" in notice
    assert "secret_search" not in disabled.prompt and "held_tool" not in notice
    assert notice in disabled.prompt
    for _ in range(2):
        result = await restart_tool(disabled).ainvoke({"reason": "load gmail_search"})
        assert "Restart cannot enable them" in result
    approval.assert_not_called()
    scheduler.assert_not_called()

    enabled = build()
    assert "Config-disabled tools" not in dict(enabled.system_prompt_parts)
    assert "gmail_search" in {t.name for t in enabled.bound_tools}
    assert (await restart_tool(enabled).ainvoke({"reason": "load gmail_search"})).startswith("Restart scheduled:")
    approval.assert_called_once()
    scheduler.assert_called_once_with("load gmail_search")
    # Building a second agent must not overwrite the first graph's guard.
    assert "Not restarted:" in await restart_tool(disabled).ainvoke({"reason": "load gmail_search"})
    scheduler.assert_called_once()


@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_plugin_update_still_requires_consent_with_disabled_tools(build_graph, monkeypatch, decision):
    build, scheduler = build_graph
    approval = Mock(return_value=decision)
    monkeypatch.setattr("tools.restart_tools.interrupt", approval)
    graph = build(tools_disabled=["gmail_search"])
    result = await restart_tool(graph).ainvoke({"reason": "Restart after gmail plugin update"})
    approval.assert_called_once()
    if decision == "approve":
        scheduler.assert_called_once()
        assert result.startswith("Restart scheduled:")
    else:
        scheduler.assert_not_called()
        assert "explicit operator approval" in result
