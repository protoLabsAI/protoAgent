"""Tests for deferred-tool progressive disclosure (ADR 0005 #3).

Covers the search_tools meta-tool (matching/listing) and the
ToolDeferralMiddleware (trimming the per-call tool set to base + loaded),
plus the config round-trip. No live model — the middleware is exercised with
a fake ModelRequest, mirroring how langchain calls wrap_model_call.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from graph.config import LangGraphConfig
from graph.middleware.tool_deferral import (
    ToolDeferralMiddleware,
    activated_tool_names,
)
from tools.lg_tools import (
    DEFERRED_BASE_TOOL_NAMES,
    SEARCH_TOOLS_NAME,
    build_search_tools_tool,
    resolve_deferred_keep,
)


def _tool(name, desc):
    return SimpleNamespace(name=name, description=desc)


def _catalog():
    # A base tool (kept) + three deferred tools.
    return [
        _tool("web_search", "Search the web with DuckDuckGo."),
        _tool("github_get_pr", "Fetch a GitHub pull request by number."),
        _tool("notes_read", "Read a tab from the operator Notes panel."),
        _tool("schedule_task", "Schedule a future reminder or job."),
    ]


# ── resolve_deferred_keep ─────────────────────────────────────────────────────


def test_resolve_keep_defaults_to_builtin_base() -> None:
    keep = resolve_deferred_keep([])
    assert keep == set(DEFERRED_BASE_TOOL_NAMES)
    assert SEARCH_TOOLS_NAME in keep


def test_resolve_keep_override_always_includes_search() -> None:
    # A custom keep list that forgets search_tools still gets it (bootstrap).
    keep = resolve_deferred_keep(["web_search"])
    assert keep == {"web_search", SEARCH_TOOLS_NAME}


# ── search_tools meta-tool ────────────────────────────────────────────────────


def test_search_tools_matches_by_keyword() -> None:
    st = build_search_tools_tool(_catalog(), {"web_search", SEARCH_TOOLS_NAME})
    out = st.invoke({"query": "github pull request"})
    assert "`github_get_pr`" in out
    assert "`notes_read`" not in out


def test_search_tools_excludes_kept_tools() -> None:
    # web_search is in keep → never offered by search_tools (it's already shown).
    st = build_search_tools_tool(_catalog(), {"web_search", SEARCH_TOOLS_NAME})
    out = st.invoke({"query": "search"})
    assert "`web_search`" not in out


def test_search_tools_empty_query_lists_all_deferred() -> None:
    st = build_search_tools_tool(_catalog(), {"web_search", SEARCH_TOOLS_NAME})
    out = st.invoke({"query": ""})
    for name in ("github_get_pr", "notes_read", "schedule_task"):
        assert f"`{name}`" in out


def test_search_tools_no_match_falls_back_to_listing() -> None:
    st = build_search_tools_tool(_catalog(), {"web_search", SEARCH_TOOLS_NAME})
    out = st.invoke({"query": "xyzzy-nonexistent"})
    assert "No tool matched" in out
    assert "`github_get_pr`" in out  # still callable via the fallback listing


# ── activated_tool_names ──────────────────────────────────────────────────────


def test_activated_reads_backticked_names_from_search_results() -> None:
    msgs = [
        HumanMessage(content="do a thing"),
        ToolMessage(
            content="Found 1 tool(s) — now available to call:\n- `github_get_pr` — Fetch a PR.",
            name=SEARCH_TOOLS_NAME,
            tool_call_id="t1",
        ),
    ]
    assert activated_tool_names(msgs) == {"github_get_pr"}


def test_activated_ignores_non_search_tool_messages() -> None:
    msgs = [ToolMessage(content="result with a `backtick`", name="github_get_pr", tool_call_id="t2")]
    assert activated_tool_names(msgs) == set()


# ── ToolDeferralMiddleware ────────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, tools, messages):
        self.tools = tools
        self.state = {"messages": messages}

    def override(self, **kw):
        self.tools = kw.get("tools", self.tools)
        return self


def _names(tools):
    return [t.name for t in tools]


def test_middleware_trims_to_base_when_nothing_loaded() -> None:
    mw = ToolDeferralMiddleware({"web_search", SEARCH_TOOLS_NAME})
    tools = _catalog() + [_tool(SEARCH_TOOLS_NAME, "Find tools.")]
    req = _FakeRequest(tools, [HumanMessage(content="hi")])
    out = mw.wrap_model_call(req, lambda r: r)
    assert set(_names(out.tools)) == {"web_search", SEARCH_TOOLS_NAME}


def test_middleware_exposes_loaded_tools() -> None:
    mw = ToolDeferralMiddleware({"web_search", SEARCH_TOOLS_NAME})
    tools = _catalog() + [_tool(SEARCH_TOOLS_NAME, "Find tools.")]
    msgs = [
        HumanMessage(content="get the PR"),
        ToolMessage(content="- `github_get_pr` — Fetch a PR.", name=SEARCH_TOOLS_NAME, tool_call_id="t1"),
    ]
    req = _FakeRequest(tools, msgs)
    out = mw.wrap_model_call(req, lambda r: r)
    assert set(_names(out.tools)) == {"web_search", SEARCH_TOOLS_NAME, "github_get_pr"}


def test_middleware_noop_when_all_tools_are_base() -> None:
    # Nothing to defer → request is returned untouched (same object).
    mw = ToolDeferralMiddleware({"web_search", SEARCH_TOOLS_NAME})
    tools = [_tool("web_search", "x"), _tool(SEARCH_TOOLS_NAME, "y")]
    req = _FakeRequest(tools, [])
    out = mw.wrap_model_call(req, lambda r: r)
    assert _names(out.tools) == ["web_search", SEARCH_TOOLS_NAME]


# ── config round-trip ─────────────────────────────────────────────────────────


def test_config_parses_deferred_tools(tmp_path) -> None:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("tools:\n  deferred:\n    enabled: true\n    keep: [web_search, current_time]\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.tools_deferred_enabled is True
    assert cfg.tools_deferred_keep == ["web_search", "current_time"]


def test_config_deferred_default_off() -> None:
    assert LangGraphConfig().tools_deferred_enabled is False
