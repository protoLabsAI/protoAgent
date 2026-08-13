"""The LLM-facing set_goal and list_verifiers tools (ADR 0028)."""

from __future__ import annotations

import asyncio

from observability import tracing
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from runtime.state import STATE
from tools.lg_tools import _build_list_verifiers_tool, _build_set_goal_tool, get_all_tools


def _register_verifiers(monkeypatch, *names):
    """Register plugin verifiers so set_goal_safe's name check accepts them (the
    registry is empty in unit tests). Only the keys matter for validation."""
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {n: object() for n in names})


def test_get_all_tools_gates_set_goal_on_goal_enabled():
    on = {t.name for t in get_all_tools(goal_enabled=True)}
    off = {t.name for t in get_all_tools(goal_enabled=False)}
    assert "set_goal" in on and "set_goal" not in off


def _lead_graph_tool_names(goal_enabled: bool) -> set[str]:
    """Bound tool names on a compiled lead-agent graph (what the MODEL can call).

    Stubs the LLM so no gateway is needed; reads the ToolNode's tool map.
    """
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **k):
            return self

    fake = _Fake(messages=iter([AIMessage(content="x")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph
        from graph.config import LangGraphConfig

        g = create_agent_graph(LangGraphConfig(goal_enabled=goal_enabled))
    node = g.nodes["tools"]
    for obj in (node, getattr(node, "runnable", None), getattr(node, "bound", None)):
        tbn = getattr(obj, "tools_by_name", None)
        if tbn:
            return set(tbn.keys())
    raise AssertionError("could not locate the ToolNode tool map")


def test_set_goal_is_actually_bound_to_the_lead_agent_when_enabled():
    # bd-2aa regression: get_all_tools(goal_enabled=True) including set_goal is
    # NOT enough — create_agent_graph must thread goal_enabled into that call, or
    # set_goal is advertised (/api/tools) but never bound to the model. This
    # asserts the binding on the COMPILED graph, which is what was broken.
    assert "set_goal" in _lead_graph_tool_names(goal_enabled=True)
    assert "set_goal" not in _lead_graph_tool_names(goal_enabled=False)


def test_set_goal_reads_session_from_injected_state(monkeypatch, tmp_path):
    # Companion to bd-3b6: once bound, set_goal must resolve the session from the
    # injected graph state, not the contextvar (empty in a tool body) — else it
    # would always refuse with "No active session" mid-turn.
    ctrl = GoalController(None, GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(tracing, "current_session_id", lambda: "")  # contextvar empty
    _register_verifiers(monkeypatch, "spacetraders:credits")
    out = _build_set_goal_tool().invoke(
        {
            "condition": "reach 1M",
            "check": "spacetraders:credits",
            "check_args": {"min": 1_000_000},
            "state": {"session_id": "s-injected"},
        }
    )
    assert "Goal set" in out
    assert ctrl.active_goal("s-injected") is not None


def test_set_goal_reports_when_goal_mode_off(monkeypatch):
    monkeypatch.setattr(STATE, "goal_controller", None)
    out = _build_set_goal_tool().invoke({"condition": "c", "check": "x:y"})
    assert "not enabled" in out


def test_set_goal_needs_an_active_session(monkeypatch, tmp_path):
    monkeypatch.setattr(STATE, "goal_controller", GoalController(None, GoalStore(base_dir=str(tmp_path))))
    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    out = _build_set_goal_tool().invoke({"condition": "c", "check": "x:y"})
    assert "No active session" in out


def test_set_goal_sets_a_plugin_verified_goal(monkeypatch, tmp_path):
    ctrl = GoalController(None, GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(tracing, "current_session_id", lambda: "s1")
    _register_verifiers(monkeypatch, "spacetraders:credits")
    out = _build_set_goal_tool().invoke(
        {"condition": "reach 1M", "check": "spacetraders:credits", "check_args": {"min": 1_000_000}}
    )
    assert "Goal set" in out
    g = ctrl.active_goal("s1")
    assert g is not None
    assert g.verifier == {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1_000_000}}


def test_set_goal_rejects_an_unknown_verifier(monkeypatch, tmp_path):
    # An unregistered verifier must be rejected up front — otherwise set_goal
    # creates a goal that can never pass and spins to the iteration cap (the
    # 'unknown plugin verifier' / unachievable loop found on the live agent).
    ctrl = GoalController(None, GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(tracing, "current_session_id", lambda: "s1")
    _register_verifiers(monkeypatch, "spacetraders:credits")  # 'manual' is NOT registered
    out = _build_set_goal_tool().invoke({"condition": "reply DONE", "check": "manual"})
    assert "unknown plugin verifier" in out.lower()
    assert "spacetraders:credits" in out  # lists what IS available
    assert ctrl.active_goal("s1") is None  # no unsatisfiable goal created


# ── list_verifiers tool (#2686) ────────────────────────────────────────────────


def test_list_verifiers_appears_when_goal_enabled():
    on = {t.name for t in get_all_tools(goal_enabled=True)}
    off = {t.name for t in get_all_tools(goal_enabled=False, watches_enabled=False)}
    assert "list_verifiers" in on
    assert "list_verifiers" not in off


def test_list_verifiers_appears_when_watches_enabled():
    on = {t.name for t in get_all_tools(watches_enabled=True)}
    off = {t.name for t in get_all_tools(goal_enabled=False, watches_enabled=False)}
    assert "list_verifiers" in on
    assert "list_verifiers" not in off


def test_list_verifiers_returns_core_types():
    tool = _build_list_verifiers_tool()
    out = asyncio.run(tool.ainvoke({}))
    for name in ("command", "test", "ci", "data", "llm", "plugin"):
        assert name in out


def test_list_verifiers_no_plugin_verifiers_message(monkeypatch):
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {})
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIER_META", {})
    tool = _build_list_verifiers_tool()
    out = asyncio.run(tool.ainvoke({}))
    assert "No plugin verifiers registered." in out
    assert "command" in out  # core types still present


def test_list_verifiers_includes_plugin_checks(monkeypatch):
    monkeypatch.setattr(
        "graph.goals.verifiers._PLUGIN_VERIFIERS",
        {"spacetraders:credits": object()},
    )
    monkeypatch.setattr(
        "graph.goals.verifiers._PLUGIN_VERIFIER_META",
        {"spacetraders:credits": {"plugin_id": "spacetraders", "description": "Credits balance check"}},
    )
    tool = _build_list_verifiers_tool()
    out = asyncio.run(tool.ainvoke({}))
    assert "spacetraders:credits" in out
    assert "Credits balance check" in out
    assert "No plugin verifiers registered." not in out
