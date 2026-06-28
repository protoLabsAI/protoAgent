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
import os
import re
from typing import Any

log = logging.getLogger("protoagent.mcp")


def _mcp_commons_path(config):
    """The box-shared MCP-server commons file (ADR 0041) — read by every agent on the
    host, never per-instance scoped. Under ``commons.path`` (blank → ``~/.protoagent/
    commons``)."""
    from pathlib import Path

    raw = (getattr(config, "commons_path", "") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / ".protoagent" / "commons")
    return base / "mcp-servers.json"


def read_mcp_commons(config) -> list[dict]:
    """The shared MCP servers (``{"servers": [...]}``); [] when absent/unreadable."""
    import json

    path = _mcp_commons_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("[mcp] commons file unreadable at %s — ignoring", path)
        return []
    servers = data.get("servers") if isinstance(data, dict) else data
    return [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]


def write_mcp_commons(config, servers: list[dict]) -> None:
    """Persist the shared MCP-server commons (used by promote/unshare)."""
    import json

    path = _mcp_commons_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": list(servers)}, indent=2) + "\n")


def _mcp_tool_error_handler(exc: Exception) -> str:
    """Turn an MCP tool failure into a recoverable tool result (roxy #58).

    ``langchain-mcp-adapters`` raises ``ToolException`` when the server returns an
    error (e.g. a board ``404 Feature not found`` from a stale id). Left unhandled
    that propagates out of the ToolNode and fails the WHOLE A2A turn. Setting this
    as each MCP tool's ``handle_tool_error`` makes the tool return this string to
    the model instead — so a single recoverable tool error (stale arg, transient
    4xx) degrades into something the model can adapt to, not a dead turn.
    """
    return (
        f"Tool error: {exc}. The tool call failed — commonly a stale/invalid "
        "argument (e.g. an id that no longer exists) or a transient error. Do NOT "
        "treat this as fatal: adjust the arguments and retry, try a different "
        "approach, or continue without this tool's result."
    )


# Env-var NAMES that look like a credential — stripped from a stdio MCP server's
# inherited environment by default. A third-party server (npx/uvx) gets the
# operational env it needs (PATH/HOME/LANG/proxy/base-URLs/...) but NOT the agent's
# secrets. We strip: generic *_SECRET/*_TOKEN/*_PASSWORD/*_KEY and API/access/
# private keys; connection strings / DSNs that embed ``user:password@host``
# (DATABASE_URL, REDIS_URL, SENTRY_DSN, ...); and capability-bearing agent handles
# (SSH_AUTH_SOCK / KRB5CCNAME / GPG_AGENT_INFO) that would let an untrusted server
# impersonate the user via the SSH / Kerberos / GPG agent. We deliberately KEEP
# plain ``*_BASE_URL``/``*_URL`` that don't carry creds, so a base-URL-only server
# still works. A server that genuinely needs a stripped var opts in with
# ``inherit_env: true`` or a per-server ``env:`` block.
_SECRET_ENV_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|_KEY$"
    r"|_DSN$"
    r"|^(?:DATABASE|POSTGRES(?:QL)?|MYSQL|MARIADB|REDIS|MONGO(?:DB)?|RABBITMQ|AMQP|"
    r"CLICKHOUSE|ELASTIC(?:SEARCH)?|OPENSEARCH)[_-]?(?:URL|URI)$"
    r"|^SQLALCHEMY_DATABASE_URI$"
    r"|^SSH_AUTH_SOCK$|^KRB5CCNAME$|^GPG_AGENT_INFO$)",
    re.IGNORECASE,
)


def _inherited_env(server_env: dict[str, str], *, inherit) -> dict[str, str] | None:
    """Build the env for a stdio MCP subprocess.

    ``inherit`` is the server's ``inherit_env`` value (``None`` = unset):

      - unset (default) → parent env with credential-looking NAMES stripped, then
        the per-server ``env:`` overlaid — a server still gets PATH/HOME/etc. but
        not the agent's secrets;
      - ``True`` → the FULL parent env (explicit opt-in escape hatch, e.g. a
        trusted server that needs a secret injected via the environment);
      - ``False`` → only the explicit per-server ``env:`` (minimal), or ``None``
        so the SDK applies its own minimal default.

    A per-server ``env:`` value always wins over an inherited one.
    """
    if inherit is False:
        return dict(server_env) if server_env else None
    if inherit is True:
        return {**os.environ, **server_env}
    base = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    return {**base, **server_env}


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
    # Build the subprocess env (secret-filtered parent env by default). The MCP
    # SDK's stdio client otherwise uses a MINIMAL env, dropping vars a server may
    # need; we inherit the operational env but strip credential-looking NAMES so a
    # third-party server can't read the agent's secrets. ``inherit_env: true``
    # passes the FULL env (escape hatch); ``false`` passes only the per-server
    # ``env:``. See ``_inherited_env``.
    server_env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
    env = _inherited_env(server_env, inherit=server.get("inherit_env"))
    if env is not None:
        conn["env"] = env
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
        names |= {"task", "task_batch"}
        return names
    except Exception:  # noqa: BLE001 — collision check is best-effort
        return set()


def build_mcp_tools(config, *, plugin_servers=None) -> tuple[list, list, list[dict]]:
    """Discover tools from configured MCP servers.

    Returns ``(clients, tools, servers_meta)``:
    - ``clients`` — live ``MultiServerMCPClient``s, one per server, kept alive so
      the stateless tools can reconnect on invocation.
    - ``tools`` — LangChain ``BaseTool``s to append to the agent.
    - ``servers_meta`` — ``[{name, transport, tool_count}]`` for runtime status.

    ``plugin_servers`` is a list of factories ``factory(config) -> entry|None``
    contributed by plugins (``register_mcp_server``) — e.g. an OAuth-gated managed
    server. A factory's entry is injected like a configured
    server (and replaces a same-named ``mcp.servers`` entry), and its presence
    alone is enough to treat MCP as active, so the operator never edits
    ``mcp.servers`` to use a plugin's managed server.

    Each server is isolated: a bad/unreachable one is logged and skipped, never
    fatal. MCP is off unless ``config.mcp_enabled`` (or a plugin contributes one).
    """
    clients: list = []
    tools: list = []
    meta: list[dict] = []

    # Tiered merge (ADR 0041), lowest precedence first so a later layer wins by name:
    #   box commons (mcp.scope: layered) < this agent's mcp.servers < plugin-managed.
    # Each surviving server is tagged with its tier for runtime status (drives the
    # console's commons/private badges + share/unshare).
    servers: list = []
    tier_by_name: dict[str, str] = {}

    def _layer(entries, tier):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            nm = str(entry.get("name") or "").strip()
            if not nm:
                continue
            servers[:] = [s for s in servers if str(s.get("name") or "").strip() != nm]
            servers.append(entry)
            tier_by_name[nm] = tier

    scope = str(getattr(config, "mcp_scope", "") or "").strip().lower()
    commons_servers = read_mcp_commons(config) if scope == "layered" else []
    _layer(commons_servers, "commons")
    _layer(list(getattr(config, "mcp_servers", []) or []), "private")

    # Plugin-contributed managed MCP servers (ADR 0019). A factory returns an entry
    # only when its surface is on + connected, so the server comes and goes with
    # config without the operator touching mcp.servers.
    plugin_entries = []
    for factory in plugin_servers or []:
        try:
            entry = factory(config)
        except Exception:  # noqa: BLE001 — a bad factory must not break MCP
            log.exception("[mcp] plugin MCP server factory failed — skipped")
            continue
        if entry:
            plugin_entries.append(entry)
    _layer(plugin_entries, "managed")

    # MCP is active if the operator enabled it, a plugin contributes a server, or the
    # agent opted into the box commons (layered) and that commons has servers.
    if not (getattr(config, "mcp_enabled", False) or plugin_entries or commons_servers):
        return clients, tools, meta

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
            bare = tool.name[len(prefix) :] if tool.name.startswith(prefix) else tool.name
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
            # roxy #58: a tool error (e.g. board 404) must degrade into a tool
            # result the model can recover from, not fail the whole turn.
            try:
                tool.handle_tool_error = _mcp_tool_error_handler
            except Exception:  # noqa: BLE001 — best-effort; never block tool registration
                log.debug("[mcp] %s: could not set handle_tool_error on %s", name, tool.name)
            kept.append(tool)

        clients.append(client)
        tools.extend(kept)
        meta.append(
            {
                "name": name,
                "transport": conn["transport"],
                "tool_count": len(kept),
                "tier": tier_by_name.get(name),
            }
        )
        log.info("[mcp] server %s (%s): %d tool(s)", name, conn["transport"], len(kept))

    return clients, tools, meta
