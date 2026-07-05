"""Operator tools as an MCP server (ADR 0033, slice 1).

Exposes THIS agent's tool registry — core tools + plugin ``register_tools`` tools —
as a single MCP server, so any MCP client (Claude Desktop, Cursor) or an ACP
coding-agent runtime (mounted via ``session/new`` ``mcpServers``) can *operate* the
instance: read/write notes & tasks, recall/ingest memory, run workflows, delegate to
subagents, set goals, schedule work — whatever you allowlist.

Design (ADR 0033 D3 + D5):

- **Allowlist-gated resolver, ACP-brain defaults to all.** ``operator_tools`` exposes only
  the tools named in its allowlist (empty → nothing) — so a *foreign* MCP client (Claude
  Desktop, Cursor) gets nothing it wasn't granted. But when the operator MCP is the BRAIN's
  own tool bridge (``agent_runtime: acp:<agent>``), ``operator_mcp_server_spec`` defaults the
  allowlist to ``"*"`` — full parity with the native runtime, where the model has every tool.
  ``operator_mcp.tools`` then only *restricts* the ACP brain. (``execute_code`` is dropped
  from ``"*"`` — the coding agent already has its own; allowlist it by name to override.)
- **Core + plugin, uniformly.** Plugin tools live in the same registry, so they ride
  this one bridge for free — no per-plugin MCP. (Plugins that *are* an MCP server, via
  ``register_mcp_server``, are mounted directly by the client, not re-wrapped here.)
- **Stores-only boot.** A standalone sidecar must NOT start the agent's background loops
  (checkpoint prune / monitor-goals) against shared data, so we build just the stores +
  plugins the tools bind to — never the graph or the loops.

Run it::

    python -m server.operator_mcp            # stdio (for an MCP client / ACP session)
    python -m server.operator_mcp --http --port 8848
"""

from __future__ import annotations

import argparse
import logging
import os

from runtime.state import STATE

log = logging.getLogger(__name__)


def _boot_stores_only(config):
    """Wire just the stores + plugins the tools need — no graph, no background loops.

    Safe to run as a sidecar against the same data dir as a live instance (reads/writes
    the same notes/tasks/knowledge — that's the point); WAL + busy_timeout cover concurrency.
    """
    import server.agent_init as ai
    from tools.lg_tools import get_all_tools

    STATE.graph_config = config
    STATE.knowledge_store = ai._build_knowledge_store(config)
    STATE.scheduler = ai._build_scheduler(config)
    STATE.inbox_store = ai._build_inbox_store(config)
    if STATE.tasks_store is None:
        from tasks import TaskStore

        STATE.tasks_store = TaskStore()

    plugins = ai._build_plugins(
        config,
        existing_tools=get_all_tools(
            STATE.knowledge_store,
            scheduler=STATE.scheduler,
            goal_enabled=getattr(config, "goal_enabled", True),
        ),
    )
    STATE.plugin_tools = plugins.tools
    STATE.plugin_tool_owner = getattr(plugins, "tool_plugins", {}) or {}
    STATE.plugin_skill_dirs = plugins.skill_dirs
    STATE.plugin_meta = plugins.meta
    STATE.knowledge_store = ai._apply_plugin_knowledge_backend(config, STATE.knowledge_store, plugins)
    # Build the skills index too (mirrors agent_init) — load_skill / list_skills /
    # save_skill read STATE.skills_index, which is None in a fresh sidecar process.
    # Without this, an ACP agent calling load_skill through this server got
    # "Skills index is not available." even though the prompt's <available_skills>
    # block (built in the host process) listed the skill.
    STATE.skills_index = ai._build_skills_index(config, extra_skill_dirs=plugins.skill_dirs)


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


def build_server(config, *, name: str = "protoAgent-operator"):
    """Return (FastMCP server, [exposed tool names]) for the allowlisted tools."""
    from langchain_mcp_adapters.tools import to_fastmcp
    from mcp.server.fastmcp import FastMCP

    fast_tools, exposed = [], []
    for t in operator_tools(config):
        try:
            fast_tools.append(to_fastmcp(t))
            exposed.append(t.name)
        except Exception:  # noqa: BLE001 — one bad tool shouldn't sink the server
            log.exception("[operator-mcp] could not expose %s", getattr(t, "name", "?"))
    server = FastMCP(name, tools=fast_tools)
    log.info(
        "[operator-mcp] exposing %d tool(s): %s",
        len(exposed),
        ", ".join(exposed) or "(none — set operator_mcp.tools)",
    )
    return server, exposed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve protoAgent's operator tools over MCP")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path

    config = LangGraphConfig.from_yaml(config_yaml_path())
    # An explicit allowlist from the caller (e.g. the ACP runtime spawning this) overrides
    # the YAML — so the exposed set matches the runtime's intent, not whatever's on disk.
    env_tools = os.environ.get("OPERATOR_MCP_TOOLS")
    if env_tools is not None:
        config.operator_mcp_tools = [t.strip() for t in env_tools.split(",") if t.strip()]
    _boot_stores_only(config)
    server, exposed = build_server(config)
    if not exposed:
        log.warning("[operator-mcp] no tools exposed — add names to operator_mcp.tools in config")
    if args.http:
        server.settings.host, server.settings.port = args.host, args.port
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
