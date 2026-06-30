"""Subagents must never be bound the lead-only HITL interrupt tools (ask_human /
request_user_input): they run on a checkpointer-less graph and can't resume an interrupt.
graph.agent._subagent_tools hard-denies them even if a SubagentConfig.tools allowlist names
one — the enforced backstop to the convention."""

from __future__ import annotations

from graph.agent import _subagent_tools
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from tools.lg_tools import HITL_TOOL_NAMES, get_all_tools


def _tool_map() -> dict:
    return {t.name: t for t in get_all_tools()}


def _cfg(tools: list[str]) -> SubagentConfig:
    return SubagentConfig(name="probe", description="", system_prompt="", tools=tools)


def test_hitl_tools_exist_in_full_lead_set():
    # Guards the test's premise: the lead agent DOES get these (they're only denied to subagents).
    assert HITL_TOOL_NAMES <= set(_tool_map())


def test_subagent_tools_hard_denies_hitl_even_when_listed():
    tm = _tool_map()
    bound = {t.name for t in _subagent_tools(_cfg(["current_time", "ask_human", "request_user_input"]), tm)}
    assert "current_time" in bound  # the legitimate tool is kept
    assert bound.isdisjoint(HITL_TOOL_NAMES)  # both HITL tools dropped


def test_subagent_tools_leaves_normal_allowlist_untouched():
    tm = _tool_map()
    bound = {t.name for t in _subagent_tools(_cfg(["current_time", "web_search"]), tm)}
    assert bound == {"current_time", "web_search"}


def test_no_registered_subagent_lists_a_hitl_tool():
    # The convention itself: no shipped subagent allowlist names a HITL tool.
    for name, cfg in SUBAGENT_REGISTRY.items():
        assert set(cfg.tools).isdisjoint(HITL_TOOL_NAMES), f"subagent '{name}' lists a HITL tool"
