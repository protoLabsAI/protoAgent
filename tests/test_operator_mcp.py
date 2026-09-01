"""Operator MCP server (ADR 0033 slice 1) — allowlist-gated tool exposure."""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from graph.config import LangGraphConfig
from runtime.state import STATE
from server.operator_mcp import build_server, operator_tools


def _cfg(tools):
    c = LangGraphConfig()
    c.operator_mcp_tools = list(tools)
    c.goal_enabled = False
    return c


@pytest.fixture(autouse=True)
def _bare_state(monkeypatch):
    # No stores → get_all_tools returns just the keyless core tools; no plugin tools.
    for attr in ("knowledge_store", "scheduler", "inbox_store", "tasks_store"):
        monkeypatch.setattr(STATE, attr, None, raising=False)
    monkeypatch.setattr(STATE, "plugin_tools", [], raising=False)


def test_resolver_lives_in_runtime_and_is_reexported():
    """The allowlist/profile resolution moved to runtime/ (ADR 0075 D2) so operator_api can
    import it without breaking the import-layering contract; server.operator_mcp re-exports
    the same objects for existing callers."""
    import runtime.operator_mcp_tools as rt
    import server.operator_mcp as sm

    assert sm.operator_tools is rt.operator_tools
    assert sm.resolve_allow is rt.resolve_allow
    assert sm.resolve_exposed_names is rt.resolve_exposed_names


def test_allowlist_filters_to_named_tools():
    names = {t.name for t in operator_tools(_cfg(["calculator", "current_time"]))}
    assert names == {"calculator", "current_time"}


def test_empty_allowlist_exposes_nothing():
    assert operator_tools(_cfg([])) == []


def test_boot_stores_builds_skills_index(tmp_path, monkeypatch):
    """The sidecar must build STATE.skills_index, not just the other stores —
    load_skill / list_skills / save_skill read it, and a fresh sidecar process
    starts with it None. Regression: an ACP agent calling load_skill through this
    server got "Skills index is not available." despite the prompt listing skills."""
    import types

    import server.agent_init as ai
    from server.operator_mcp import _boot_stores_only

    # Stub the heavy/side-effecting store builders; let the REAL _build_skills_index run.
    monkeypatch.setattr(ai, "_build_knowledge_store", lambda c: None)
    monkeypatch.setattr(ai, "_build_scheduler", lambda c: None)
    monkeypatch.setattr(ai, "_build_inbox_store", lambda c: None)
    monkeypatch.setattr(ai, "_apply_plugin_knowledge_backend", lambda c, ks, p: ks)
    monkeypatch.setattr(
        ai,
        "_build_plugins",
        # The registry fields ride every real bundle; _boot_stores_only now applies them
        # (#3248), so the duck-typed stub carries empty ones.
        lambda config, existing_tools=None: types.SimpleNamespace(
            tools=[], skill_dirs=[], meta={},
            goal_verifiers={}, goal_verifier_meta=None, goal_hooks={}, watch_hooks={}, lifecycle_hooks={},
        ),
    )
    monkeypatch.setattr(STATE, "tasks_store", object(), raising=False)  # skip real TaskStore
    monkeypatch.setattr(STATE, "skills_index", None, raising=False)

    cfg = _cfg([])
    cfg.skills_db_path = str(tmp_path / "skills.db")  # don't touch the real DB
    _boot_stores_only(cfg)

    assert STATE.skills_index is not None  # the fix — was None before
    # It's a real index the curation tools can query (bundled config/skills seed).
    assert {s["name"] for s in STATE.skills_index.skill_summaries()}


def test_plugin_tools_ride_the_same_bridge(monkeypatch):
    @tool
    def my_plugin_tool(x: str) -> str:
        """A plugin-contributed tool."""
        return x

    monkeypatch.setattr(STATE, "plugin_tools", [my_plugin_tool], raising=False)
    names = {t.name for t in operator_tools(_cfg(["my_plugin_tool", "calculator"]))}
    assert names == {"my_plugin_tool", "calculator"}  # core + plugin, one allowlist


def test_build_server_exposes_allowlisted_as_mcp():
    server, exposed = build_server(_cfg(["calculator"]))
    assert exposed == ["calculator"]
    assert server is not None


def test_star_exposes_all_except_execute_code(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def execute_code(code: str) -> str:
        """run code"""
        return code

    @tool
    def plugin_thing(x: str) -> str:
        """a plugin tool"""
        return x

    monkeypatch.setattr(STATE, "plugin_tools", [execute_code, plugin_thing], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*"]))}
    assert "calculator" in names and "plugin_thing" in names  # core + plugin all flow
    assert "execute_code" not in names  # excluded from the wildcard


def test_star_plus_explicit_name_still_includes_it(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def execute_code(code: str) -> str:
        """run code"""
        return code

    monkeypatch.setattr(STATE, "plugin_tools", [execute_code], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*", "execute_code"]))}
    assert "execute_code" in names  # naming it explicitly overrides the wildcard exclusion


# ── HITL hard-exclusion (ADR 0075 D3 — a real bug: these HANG a foreign MCP client) ──


def test_hitl_tools_never_exposed_even_via_star():
    # ask_human / request_user_input are in the keyless core, so "*" would grab them —
    # but they pause the turn via a LangGraph interrupt only the lead runner resumes.
    names = {t.name for t in operator_tools(_cfg(["*"]))}
    assert "ask_human" not in names and "request_user_input" not in names


def test_hitl_tools_never_exposed_even_when_named():
    names = {t.name for t in operator_tools(_cfg(["ask_human", "request_user_input", "calculator"]))}
    assert names == {"calculator"}  # the HITL names are dropped, hard


# ── profile presets (ADR 0075 D3) ──


def _cfg_profile(profile, tools=()):
    c = _cfg(list(tools))
    c.operator_mcp_profile = profile
    return c


def test_profile_read_only_exposes_reads_not_writes():
    names = {t.name for t in operator_tools(_cfg_profile("read-only"))}
    assert "current_time" in names and "load_skill" in names  # reads/queries
    assert "web_search" in names
    # writes are absent (no memory_ingest / write_note in the read-only set)
    assert "memory_ingest" not in names and "write_note" not in names


def test_profile_full_is_wildcard(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def plugin_thing(x: str) -> str:
        """a plugin tool"""
        return x

    monkeypatch.setattr(STATE, "plugin_tools", [plugin_thing], raising=False)
    names = {t.name for t in operator_tools(_cfg_profile("full"))}
    assert "plugin_thing" in names and "calculator" in names  # everything
    assert "ask_human" not in names  # …still minus the HITL hard-exclusion


def test_profile_unions_with_explicit_names():
    # read-only + an explicitly-named write tool → both
    names = {t.name for t in operator_tools(_cfg_profile("read-only", tools=["show_component"]))}
    assert "current_time" in names and "show_component" in names


# ── fleet_diagnostics stays OUT of the curated profiles (#3170, ADR 0071) ─────────────
# Cross-member log/task inspection over a FOREIGN MCP client is a trust surface that needs
# its own security review before ANY operator-MCP exposure, so neither the read-only nor the
# full/"*" profile reaches it; only a deliberate by-name allowlist entry does.


def test_fleet_diagnostics_not_in_read_only_profile():
    names = {t.name for t in operator_tools(_cfg_profile("read-only"))}
    assert "fleet_diagnostics" not in names


def test_fleet_diagnostics_excluded_from_wildcard(monkeypatch):
    from tools.fleet_diagnostics import fleet_diagnostics

    # Inject it as a candidate (the operator-MCP path builds get_all_tools without a
    # graph_config, so the real tool is never built there — plugin_tools is how it could
    # otherwise reach the wildcard).
    monkeypatch.setattr(STATE, "plugin_tools", [fleet_diagnostics], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*"]))}
    assert "calculator" in names  # the wildcard still works generally
    assert "fleet_diagnostics" not in names  # …but not for this tool


def test_fleet_diagnostics_still_allowable_by_explicit_name(monkeypatch):
    from tools.fleet_diagnostics import fleet_diagnostics

    monkeypatch.setattr(STATE, "plugin_tools", [fleet_diagnostics], raising=False)
    names = {t.name for t in operator_tools(_cfg(["*", "fleet_diagnostics"]))}
    assert "fleet_diagnostics" in names  # naming it overrides the wildcard exclusion


def test_unknown_profile_falls_back_to_allowlist():
    names = {t.name for t in operator_tools(_cfg_profile("bogus", tools=["calculator"]))}
    assert names == {"calculator"}  # unknown profile ignored, explicit names honored


def test_env_trust_full_overrides_deny_default(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_MCP_TRUST", "full")
    names = {t.name for t in operator_tools(_cfg([]))}  # empty allowlist would be deny-all
    assert "calculator" in names and "current_time" in names  # env forces full
    assert "ask_human" not in names  # HITL still hard-excluded


def test_allowlist_exposes_create_watch_when_watches_enabled(monkeypatch):
    """operator_tools must pass ``watches_enabled`` through like ``goal_enabled`` (#3248):
    before this, the watch tools could never bind here, so naming ``create_watch`` in the
    allowlist silently exposed nothing. They also need a registered verifier, exactly as
    the goal tools do."""
    import graph.goals.verifiers as verifiers

    monkeypatch.setattr(verifiers, "_PLUGIN_VERIFIERS", {"test:check": object()})

    on = _cfg(["create_watch", "list_watches"])
    on.watches_enabled = True
    assert {t.name for t in operator_tools(on)} == {"create_watch", "list_watches"}

    off = _cfg(["create_watch", "list_watches"])
    off.watches_enabled = False
    assert operator_tools(off) == []


# ── the ACP sidecar's exposed set, derived once for both sides (#3248) ────────────────


def test_acp_operator_allowlist_defaults_to_star():
    from runtime.operator_mcp_tools import acp_operator_allowlist

    assert acp_operator_allowlist(_cfg([])) == ["*"]
    assert acp_operator_allowlist(_cfg(["calculator"])) == ["calculator"]


def _served_by_a_sidecar(cfg, monkeypatch):
    """What server.operator_mcp would expose for *cfg*: it receives acp_operator_allowlist
    via OPERATOR_MCP_TOOLS and boots its own stores — a tasks store unconditionally."""
    from runtime.operator_mcp_tools import acp_operator_allowlist, operator_tools

    child = _cfg(acp_operator_allowlist(cfg))
    child.goal_enabled = cfg.goal_enabled
    child.watches_enabled = getattr(cfg, "watches_enabled", True)
    monkeypatch.setattr(STATE, "tasks_store", object(), raising=False)  # the sidecar's TaskStore()
    try:
        return {t.name for t in operator_tools(child)}
    finally:
        monkeypatch.setattr(STATE, "tasks_store", None, raising=False)  # back to the bare host


@pytest.mark.parametrize(
    ("tools", "goal_enabled"),
    [
        ([], False),  # (a) unset allowlist → the sidecar serves "*"
        (["task_list", "calculator"], False),  # (b) explicit allowlist naming a task tool
        ([], True),  # (c) goal flag flipped — the host must follow the same gate
    ],
)
def test_host_computes_exactly_what_the_sidecar_serves(monkeypatch, tools, goal_enabled):
    """sidecar_exposed_names() on a host with NO tasks store equals operator_tools() in a
    sidecar that always has one — the two sides can't drift (#3248 B1/B2)."""
    from runtime.operator_mcp_tools import sidecar_exposed_names

    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"test:check": object()})
    cfg = _cfg(tools)
    cfg.goal_enabled = goal_enabled
    served = _served_by_a_sidecar(cfg, monkeypatch)
    assert STATE.tasks_store is None  # the host really has none
    host = set(sidecar_exposed_names(cfg))
    assert host == served
    assert "task_list" in host  # the sidecar's unconditional tasks store, mirrored
    assert ("set_goal" in host) is goal_enabled


def test_sidecar_set_honors_the_trust_override(monkeypatch):
    from runtime.operator_mcp_tools import sidecar_exposed_names

    monkeypatch.setenv("PROTOAGENT_MCP_TRUST", "full")
    names = set(sidecar_exposed_names(_cfg(["calculator"])))
    assert {"calculator", "current_time", "task_list"} <= names  # "*", not the one name


def test_acp_operator_allowlist_strips_names():
    from runtime.operator_mcp_tools import acp_operator_allowlist

    assert acp_operator_allowlist(_cfg([" calculator ", "", "  "])) == ["calculator"]


def test_boot_stores_applies_registries_and_denylist(tmp_path, monkeypatch):
    """_boot_stores_only must apply the bundle's registries (verifier-gated tools were
    served by the host derivation but NOT by a real sidecar, whose registry stayed empty)
    and the fork tool denylist (a standalone sidecar served tools.disabled names)."""
    import types

    import server.agent_init as ai
    import tools.lg_tools as lg
    from server.operator_mcp import _boot_stores_only

    monkeypatch.setattr(ai, "_build_knowledge_store", lambda c: None)
    monkeypatch.setattr(ai, "_build_scheduler", lambda c: None)
    monkeypatch.setattr(ai, "_build_inbox_store", lambda c: None)
    monkeypatch.setattr(ai, "_apply_plugin_knowledge_backend", lambda c, ks, p: ks)
    monkeypatch.setattr(ai, "_build_skills_index", lambda c, extra_skill_dirs=None: None)
    bundle = types.SimpleNamespace(tools=[], skill_dirs=[], meta={})
    monkeypatch.setattr(ai, "_build_plugins", lambda config, existing_tools=None: bundle)
    applied = []
    monkeypatch.setattr(ai, "_apply_plugin_registries", lambda plugins: applied.append(plugins))
    monkeypatch.setattr(STATE, "tasks_store", object(), raising=False)
    monkeypatch.setattr(lg, "_disabled_tools", set())

    cfg = _cfg([])
    cfg.tools_disabled = ["calculator", "web_search"]
    cfg.tools_hidden = ["web_search", "execute_code"]
    _boot_stores_only(cfg)

    assert applied == [bundle]  # the same registration full init runs (#1752 semantics)
    assert lg._disabled_tools == {"calculator", "web_search", "execute_code"}  # hidden ⊂ denied


@pytest.mark.parametrize(("goal_enabled", "watches_enabled"), [(True, True), (True, False), (False, True)])
def test_host_and_sidecar_agree_with_registries_and_denylist(monkeypatch, goal_enabled, watches_enabled):
    """Registries applied the way _boot_stores_only now applies them + a denylisted tool:
    the host derivation equals the sidecar's served set, verifier-gated tools included,
    and the disabled name is absent on BOTH sides even under the "*" allowlist."""
    import types

    import server.agent_init as ai
    import tools.lg_tools as lg
    from graph.goals import verifiers
    from runtime.operator_mcp_tools import sidecar_exposed_names

    monkeypatch.setattr(verifiers, "_PLUGIN_VERIFIERS", {})
    monkeypatch.setattr(verifiers, "_PLUGIN_VERIFIER_META", {})
    ai._apply_plugin_registries(
        types.SimpleNamespace(
            goal_verifiers={"p:v": object()}, goal_verifier_meta=None,
            goal_hooks={}, watch_hooks={}, lifecycle_hooks={},
        )
    )
    monkeypatch.setattr(lg, "_disabled_tools", {"calculator"})

    cfg = _cfg([])
    cfg.goal_enabled = goal_enabled
    cfg.watches_enabled = watches_enabled
    served = _served_by_a_sidecar(cfg, monkeypatch)
    host = set(sidecar_exposed_names(cfg))
    assert host == served
    assert "calculator" not in host  # denylist holds on the bus, "*" notwithstanding
    assert ("set_goal" in host) is goal_enabled
    assert ("create_watch" in host) is watches_enabled


def test_registryless_sidecar_serves_no_verifier_gated_tools(monkeypatch):
    """With an EMPTY verifier registry (the pre-fix standalone sidecar) neither side may
    claim the goal/watch tools — the drift the re-check probe demonstrated."""
    import tools.lg_tools as lg
    from graph.goals import verifiers
    from runtime.operator_mcp_tools import sidecar_exposed_names

    monkeypatch.setattr(verifiers, "_PLUGIN_VERIFIERS", {})
    monkeypatch.setattr(lg, "_disabled_tools", set())
    cfg = _cfg([])
    cfg.goal_enabled = True
    cfg.watches_enabled = True
    served = _served_by_a_sidecar(cfg, monkeypatch)
    host = set(sidecar_exposed_names(cfg))
    assert host == served
    for gated in ("set_goal", "update_goal_plan", "abandon_goal", "create_watch", "list_watches"):
        assert gated not in host
