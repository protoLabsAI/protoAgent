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
#
# ``fleet_diagnostics`` (#3170, ADR 0071) is kept out of the wildcard too, but for a
# different reason: cross-member log/task inspection over a FOREIGN MCP client is a trust
# surface that needs its own security review before ANY operator-MCP exposure. Keeping it
# out of ``*`` (and out of the ``read-only`` profile below) means neither curated profile
# reaches it by default; a deliberate by-name allowlist entry remains the only way in,
# pending that review.
_STAR_EXCLUDE = {"execute_code", "fleet_diagnostics"}

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


def resolve_allow(config, *, tools: list[str] | None = None) -> set[str]:
    """The effective allowlist: ``PROTOAGENT_MCP_TRUST=full`` env override > the profile
    (unioned with any explicit names) > the explicit ``operator_mcp_tools`` list.

    ``tools`` substitutes for ``config.operator_mcp_tools`` — the sidecar spawned by the
    ACP runtime receives its list via ``OPERATOR_MCP_TOOLS`` and applies exactly this
    resolution to it, so a host computing "what does that sidecar serve?" passes the same
    list here (:func:`acp_operator_allowlist`) and gets the same answer.
    """
    if os.environ.get("PROTOAGENT_MCP_TRUST", "").strip().lower() == "full":
        return {"*"}
    source = tools if tools is not None else (getattr(config, "operator_mcp_tools", []) or [])
    allow = set(source)
    prof = _profile_allow(getattr(config, "operator_mcp_profile", ""))
    if prof is not None:
        allow |= prof
    return allow


def acp_operator_allowlist(config) -> list[str]:
    """The allowlist an ACP-spawned operator MCP is handed: the configured names, or ``"*"``
    when unset — under an ACP runtime the coding agent IS the brain and gets the full
    toolset by default (parity with the native runtime), ``operator_mcp.tools`` being an
    optional *restriction*, never a requirement. ONE derivation, used by the spawn spec
    (``runtime.acp_runtime.operator_mcp_server_spec``) AND by the host-side prefix/persona
    (:func:`sidecar_exposed_names`) so the two can never drift (#3248)."""
    # Strip each name: the sidecar strips when parsing OPERATOR_MCP_TOOLS, so a padded
    # YAML entry (" calculator ") must not make the host match a literal the child never sees.
    configured = [str(t).strip() for t in (getattr(config, "operator_mcp_tools", None) or []) if str(t).strip()]
    return configured or ["*"]


class _SidecarTasksStorePresent:
    """Stand-in for the tasks store the sidecar ALWAYS boots (``server.operator_mcp.
    _boot_stores_only`` creates a ``TaskStore`` unconditionally). The task tools only close
    over their store at build time, so a placeholder is enough to learn their NAMES here —
    the host never opens the sidecar's DB just to compute a prefix."""


def _exposed_tools(config, allow: set[str], *, knowledge_store, scheduler, inbox_store, tasks_store, plugin_tools):
    from tools.lg_tools import drop_disabled_tools, get_all_tools

    if not allow:
        return []
    tools = list(
        get_all_tools(
            knowledge_store,
            scheduler=scheduler,
            inbox_store=inbox_store,
            tasks_store=tasks_store,
            goal_enabled=bool(getattr(config, "goal_enabled", False)),
            # Mirror goal_enabled: without this the watch tools could never bind here, so an
            # allowlist naming create_watch silently exposed nothing (#3248).
            watches_enabled=bool(getattr(config, "watches_enabled", False)),
        )
    )
    tools += list(plugin_tools or [])
    # The fork tool denylist (tools.disabled/hidden) is applied over the ASSEMBLED set in
    # graph.agent — get_all_tools doesn't filter it — so the bus must filter here too or a
    # disabled tool stays callable over MCP even though it never binds to the graph (#3248).
    tools = drop_disabled_tools(tools)
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


def operator_tools(config, *, allow: set[str] | None = None):
    """The allowlisted tools (core + plugin) to expose — empty allowlist ⇒ none.

    ``allow`` overrides :func:`resolve_allow` for callers that already resolved the
    effective set (the ACP host computing what its sidecar serves)."""
    return _exposed_tools(
        config,
        resolve_allow(config) if allow is None else set(allow),
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        inbox_store=STATE.inbox_store,
        tasks_store=STATE.tasks_store,
        plugin_tools=getattr(STATE, "plugin_tools", None),
    )


def resolve_exposed_names(config, *, allow: set[str] | None = None) -> list[str]:
    """The tool names the operator MCP would expose for *config* — powers the
    ``GET /api/mcp/exposed`` discovery route (the exposed set was previously
    introspectable only by reading logs). Requires the stores to be booted."""
    return [t.name for t in operator_tools(config, allow=allow)]


def sidecar_exposed_names(config, *, allow: set[str] | None = None) -> list[str]:
    """The tool names the ACP-spawned sidecar (``server.operator_mcp``) serves for *config*,
    computed on the host under the SIDECAR's boot assumptions rather than the host's — so
    the honest prefix (``runtime.context.ContextAssembler``) and the brain's persona describe
    exactly the bus the brain talks to (#3248).

    The sidecar rebuilds its stores from the same config the host booted its own from, so
    presence matches for knowledge / scheduler / inbox and the plugin tools come from the
    same loader — with ONE structural difference mirrored here: it creates a tasks store
    unconditionally, so the task tools are always served. ``allow`` defaults to what the
    spawn spec hands the sidecar (:func:`acp_operator_allowlist`) resolved exactly as the
    sidecar resolves it (:func:`resolve_allow`, incl. the ``PROTOAGENT_MCP_TRUST`` override
    the spec forwards). Requires the host stores to be booted (a real turn), never guesses.
    """
    if allow is None:
        allow = resolve_allow(config, tools=acp_operator_allowlist(config))
    tasks_store = STATE.tasks_store if getattr(STATE, "tasks_store", None) is not None else _SidecarTasksStorePresent()
    return [
        t.name
        for t in _exposed_tools(
            config,
            set(allow),
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            inbox_store=STATE.inbox_store,
            tasks_store=tasks_store,
            plugin_tools=getattr(STATE, "plugin_tools", None),
        )
    ]
