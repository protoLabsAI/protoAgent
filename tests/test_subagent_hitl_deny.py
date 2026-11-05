"""Subagents must never be bound a lead-only interrupt tool: the answer-type HITL tools
(ask_human / request_user_input) or the approval-type self_restart. They run on a
checkpointer-less graph and can't resume an interrupt. graph.agent._subagent_tools hard-denies
every tool in SUBAGENT_DENIED_TOOLS even if a SubagentConfig.tools allowlist names one — the
enforced backstop to the convention."""

from __future__ import annotations

from graph.agent import _subagent_tools
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from tools.lg_tools import HITL_TOOL_NAMES, SUBAGENT_DENIED_TOOLS, get_all_tools


def _tool_map() -> dict:
    return {t.name: t for t in get_all_tools(restart_scheduler=lambda reason: {"scheduled": True})}


def _cfg(tools: list[str]) -> SubagentConfig:
    return SubagentConfig(name="probe", description="", system_prompt="", tools=tools)


def test_self_restart_is_denied_but_not_answer_type():
    # self_restart is hard-denied to subagents (approval-type interrupt) but is NOT an
    # answer-type HITL tool — the fence must decline it on a resume, not run it (#4577).
    assert "self_restart" in SUBAGENT_DENIED_TOOLS
    assert "self_restart" not in HITL_TOOL_NAMES
    assert HITL_TOOL_NAMES < SUBAGENT_DENIED_TOOLS


def test_denied_tools_exist_in_full_lead_set():
    # Guards the test's premise: the lead agent DOES get these (they're only denied to subagents).
    assert SUBAGENT_DENIED_TOOLS <= set(_tool_map())


def test_subagent_tools_hard_denies_lead_only_even_when_listed():
    tm = _tool_map()
    bound = {t.name for t in _subagent_tools(_cfg(["current_time", "ask_human", "request_user_input", "self_restart"]), tm)}
    assert "current_time" in bound  # the legitimate tool is kept
    assert bound.isdisjoint(SUBAGENT_DENIED_TOOLS)  # ask_human, request_user_input AND self_restart dropped
    assert "self_restart" not in bound


def test_subagent_tools_leaves_normal_allowlist_untouched():
    tm = _tool_map()
    bound = {t.name for t in _subagent_tools(_cfg(["current_time", "web_search"]), tm)}
    assert bound == {"current_time", "web_search"}


def test_no_registered_subagent_lists_a_lead_only_tool():
    # The convention itself: no shipped subagent allowlist names a lead-only interrupt tool.
    for name, cfg in SUBAGENT_REGISTRY.items():
        assert set(cfg.tools).isdisjoint(SUBAGENT_DENIED_TOOLS), f"subagent '{name}' lists a lead-only tool"
