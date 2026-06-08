"""Operator tools as an MCP server (ADR 0033, slice 1).

Exposes THIS agent's tool registry — core tools + plugin ``register_tools`` tools —
as a single MCP server, so any MCP client (Claude Desktop, Cursor) or an ACP
coding-agent runtime (mounted via ``session/new`` ``mcpServers``) can *operate* the
instance: read/write notes & beads, recall/ingest memory, run workflows, delegate to
subagents, set goals, schedule work — whatever you allowlist.

Design (ADR 0033 D3 + D5):

- **Opt-in + allowlist-gated.** Only tools named in ``operator_mcp.tools`` are exposed
  (empty → nothing, mirroring how the comps default external brains to no framework
  tools). Don't hand ``execute_code`` etc. to an outside agent unless you mean to.
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
    the same notes/beads/knowledge — that's the point); WAL + busy_timeout cover concurrency.
    """
    import server.agent_init as ai
    from tools.lg_tools import get_all_tools

    STATE.graph_config = config
    STATE.knowledge_store = ai._build_knowledge_store(config)
    STATE.scheduler = ai._build_scheduler(config)
    STATE.inbox_store = ai._build_inbox_store(config)
    if STATE.beads_store is None:
        from beads import BeadsStore
        STATE.beads_store = BeadsStore()

    plugins = ai._build_plugins(
        config,
        existing_tools=get_all_tools(
            STATE.knowledge_store, scheduler=STATE.scheduler,
            goal_enabled=getattr(config, "goal_enabled", True),
        ),
    )
    STATE.plugin_tools = plugins.tools
    STATE.plugin_meta = plugins.meta
    STATE.knowledge_store = ai._apply_plugin_knowledge_backend(config, STATE.knowledge_store, plugins)


def operator_tools(config):
    """The allowlisted tools (core + plugin) to expose — empty allowlist ⇒ none."""
    from tools.lg_tools import get_all_tools

    allow = set(getattr(config, "operator_mcp_tools", []) or [])
    if not allow:
        return []
    tools = list(get_all_tools(
        STATE.knowledge_store,
        scheduler=STATE.scheduler,
        inbox_store=STATE.inbox_store,
        beads_store=STATE.beads_store,
        goal_enabled=bool(getattr(config, "goal_enabled", False)),
    ))
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
        if (name in allow) or (star and name not in _STAR_EXCLUDE):
            seen.add(name)
            out.append(t)
    return out


# Tools "*" skips — a coding-agent brain already has its own code execution / file tools,
# so exposing protoAgent's execute_code over the bus is redundant. Not a security gate
# (you can still allowlist it by name); just avoids handing it a tool it already has.
_STAR_EXCLUDE = {"execute_code"}


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
        len(exposed), ", ".join(exposed) or "(none — set operator_mcp.tools)",
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
    from graph.config_io import CONFIG_YAML_PATH

    config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
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
