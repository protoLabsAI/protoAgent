"""ADR 0125 P2-D2c — the operator MCP allowlist, ACP runtime routing rules and console
tool labels all use the renamed memory/knowledge tools (memory_save / memory_search /
memory_list / memory_forget + knowledge_search), with no trace of the pre-rename names.

The D3 hard rename (memory_ingest→memory_save, memory_recall→memory_search,
forget_memory→memory_forget, memory_stats dropped, knowledge_search added) landed on the
tools themselves in P2-D1; this pins the three downstream consumers to the new vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive

# The names the rename retired — none of them may survive in any of the three consumer sites.
_OLD_NAMES = ("memory_recall", "memory_stats", "memory_ingest", "forget_memory")

_SOURCES = (
    Path("src/runtime/operator_mcp_tools.py"),
    Path("src/runtime/acp_runtime.py"),
    Path("src/operator_api/console_handlers.py"),
)


@pytest.fixture(autouse=True)
def _bare_state(monkeypatch):
    for attr in ("scheduler", "inbox_store", "tasks_store"):
        monkeypatch.setattr(STATE, attr, None, raising=False)
    monkeypatch.setattr(STATE, "plugin_tools", [], raising=False)
    # A stand-in knowledge store so get_all_tools binds the memory/knowledge cluster
    # (the tools close over it at build time, only touch it at invocation).
    monkeypatch.setattr(STATE, "knowledge_store", object(), raising=False)
    monkeypatch.setattr(STATE, "memory_store", None, raising=False)
    monkeypatch.setattr(STATE, "history_store", None, raising=False)


# ── r1 — the operator MCP exposed set ────────────────────────────────────────────────


def test_read_only_allowlist_names_the_renamed_memory_tools():
    from runtime.operator_mcp_tools import _READ_ONLY_TOOLS

    assert {"memory_search", "memory_list", "knowledge_search"} <= _READ_ONLY_TOOLS
    for old in _OLD_NAMES:
        assert old not in _READ_ONLY_TOOLS


def test_operator_mcp_exposes_renamed_memory_tools():
    """The read-only profile actually SERVES memory_search / memory_list / knowledge_search
    (reads), and never the retired names."""
    from runtime.operator_mcp_tools import operator_tools

    cfg = LangGraphConfig()
    cfg.operator_mcp_profile = "read-only"
    names = {t.name for t in operator_tools(cfg)}
    assert {"memory_search", "memory_list", "knowledge_search"} <= names
    for old in _OLD_NAMES:
        assert old not in names


# ── r2 — the ACP runtime routing rules ───────────────────────────────────────────────


def test_acp_rules_route_remember_and_lookup_to_renamed_tools():
    import runtime.acp_runtime as rt

    _what, persist = rt._tool_families_note({"memory_save", "knowledge_search", "calculator"})
    assert "remembering a fact → `memory_save`" in persist
    assert "looking up an ingested document → `knowledge_search`" in persist


def test_acp_rules_name_a_tool_only_when_it_is_exposed():
    import runtime.acp_runtime as rt

    # knowledge_search present but memory_save absent → only the lookup rule.
    _w1, p1 = rt._tool_families_note({"knowledge_search", "calculator"})
    assert "knowledge_search" in p1 and "memory_save" not in p1
    # neither present → neither rule (and no retired name ever).
    _w2, p2 = rt._tool_families_note({"calculator", "current_time"})
    assert "knowledge_search" not in p2 and "memory_save" not in p2
    for old in _OLD_NAMES:
        assert old not in p2


# ── r3 — the console tool→category labels ─────────────────────────────────────────────


def test_console_labels_map_the_renamed_memory_tools():
    from operator_api.console_handlers import _tool_category

    for name in ("memory_save", "memory_search", "memory_list", "memory_forget"):
        assert _tool_category(name, "core") == "Memory", name
    # knowledge_search follows the existing knowledge_ingest entry.
    assert _tool_category("knowledge_search", "core") == _tool_category("knowledge_ingest", "core")


def test_console_category_map_drops_the_retired_names():
    from operator_api.console_handlers import _TOOL_CATEGORY

    for old in _OLD_NAMES:
        assert old not in _TOOL_CATEGORY


# ── r3 (source guard) — no retired name survives in any of the three consumer files ───


def test_consumer_sources_contain_no_retired_tool_names():
    repo_root = Path(__file__).resolve().parents[1]
    for rel in _SOURCES:
        text = (repo_root / rel).read_text(encoding="utf-8")
        for old in _OLD_NAMES:
            assert old not in text, f"{rel} still references the retired name {old!r}"
