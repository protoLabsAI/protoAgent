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
    headers = body.get("headers")
    if transport != "stdio" and isinstance(headers, dict):
        headers = {str(k).strip(): str(v) for k, v in headers.items() if str(k).strip()}
        if headers:
            entry["headers"] = headers
    return entry


# Map the various "transport"/"type" spellings found in shared MCP JSON to ours.
_TRANSPORT_ALIASES = {
    "streamable-http": "streamable_http", "streamablehttp": "streamable_http",
    "http-stream": "streamable_http",
}


def _normalize_named(name: str, spec: dict) -> dict:
    """A `{name: {...}}` entry (Claude-Desktop / mcp.json style) → a clean entry.

    Infers transport when absent: an explicit ``transport``/``type``, else ``stdio``
    if there's a ``command``, else ``http`` if there's a ``url``.
    """
    s = dict(spec or {})
    s["name"] = s.get("name") or name
    if "transport" not in s:
        t = str(s.get("type", "")).strip().lower()
        if t:
            s["transport"] = _TRANSPORT_ALIASES.get(t, t)
        elif s.get("command"):
            s["transport"] = "stdio"
        elif s.get("url"):
            s["transport"] = "http"
    return _clean_entry(s)


def _entries_from_blob(data: object) -> list[dict]:
    """Normalize a pasted MCP JSON blob into clean mcp.servers entries.

    Accepts the common shapes: the standard ``{"mcpServers": {name: spec}}`` wrapper,
    our own ``{"servers": [ {name, ...} ]}`` export, a single server object, or a bare
    ``{name: spec}`` map.
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    if isinstance(data.get("mcpServers"), dict):
        return [_normalize_named(n, s) for n, s in data["mcpServers"].items()]
    if isinstance(data.get("servers"), list):
        return [_clean_entry(s) for s in data["servers"]]
    if data.get("command") or data.get("url"):
        return [_clean_entry(data)]  # a single server object (must carry a name)
    if data and all(isinstance(v, dict) for v in data.values()):
        return [_normalize_named(n, s) for n, s in data.items()]
    raise HTTPException(status_code=400, detail="no MCP server found in the JSON")


def register_mcp_routes(app) -> None:
    """Register add / import / delete for `mcp.servers`."""

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

    @app.post("/api/mcp/servers/import")
    async def _import(body: dict | None = None):
        """Add one or more servers from a pasted MCP JSON blob (`{"raw": "<json>"}`)."""
        import json as _json

        raw = (body or {}).get("raw")
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(status_code=400, detail="raw JSON is required")
        try:
            data = _json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

        entries = _entries_from_blob(data)
        names = {e["name"] for e in entries}
        cfg = STATE.graph_config
        servers = [s for s in (getattr(cfg, "mcp_servers", []) or []) if s.get("name") not in names]
        servers.extend(entries)

        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"enabled": True, "servers": servers}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "added": sorted(names), "servers": [s["name"] for s in servers]}

    @app.delete("/api/mcp/servers/{name}")
    async def _remove(name: str):
        cfg = STATE.graph_config
        servers = [s for s in (getattr(cfg, "mcp_servers", []) or []) if s.get("name") != name]

        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"servers": servers}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "servers": [s["name"] for s in servers]}
