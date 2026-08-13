"""The watches feature flag (#2020, ADR 0067) — ``watches.enabled`` gates the agent-facing
watch tools, and NOTHING else.

Mirrors the ``goal.enabled`` gating in test_set_goal_tool.py: the flag controls TOOL
AVAILABILITY only — it never deletes or mutates stored watch state. It is also
INDEPENDENT of goal mode: the tools used to ride inside the goal-enabled group, which
coupled two unrelated dispositions (a goal the agent drives vs. a condition an external
process moves), so turning goal mode off silently took the watch tools with it."""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from runtime.state import STATE
from tools.lg_tools import get_all_tools

WATCH_TOOLS = {"create_watch", "list_watches", "update_watch", "clear_watch"}


def _register_verifiers(monkeypatch, *names):
    """Populate the plugin-verifier registry so get_all_tools gates open."""
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {n: object() for n in names})


# --- get_all_tools gating --------------------------------------------------


def test_watch_tools_absent_when_the_flag_is_off():
    # get_all_tools' own parameter default is off (callers thread the config value), so an
    # unflagged build binds nothing — the flag is what turns them on, not the call shape.
    names = {t.name for t in get_all_tools(goal_enabled=True)}
    assert not (WATCH_TOOLS & names)


def test_watch_tools_bound_when_flag_and_goal_mode_on(monkeypatch):
    _register_verifiers(monkeypatch, "test:check")
    names = {t.name for t in get_all_tools(goal_enabled=True, watches_enabled=True)}
    assert WATCH_TOOLS <= names


def test_watch_tools_are_independent_of_goal_mode(monkeypatch):
    # `watches.enabled` alone binds the tools — a watch is not a goal, so goal mode has no
    # say. (It used to: the tools were nested in the goal-enabled group, so an instance
    # that turned goal mode off lost watches it never asked to lose.)
    _register_verifiers(monkeypatch, "test:check")
    names = {t.name for t in get_all_tools(goal_enabled=False, watches_enabled=True)}
    assert WATCH_TOOLS <= names
    # ...and the goal tools stay gone, so this didn't just leak the whole group.
    assert not ({"set_goal", "abandon_goal"} & names)


def test_goal_tools_do_not_bring_watch_tools_along(monkeypatch):
    # The inverse coupling: goal mode on with watches off binds set_goal but no watch tool.
    _register_verifiers(monkeypatch, "test:check")
    names = {t.name for t in get_all_tools(goal_enabled=True, watches_enabled=False)}
    assert "set_goal" in names
    assert not (WATCH_TOOLS & names)


def test_no_goal_or_watch_tools_without_verifiers():
    # Both flags on but no plugin verifiers → all goal+watch tools suppressed.
    names = {t.name for t in get_all_tools(goal_enabled=True, watches_enabled=True)}
    assert not ({"set_goal", "update_goal_plan", "abandon_goal"} & names)
    assert not (WATCH_TOOLS & names)


# --- config plumbing -------------------------------------------------------


def test_watches_enabled_defaults_on_and_parses_from_yaml():
    assert LangGraphConfig().watches_enabled is True
    assert LangGraphConfig.from_dict({"watches": {"enabled": False}}).watches_enabled is False
    # Absent section -> the default (on).
    assert LangGraphConfig.from_dict({}).watches_enabled is True


def test_watch_interval_is_a_real_field_parsed_from_yaml():
    """Regression for the gap this replaced: `watch_interval` was read in three places via
    ``getattr(cfg, "watch_interval", 30)`` against a field that DID NOT EXIST, so the
    cadence silently pinned to 30s and `watches.interval` in YAML did nothing."""
    from graph.watches import DEFAULT_WATCH_INTERVAL_S

    assert LangGraphConfig().watch_interval == DEFAULT_WATCH_INTERVAL_S
    assert LangGraphConfig.from_dict({"watches": {"interval": 120}}).watch_interval == 120.0
    # Absent section / null value -> the default, never None (the loop does arithmetic on it).
    assert LangGraphConfig.from_dict({}).watch_interval == DEFAULT_WATCH_INTERVAL_S
    assert LangGraphConfig.from_dict({"watches": {"interval": None}}).watch_interval == DEFAULT_WATCH_INTERVAL_S


def test_watch_config_keys_are_reachable_from_settings():
    """Both keys were missing from FIELDS, so neither rendered in the Settings UI — the
    only way to enable watches was hand-editing YAML on an install that already ships the
    Watches panel."""
    from graph.settings_schema import FIELDS, _SECTION_CATEGORY

    by_key = {f.key: f for f in FIELDS}
    assert "watches.enabled" in by_key and not by_key["watches.enabled"].ui_hidden
    assert "watches.interval" in by_key and not by_key["watches.interval"].ui_hidden
    assert by_key["watches.enabled"].attr == "watches_enabled"
    assert by_key["watches.interval"].attr == "watch_interval"
    # A core section missing from the category map renders nowhere.
    assert _SECTION_CATEGORY.get("Watches") == "Behavior"


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


def test_watch_tools_bound_to_lead_graph_only_when_enabled(monkeypatch):
    _register_verifiers(monkeypatch, "test:check")
    on = _lead_graph_tool_names(goal_enabled=True, watches_enabled=True)
    assert WATCH_TOOLS <= on
    off = _lead_graph_tool_names(goal_enabled=True, watches_enabled=False)
    assert not (WATCH_TOOLS & off)
    # Goal mode off must still bind them on the COMPILED graph, not just in get_all_tools —
    # create_agent_graph threads the two flags separately or the decoupling is cosmetic.
    goalless = _lead_graph_tool_names(goal_enabled=False, watches_enabled=True)
    assert WATCH_TOOLS <= goalless


# --- the POLLER, not the tools (#2320) -------------------------------------


def test_the_watch_poller_is_started_unconditionally():
    """The regression this guards is the LOOP, not the tool gating above.

    ``_watch_loop`` used to be started inside the ``goal_enabled`` branch — a leftover of
    the deleted ADR 0030 monitor-goal loop it grew out of. ``POST /api/watches`` has no
    such gate, so a goal-mode-off instance accepted watches, persisted them, listed them
    ``active`` in the console, and never polled one. Nothing raised; the watches simply
    never fired, which is why it survived two releases.

    Asserted STRUCTURALLY because the start site is a closure registered with
    ``@fastapi_app.on_event("startup")`` inside the app builder — reaching it functionally
    means booting the whole server. What the fix *is*, precisely, is "this statement has no
    enclosing conditional", so that is what this checks. Same technique as
    tests/test_bundled_config_assets.py.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "server" / "__init__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    # Every `_watch_loop()` CALL (the import of the same name is an alias, not a Call),
    # paired with the chain of nodes enclosing it.
    found: list[tuple[ast.Call, list[ast.AST]]] = []

    def visit(node: ast.AST, ancestors: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "_watch_loop":
                found.append((child, [*ancestors, node]))
            visit(child, [*ancestors, node])

    visit(tree, [])
    assert found, "no _watch_loop() start found in server/__init__.py — did the poller move?"

    for call, ancestors in found:
        gates = [n for n in ancestors if isinstance(n, (ast.If, ast.IfExp))]
        assert not gates, (
            f"the watch poller (line {call.lineno}) is started inside a conditional at line "
            f"{gates[0].lineno}. It must start unconditionally: any instance where that "
            "condition is false will accept watches on POST /api/watches, list them active, "
            "and silently never poll one (#2320). The idle tick is a no-op on an empty "
            "store, which is what makes the unconditional start affordable."
        )


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
