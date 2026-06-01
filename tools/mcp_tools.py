"""Model Context Protocol (MCP) client — expose MCP-server tools to the agent.

Configured MCP servers (stdio or streamable-HTTP) are connected via
``langchain-mcp-adapters``; their tools are discovered at graph-build time and
appended to the agent's tool list as ordinary LangChain ``BaseTool``s. Tools are
namespaced by server (``<server>__<tool>``) so they can't shadow core tools, and
``MultiServerMCPClient`` is stateless — each invocation opens a fresh MCP
session — so the discovered tools are event-loop-agnostic and the client object
just needs to stay alive for reconnection.

Configuring a server is the opt-in act; MCP is off unless ``mcp.enabled`` is set.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("protoagent.mcp")


def _server_connection(server: dict) -> dict | None:
    """Map a config ``mcp.servers[]`` entry to a langchain-mcp-adapters
    connection dict. Returns ``None`` for an entry missing its essential fields
    (logged + skipped by the caller). Only provided keys are set; the adapter
    fills the rest with defaults.
    """
    transport = str(server.get("transport") or "stdio").strip().lower()

    if transport in ("http", "streamable_http", "streamable-http"):
        url = server.get("url")
        if not url:
            return None
        conn: dict[str, Any] = {"transport": "streamable_http", "url": str(url)}
        if server.get("headers"):
            conn["headers"] = dict(server["headers"])
        return conn

    if transport == "sse":
        url = server.get("url")
        if not url:
            return None
        conn = {"transport": "sse", "url": str(url)}
        if server.get("headers"):
            conn["headers"] = dict(server["headers"])
        return conn

    # Default: stdio (local subprocess).
    command = server.get("command")
    if not command:
        return None
    conn = {"transport": "stdio", "command": str(command), "args": list(server.get("args") or [])}
    if server.get("env"):
        conn["env"] = dict(server["env"])
    if server.get("cwd"):
        conn["cwd"] = str(server["cwd"])
    return conn


def _run_blocking(coro, timeout: float):
    """Run an async coroutine to completion from sync code, in any context.

    At boot there's no running loop → ``asyncio.run``. The reload path runs
    inside the server's event loop → offload to a throwaway thread with its own
    loop. Safe because MCP discovery sessions are stateless and short-lived.
    """
    import asyncio

    async def _with_timeout():
        return await asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_timeout())

    import threading

    box: dict[str, Any] = {}

    def _worker():
        try:
            box["value"] = asyncio.run(_with_timeout())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _core_tool_names() -> set[str]:
    """Names the agent already uses — MCP tools that collide are skipped."""
    try:
        from tools.lg_tools import (
            INBOX_TOOL_NAMES,
            MEMORY_TOOL_NAMES,
            SCHEDULER_TOOL_NAMES,
            get_all_tools,
        )

        names = {t.name for t in get_all_tools()}
        names |= set(MEMORY_TOOL_NAMES) | set(SCHEDULER_TOOL_NAMES) | set(INBOX_TOOL_NAMES)
        names |= {"task", "task_batch", "execute_code"}
        return names
    except Exception:  # noqa: BLE001 — collision check is best-effort
        return set()


def build_mcp_tools(config) -> tuple[list, list, list[dict]]:
    """Discover tools from configured MCP servers.

    Returns ``(clients, tools, servers_meta)``:
    - ``clients`` — live ``MultiServerMCPClient``s, one per server, kept alive so
      the stateless tools can reconnect on invocation.
    - ``tools`` — LangChain ``BaseTool``s to append to the agent.
    - ``servers_meta`` — ``[{name, transport, tool_count}]`` for runtime status.

    Each server is isolated: a bad/unreachable one is logged and skipped, never
    fatal. MCP is off unless ``config.mcp_enabled``.
    """
    clients: list = []
    tools: list = []
    meta: list[dict] = []

    if not getattr(config, "mcp_enabled", False):
        return clients, tools, meta

    servers = getattr(config, "mcp_servers", []) or []
    timeout = float(getattr(config, "mcp_timeout_seconds", 20.0))
    denylist = set(getattr(config, "mcp_denylist", []) or [])
    core_names = _core_tool_names()

    from langchain_mcp_adapters.client import MultiServerMCPClient

    for server in servers:
        if not isinstance(server, dict):
            log.warning("[mcp] skipping non-mapping server entry: %r", server)
            continue
        name = str(server.get("name") or "").strip()
        conn = _server_connection(server)
        if not name or conn is None:
            log.warning("[mcp] skipping invalid server entry (need name + command/url): %r", server)
            continue

        # Lazy connect: a server explicitly disabled is never contacted, so a
        # configured-but-paused server costs neither a connection nor context.
        if server.get("enabled", True) is False:
            log.info("[mcp] server %r disabled — not connecting", name)
            continue

        # Per-server tool filter — the primary defense against a large catalog
        # dumping dozens of tool schemas into context. ``include`` is an
        # allowlist (when set, only those tools survive); ``exclude`` drops
        # tools from whatever remains. Both match the bare tool name (what you
        # configure) or the namespaced ``<server>__<tool>`` form.
        tool_filter = server.get("tools") or {}
        include = {str(n) for n in (tool_filter.get("include") or [])}
        exclude = {str(n) for n in (tool_filter.get("exclude") or [])}

        try:
            # tool_name_prefix=True → tools are named "<server>__<tool>".
            client = MultiServerMCPClient({name: conn}, tool_name_prefix=True)
            discovered = _run_blocking(client.get_tools(), timeout)
        except Exception as exc:  # noqa: BLE001 — one server must not break the rest
            log.warning("[mcp] server %r discovery failed: %s — skipping", name, exc)
            continue

        prefix = f"{name}__"
        kept = []
        for tool in discovered:
            bare = tool.name[len(prefix):] if tool.name.startswith(prefix) else tool.name
            names = {tool.name, bare}
            included = bool(names & include)
            if include and not included:
                log.info("[mcp] %s: %s not in include allowlist — skipped", name, tool.name)
                continue
            # include wins over a same-server exclude; the global denylist is the
            # hard safety net and is never overridden.
            if (names & exclude) and not included:
                log.info("[mcp] %s: %s in exclude — skipped", name, tool.name)
                continue
            if names & denylist:
                log.info("[mcp] %s: %s in denylist — skipped", name, tool.name)
                continue
            if tool.name in core_names:
                log.warning("[mcp] %s: %s collides with a core tool — skipped", name, tool.name)
                continue
            kept.append(tool)

        clients.append(client)
        tools.extend(kept)
        meta.append({
            "name": name,
            "transport": conn["transport"],
            "tool_count": len(kept),
        })
        log.info("[mcp] server %s (%s): %d tool(s)", name, conn["transport"], len(kept))

    return clients, tools, meta
