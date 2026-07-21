"""The watches feature flag (#2020, ADR 0067) — gate the agent-facing watch tools
behind ``watches.enabled`` (default OFF) so the feature can cook before shipping on.

Mirrors the ``goal.enabled`` gating in test_set_goal_tool.py: the flag controls TOOL
AVAILABILITY only. It never deletes or mutates stored watch state, and the watch tools
ride INSIDE the goal-enabled tool group (goal mode must also be on)."""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from runtime.state import STATE
from tools.lg_tools import get_all_tools

WATCH_TOOLS = {"create_watch", "list_watches", "clear_watch"}


# --- get_all_tools gating --------------------------------------------------


def test_watch_tools_absent_by_default_even_with_goal_mode_on():
    # Default: goal mode on, watches flag off -> the three watch tools are NOT bound.
    names = {t.name for t in get_all_tools(goal_enabled=True)}
    assert not (WATCH_TOOLS & names)


def test_watch_tools_bound_when_flag_and_goal_mode_on():
    names = {t.name for t in get_all_tools(goal_enabled=True, watches_enabled=True)}
    assert WATCH_TOOLS <= names


def test_watch_tools_require_goal_mode():
    # The watch tools ride inside the goal-enabled group; with goal mode off the flag
    # alone binds nothing (parity with how set_goal is gated).
    names = {t.name for t in get_all_tools(goal_enabled=False, watches_enabled=True)}
    assert not (WATCH_TOOLS & names)


# --- config plumbing -------------------------------------------------------


def test_watches_enabled_defaults_off_and_parses_from_yaml():
    assert LangGraphConfig().watches_enabled is False
    assert LangGraphConfig.from_dict({"watches": {"enabled": True}}).watches_enabled is True
    # Absent section -> the default (off).
    assert LangGraphConfig.from_dict({}).watches_enabled is False


# --- actually bound to the compiled lead graph -----------------------------


def _lead_graph_tool_names(*, goal_enabled: bool, watches_enabled: bool) -> set[str]:
    """Bound tool names on a compiled lead-agent graph (what the MODEL can call).

    Stubs the LLM so no gateway is needed; reads the ToolNode's tool map. Mirrors
    test_set_goal_tool.py's bd-2aa regression guard — get_all_tools returning a tool is
    NOT enough; create_agent_graph must thread the flag into that call or the tool is
    advertised but never bound."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **k):
            return self

    fake = _Fake(messages=iter([AIMessage(content="x")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(
            LangGraphConfig(goal_enabled=goal_enabled, watches_enabled=watches_enabled)
        )
    node = g.nodes["tools"]
    for obj in (node, getattr(node, "runnable", None), getattr(node, "bound", None)):
        tbn = getattr(obj, "tools_by_name", None)
        if tbn:
            return set(tbn.keys())
    raise AssertionError("could not locate the ToolNode tool map")


def test_watch_tools_bound_to_lead_graph_only_when_enabled():
    on = _lead_graph_tool_names(goal_enabled=True, watches_enabled=True)
    assert WATCH_TOOLS <= on
    off = _lead_graph_tool_names(goal_enabled=True, watches_enabled=False)
    assert not (WATCH_TOOLS & off)


# --- preservation semantics (issue #2020) ----------------------------------


def test_disabling_the_flag_does_not_touch_stored_watches(monkeypatch, tmp_path):
    # The flag is tool-availability only: building the toolset with watches off must not
    # clear or mutate an existing watch (no destructive call is keyed to the flag).
    ctrl = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    monkeypatch.setattr(STATE, "watch_controller", ctrl)
    ctrl.create(condition="deploy is green", verifier={"type": "plugin", "check": "p:v"})
    before = [w.id for w in ctrl.list_watches()]
    assert before  # sanity: the watch exists

    get_all_tools(goal_enabled=True, watches_enabled=False)  # flag off -> tools unbound

    after = [w.id for w in ctrl.list_watches()]
    assert after == before  # stored watch survived the disabled build untouched
