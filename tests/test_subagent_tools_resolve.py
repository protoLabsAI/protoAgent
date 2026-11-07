"""ADR 0125 P2-D2a: the built-in subagents name only tools that still exist.

Subagent tool allowlists SKIP unknown names silently (``graph.agent`` builds the
map and drops anything it can't resolve), so a tool RENAME quietly strips the tool
from every subagent that still names the old spelling — exactly how the P2-D1
``memory_recall``/``memory_ingest``/``forget_memory``/``memory_stats`` rename could
have left the researcher and ``dream`` without their memory tools.

These tests build the default-build tool universe and assert nothing a built-in
subagent names falls outside it, so a future rename that forgets a subagent fails
here instead of silently degrading a live delegation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY
from knowledge.store import KnowledgeStore
from tools.fs_tools import build_fs_tools
from tools.lg_tools import get_all_tools

pytestmark = pytest.mark.platform_sensitive

# The retired spellings P2-D1 removed. A built-in that still names one has lost the
# tool (unknown names are dropped at build time), so they must appear nowhere in the
# subagent config or the self-improvement allowlist.
RETIRED_TOOL_NAMES = ("memory_ingest", "memory_recall", "forget_memory", "memory_stats")

# ``github_*`` are an OPTIONAL external plugin, not part of a default build: the
# code-review / verify roles name them and ``graph.agent`` binds them only when the
# github plugin is installed, skipping them otherwise (documented in config.py). They
# are therefore legitimately absent from the core universe — the one allowed exemption.
OPTIONAL_PLUGIN_TOOLS = frozenset(
    {
        "github_pr_diff",
        "github_get_commit_diff",
        "github_read_pr_file",
        "github_read_file",
        "github_get_pr",
        "github_repo_contents",
    }
)


def _default_build_tool_names(tmp_path) -> set[str]:
    """Every tool name a default agent build resolves: the core toolset plus the
    fenced filesystem tools (which need a registered project to bind)."""

    class _Truthy:  # builders only need a non-None object; methods are call-time.
        pass

    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    core = {
        t.name
        for t in get_all_tools(
            knowledge_store=ks,
            scheduler=_Truthy(),
            inbox_store=None,
            tasks_store=_Truthy(),
            goal_enabled=True,
            watches_enabled=True,
            soul_edit_enabled=True,
            self_config_enabled=True,
            skill_edit_enabled=True,
        )
    }
    fs_config = LangGraphConfig()
    fs_config.filesystem_projects = [{"name": "p", "path": str(tmp_path)}]
    fs = {t.name for t in build_fs_tools(fs_config)}
    return core | fs


def test_every_builtin_subagent_tool_resolves(tmp_path):
    """r1: for every built-in subagent, every name in its tools list resolves to a
    registered tool in a default build (bar the documented optional github plugin)."""
    universe = _default_build_tool_names(tmp_path) | OPTIONAL_PLUGIN_TOOLS
    dropped = {
        name: [t for t in cfg.tools if t not in universe]
        for name, cfg in SUBAGENT_REGISTRY.items()
    }
    offenders = {name: miss for name, miss in dropped.items() if miss}
    assert not offenders, f"subagents name tools absent from a default build: {offenders}"


def test_optional_plugin_exemption_is_exactly_the_github_names_used():
    """Keep the exemption honest: it must cover exactly the github_* names the
    built-ins reference — no stale entry masking a drop, no unlisted github tool."""
    used_github = {t for cfg in SUBAGENT_REGISTRY.values() for t in cfg.tools if t.startswith("github_")}
    assert used_github == OPTIONAL_PLUGIN_TOOLS


def test_no_retired_memory_tool_names_in_subagents_or_self_improvement():
    """r2: the renamed-away spellings appear in neither source file."""
    src = Path(__file__).resolve().parents[1] / "src" / "graph"
    for rel in ("subagents/config.py", "self_improvement.py"):
        text = (src / rel).read_text(encoding="utf-8")
        for retired in RETIRED_TOOL_NAMES:
            # word-boundary so a longer live name (e.g. ``memory_search``) never matches.
            assert not re.search(rf"\b{retired}\b", text), f"{rel} still references retired tool {retired!r}"


def test_researcher_has_memory_and_knowledge_tools():
    """r3: researcher keeps memory_search + memory_save, and gains knowledge_search so
    it still reaches the operator's ingested reference docs after the memory/knowledge split."""
    tools = SUBAGENT_REGISTRY["researcher"].tools
    assert {"memory_search", "memory_save", "knowledge_search"} <= set(tools)


def test_dream_has_the_full_consolidate_then_forget_toolset():
    """r3: dream keeps memory_search + memory_save and also memory_forget for its
    consolidate-then-prune pass."""
    tools = SUBAGENT_REGISTRY["dream"].tools
    assert {"memory_search", "memory_save", "memory_forget"} <= set(tools)
