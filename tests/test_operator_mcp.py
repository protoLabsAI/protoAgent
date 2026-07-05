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
        lambda config, existing_tools=None: types.SimpleNamespace(tools=[], skill_dirs=[], meta={}),
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


def test_unknown_profile_falls_back_to_allowlist():
    names = {t.name for t in operator_tools(_cfg_profile("bogus", tools=["calculator"]))}
    assert names == {"calculator"}  # unknown profile ignored, explicit names honored


def test_env_trust_full_overrides_deny_default(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_MCP_TRUST", "full")
    names = {t.name for t in operator_tools(_cfg([]))}  # empty allowlist would be deny-all
    assert "calculator" in names and "current_time" in names  # env forces full
    assert "ask_human" not in names  # HITL still hard-excluded
