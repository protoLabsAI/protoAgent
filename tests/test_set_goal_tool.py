"""The LLM-facing set_goal tool — agent owns a plugin-verified goal (ADR 0028)."""

from __future__ import annotations

from observability import tracing
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from runtime.state import STATE
from tools.lg_tools import _build_set_goal_tool, get_all_tools


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
    out = _build_set_goal_tool().invoke(
        {"condition": "reach 1M", "check": "spacetraders:credits",
         "check_args": {"min": 1_000_000}, "state": {"session_id": "s-injected"}}
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
    out = _build_set_goal_tool().invoke(
        {"condition": "reach 1M", "check": "spacetraders:credits", "check_args": {"min": 1_000_000}}
    )
    assert "Goal set" in out
    g = ctrl.active_goal("s1")
    assert g is not None
    assert g.verifier == {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1_000_000}}
