"""Removed memory tool names translate wherever a tool list is read (ADR 0125, #4555).

The stress test of v0.231.0 found designSystem's ``execute_code.tools`` still naming
``memory_recall``: the bridge dropped it with no log line, so scripts lost memory search.
The subagent shim (#4630) only covered subagent lists. These tests pin every other site,
plus the boot audit that names skills, subagents and SOUL text still using the old names.
"""

from __future__ import annotations

import json
import logging

import pytest
from langchain_core.tools import tool

import graph.tool_renames as tool_renames
from graph.config import LangGraphConfig
from graph.subagents.config import SubagentConfig

OLD_NEW = list(tool_renames.LEGACY_TOOL_RENAMES.items())


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch, caplog):
    monkeypatch.setattr(tool_renames, "_WARNED", set())
    monkeypatch.setattr(tool_renames, "_AUDITED", set())
    caplog.set_level(logging.WARNING)


def rename_warnings(caplog, needle="removed tool"):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# ── config parse: every tool-name field ───────────────────────────────────────


@pytest.mark.parametrize("old,new", OLD_NEW)
def test_config_tool_lists_translate_and_warn_once_per_site(old, new, caplog):
    data = {
        "tools": {
            "disabled": [old, "web_search"],
            "hidden": [old],
            "subagent_only": [old],
            "deferred": {"keep": [old, new]},
        },
        "operator_mcp": {"tools": [old]},
        "enforcement": {"disallowed_tools": [old], "rate_limits": {old: {"max_calls": 3}}},
    }
    for _ in range(2):  # a reload re-parses; the warnings stay one per site
        cfg = LangGraphConfig.from_dict(data)
        assert cfg.tools_disabled == [new, "web_search"]
        assert cfg.tools_hidden == [new]
        assert cfg.tools_subagent_only == [new]
        assert cfg.tools_deferred_keep == [new]
        assert cfg.operator_mcp_tools == [new]
        assert cfg.enforcement_disallowed_tools == [new]
        assert cfg.enforcement_rate_limits == {new: {"max_calls": 3}}
    warnings = rename_warnings(caplog)
    sources = [
        "tools.disabled",
        "tools.hidden",
        "tools.subagent_only",
        "tools.deferred.keep",
        "operator_mcp.tools",
        "enforcement.disallowed_tools",
        "enforcement.rate_limits",
    ]
    assert len(warnings) == len(sources)
    for source in sources:
        assert any(w.startswith(f"[tool-rename] {source} lists removed tool '{old}'; using '{new}'") for w in warnings)


def test_config_with_current_names_is_untouched(caplog):
    cfg = LangGraphConfig.from_dict({"tools": {"disabled": ["memory_search"]}, "enforcement": {"rate_limits": None}})
    assert cfg.tools_disabled == ["memory_search"]
    assert cfg.enforcement_rate_limits == {}
    assert not rename_warnings(caplog)


def test_rate_limit_new_key_wins_over_old():
    out = tool_renames.migrate_tool_keys(
        {"memory_search": {"max_calls": 1}, "memory_recall": {"max_calls": 9}}, source="enforcement.rate_limits"
    )
    assert out == {"memory_search": {"max_calls": 1}}


def test_disabled_old_name_drops_the_new_tool():
    from tools.lg_tools import drop_disabled_tools, set_disabled_tools

    @tool
    def memory_search(query: str) -> str:
        """Search memories."""
        return query

    @tool
    def memory_list() -> str:
        """List memories."""
        return ""

    cfg = LangGraphConfig.from_dict({"tools": {"disabled": ["memory_recall"]}})
    set_disabled_tools(cfg.tools_disabled)
    try:
        assert [t.name for t in drop_disabled_tools([memory_search, memory_list])] == ["memory_list"]
    finally:
        set_disabled_tools([])


# ── execute_code bridge allowlist ─────────────────────────────────────────────


@tool
async def memory_search(query: str) -> str:
    """Search saved memories."""
    return f"memory: {query}"


@tool
async def read_file(path: str) -> str:
    """Read a file."""
    return path


@pytest.mark.parametrize("old,new", OLD_NEW)
def test_execute_code_explicit_tools_translate(old, new, caplog):
    from plugins.execute_code import _effective_tools

    for _ in range(2):
        assert _effective_tools(["read_file", old], [], [read_file, memory_search]) == ["read_file", new]
    warnings = rename_warnings(caplog)
    assert warnings == [
        f"[tool-rename] execute_code.tools lists removed tool '{old}'; using '{new}'. Update the config or "
        "plugin that sets it (ADR 0125: the old memory tool names go away after #4555)."
    ]


def test_execute_code_extra_tools_translate():
    from plugins.execute_code import _effective_tools

    out = _effective_tools(["read_file"], ["memory_recall"], [read_file, memory_search])
    assert out == ["memory_search", "read_file"]


def test_execute_code_unregistered_explicit_name_warns_once(caplog):
    from plugins.execute_code import _effective_tools

    for _ in range(2):
        _effective_tools(["read_file", "not_a_tool"], [], [read_file])
    msgs = [r.getMessage() for r in caplog.records if "not_a_tool" in r.getMessage()]
    assert msgs == ["[execute_code] tools: 'not_a_tool' is not a registered tool on this agent; scripts can't call it."]


@pytest.mark.asyncio
async def test_execute_code_factory_bridges_memory_search_for_a_legacy_list():
    """The designSystem repro end to end: the plugin's own config names memory_recall."""
    from pathlib import Path

    from graph.plugins.registry import PluginRegistry
    from plugins.execute_code import register

    reg = PluginRegistry("execute_code", Path("."), config={"tools": ["read_file", "memory_recall"]})
    register(reg)
    ec = reg.late_tool_factories[0]([read_file, memory_search], LangGraphConfig())
    assert "tools.memory_search(query" in ec.description
    out = await ec.ainvoke({"code": "print(tools.memory_search(query='voice'))"})
    assert out.strip() == "memory: voice"


# ── operator MCP allowlist (config parse + the sidecar's env list) ────────────


def test_operator_mcp_allowlists_translate(monkeypatch):
    from runtime.operator_mcp_tools import acp_operator_allowlist, resolve_allow

    monkeypatch.delenv("PROTOAGENT_MCP_TRUST", raising=False)
    cfg = LangGraphConfig()
    cfg.operator_mcp_tools = ["memory_recall", "web_search"]  # the OPERATOR_MCP_TOOLS env path
    assert resolve_allow(cfg) == {"memory_search", "web_search"}
    assert resolve_allow(cfg, tools=["forget_memory"]) == {"memory_forget"}
    assert acp_operator_allowlist(cfg) == ["memory_search", "web_search"]


# ── stored tool fences (scheduled jobs, background jobs) ──────────────────────


@pytest.mark.parametrize("module", ["scheduler.local", "background.store"])
def test_stored_fences_translate(module):
    import importlib

    load = importlib.import_module(module)._load_fence
    assert load(json.dumps(["memory_recall", "memory_search", "web_search"])) == ["memory_search", "web_search"]
    assert load(json.dumps(["memory_stats"])) == ["memory_list"]


# ── boot audit: skills, subagents, SOUL ───────────────────────────────────────


def test_audit_finds_each_source_and_logs_once(tmp_path, caplog):
    skill = tmp_path / "job-hunt" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: job-hunt\ntools: [memory_recall]\n---\nUse `memory_recall` then memory_ingest.\n")
    clean = tmp_path / "clean" / "SKILL.md"
    clean.parent.mkdir()
    clean.write_text("---\nname: clean\n---\nUse memory_search. Not memory_recalled.\n")
    subs = [
        SubagentConfig(
            name="social", description="d", system_prompt="Call forget_memory when asked.", tools=["memory_stats"]
        ),
        SubagentConfig(name="fine", description="d", system_prompt="Call memory_search.", tools=["memory_search"]),
    ]
    findings = tool_renames.audit_legacy_tool_references(
        skill_files=[skill, clean, tmp_path / "missing" / "SKILL.md"], subagents=subs, soul="Save with memory_ingest."
    )
    assert findings == [
        {"source": f"skill {skill}", "names": ["memory_ingest", "memory_recall"]},
        {"source": "subagent 'social' prompt", "names": ["forget_memory"]},
        {"source": "subagent 'social' tools", "names": ["memory_stats"]},
        {"source": "SOUL.md", "names": ["memory_ingest"]},
    ]
    assert tool_renames.log_legacy_tool_references(findings) == 4
    assert tool_renames.log_legacy_tool_references(findings) == 0  # a reload re-audits quietly
    warnings = rename_warnings(caplog, "names removed tool")
    assert len(warnings) == 4
    assert warnings[0] == (
        f"[tool-rename] skill {skill} names removed tool(s) memory_ingest -> memory_save, "
        "memory_recall -> memory_search. The model can't call these; update it (ADR 0125, #4555)."
    )


def test_boot_audit_reads_plugin_skills_registry_and_soul(tmp_path, monkeypatch, caplog):
    from server import agent_init

    plugin_skills = tmp_path / "plugin-skills"
    (plugin_skills / "loop-retro").mkdir(parents=True)
    (plugin_skills / "loop-retro" / "SKILL.md").write_text("---\nname: loop-retro\n---\nmemory_recall first.\n")
    monkeypatch.setattr(agent_init, "_skill_roots", lambda config, extra=None: [*map(type(tmp_path), extra or [])])
    monkeypatch.setattr(
        "graph.subagents.config.SUBAGENT_REGISTRY",
        {"researcher": SubagentConfig(name="researcher", description="d", system_prompt="p", tools=["memory_recall"])},
    )
    monkeypatch.setattr("graph.config_io.read_soul", lambda: "I recall with memory_recall.")
    cfg = LangGraphConfig()
    for _ in range(2):
        agent_init._audit_legacy_tool_references(cfg, extra_skill_dirs=[str(plugin_skills)])
    warnings = rename_warnings(caplog, "names removed tool")
    assert len(warnings) == 3
    assert "loop-retro" in warnings[0] and "subagent 'researcher' tools" in warnings[1] and "SOUL.md" in warnings[2]


def test_boot_audit_never_raises(monkeypatch, caplog):
    from server import agent_init

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(agent_init, "_skill_roots", boom)
    assert agent_init._audit_legacy_tool_references(LangGraphConfig()) == 0
    assert "legacy tool-name audit failed" in caplog.text
