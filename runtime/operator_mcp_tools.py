"""Operator-MCP tool resolution — the allowlist/profile logic, in a neutral home.

This is the shared spine (ADR 0075 D2) for "which of THIS agent's tools does the
operator MCP expose for a given config": the ``operator_mcp_tools`` allowlist, the
curated profiles (``read-only`` / ``full``), the ``PROTOAGENT_MCP_TRUST`` override, and
the two never-expose sets. It lives in ``runtime/`` — an infra package that must never
import ``server`` / ``operator_api`` — so *both* the sidecar (``server.operator_mcp``,
which wraps these as a FastMCP server) and the operator HTTP surface (``operator_api.
mcp_routes``, which surfaces the exposed set at ``GET /api/mcp/exposed``) can import it
without tripping the import-layering contract. The FastMCP wrapping + stores boot stay
in ``server.operator_mcp``; only the pure resolution moved here.

Reads ``runtime.state.STATE`` for the booted stores + plugin tools; in the live server
those are populated by ``server.agent_init``, in a standalone sidecar by
``server.operator_mcp._boot_stores_only``.
"""

from __future__ import annotations

import logging
import os

from runtime.state import STATE

log = logging.getLogger(__name__)

# Tools "*" skips — a coding-agent brain already has its own code execution / file tools,
# so exposing protoAgent's execute_code over the bus is redundant. Not a security gate
# (you can still allowlist it by name); just avoids handing it a tool it already has.
_STAR_EXCLUDE = {"execute_code"}

# NEVER exposed over MCP, even when named explicitly — ask_human / request_user_input are
# HITL tools that pause the turn via a LangGraph ``interrupt`` only the lead-turn runner
# resumes. Called over a foreign stdio/HTTP MCP client (Claude Desktop, Cursor) there's no
# runner to resume them, so they HANG the client (ADR 0075 D3 — a real bug, not a gate).
_MCP_INCOMPATIBLE = {"ask_human", "request_user_input"}

# Curated profile presets over the allowlist (ADR 0075 D3). A profile is just a preset set
# of names layered on ``operator_mcp_tools`` — unset keeps deny-by-default (a foreign client
# gets only what you name). ``read-only`` is a stable, principled set (reads/queries, no state
# change). ``full`` = ``"*"``. The middle tier ``safe-operator`` (read + non-destructive
# writes) lands with the ops layer (ADR 0075 D2), which carries per-op read/write metadata so
# it's principled rather than a hand-maintained list — so it's deliberately NOT hardcoded here.
_READ_ONLY_TOOLS = frozenset(
    {
        "current_time", "calculator", "web_search", "fetch_url", "load_skill",
        "search_tools", "list_skills", "recent_activity", "list_agents",
        "memory_recall", "recall_session", "memory_list", "memory_stats",
        "list_schedules", "check_inbox", "task_list", "list_watches", "read_note",
    }
)


def _profile_allow(profile: str) -> set[str] | None:
    """A profile name → its allowlist set (or ``{"*"}`` for full), or ``None`` when the
    profile is unset/unknown so the caller falls back to the explicit tools list."""
    p = (profile or "").strip().lower().replace("_", "-")
    if p in ("", "custom", "none"):
        return None
    if p in ("full", "all"):
        return {"*"}
    if p in ("read-only", "readonly"):
        return set(_READ_ONLY_TOOLS)
    log.warning("[operator-mcp] unknown profile %r — falling back to the tools allowlist", profile)
    return None


def resolve_allow(config) -> set[str]:
    """The effective allowlist: ``PROTOAGENT_MCP_TRUST=full`` env override > the profile
    (unioned with any explicit names) > the explicit ``operator_mcp_tools`` list."""
    if os.environ.get("PROTOAGENT_MCP_TRUST", "").strip().lower() == "full":
        return {"*"}
    allow = set(getattr(config, "operator_mcp_tools", []) or [])
    prof = _profile_allow(getattr(config, "operator_mcp_profile", ""))
    if prof is not None:
        allow |= prof
    return allow


def operator_tools(config):
    """The allowlisted tools (core + plugin) to expose — empty allowlist ⇒ none."""
    from tools.lg_tools import get_all_tools

    allow = resolve_allow(config)
    if not allow:
        return []
    tools = list(
        get_all_tools(
            STATE.knowledge_store,
            scheduler=STATE.scheduler,
            inbox_store=STATE.inbox_store,
            tasks_store=STATE.tasks_store,
            goal_enabled=bool(getattr(config, "goal_enabled", False)),
        )
    )
    tools += list(getattr(STATE, "plugin_tools", None) or [])
    # "*" = expose everything (minus a small danger set you must opt into by name) — so you
    # don't have to enumerate every tool. List specific names instead for tight control.
    star = "*" in allow
    seen: set[str] = set()
    out = []
    for t in tools:
        name = getattr(t, "name", None)
        if not name or name in seen:
            continue
        if name in _MCP_INCOMPATIBLE:  # HANGS a foreign client — never expose, even by name
            continue
        if (name in allow) or (star and name not in _STAR_EXCLUDE):
            seen.add(name)
            out.append(t)
    return out


def resolve_exposed_names(config) -> list[str]:
    """The tool names the operator MCP would expose for *config* — powers the
    ``GET /api/mcp/exposed`` discovery route (the exposed set was previously
    introspectable only by reading logs). Requires the stores to be booted."""
    return [t.name for t in operator_tools(config)]
