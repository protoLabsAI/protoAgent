"""Operator API for MCP servers — add/remove from the console (hot reload).

Backs the Agent → MCP tab: edit ``mcp.servers`` without hand-editing YAML. Both
endpoints persist the change and hot-reload (``_build_mcp`` reconnects on reload),
so a new server's tools wire in immediately — no restart. The live config is
gitignored, so ``env`` values stay local.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from runtime.state import STATE

log = logging.getLogger(__name__)

_TRANSPORTS = {"stdio", "http", "streamable_http", "sse"}


def _clean_entry(body: dict) -> dict:
    """Validate + normalize an mcp.servers entry from the form."""
    name = str(body.get("name", "")).strip()
    transport = (str(body.get("transport", "stdio")).strip() or "stdio")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if transport not in _TRANSPORTS:
        raise HTTPException(status_code=400, detail=f"transport must be one of {sorted(_TRANSPORTS)}")

    entry: dict = {"name": name, "transport": transport}
    if transport == "stdio":
        command = str(body.get("command", "")).strip()
        if not command:
            raise HTTPException(status_code=400, detail="stdio transport needs a command")
        entry["command"] = command
        args = body.get("args")
        if isinstance(args, list):
            args = [str(a).strip() for a in args if str(a).strip()]
        elif isinstance(args, str):
            args = args.split()
        else:
            args = []
        if args:
            entry["args"] = args
    else:  # http / streamable_http / sse
        url = str(body.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail=f"{transport} transport needs a url")
        entry["url"] = url

    env = body.get("env")
    if isinstance(env, dict):
        env = {str(k).strip(): str(v) for k, v in env.items() if str(k).strip()}
        if env:
            entry["env"] = env
    return entry


def register_mcp_routes(app) -> None:
    """Register `POST /api/mcp/servers` and `DELETE /api/mcp/servers/{name}`."""

    @app.post("/api/mcp/servers")
    async def _add(body: dict | None = None):
        entry = _clean_entry(body or {})
        cfg = STATE.graph_config
        servers = [s for s in (getattr(cfg, "mcp_servers", []) or []) if s.get("name") != entry["name"]]
        servers.append(entry)

        from server.agent_init import _apply_settings_changes

        # enabling MCP + replacing the servers list; _build_mcp reconnects on reload.
        ok, messages = _apply_settings_changes(config={"mcp": {"enabled": True, "servers": servers}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "name": entry["name"], "servers": [s["name"] for s in servers]}

    @app.delete("/api/mcp/servers/{name}")
    async def _remove(name: str):
        cfg = STATE.graph_config
        servers = [s for s in (getattr(cfg, "mcp_servers", []) or []) if s.get("name") != name]

        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"servers": servers}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "servers": [s["name"] for s in servers]}
