"""Operator API for MCP servers — add/remove from the console (hot reload).

Backs the Agent → MCP tab: edit ``mcp.servers`` without hand-editing YAML. Both
endpoints persist the change and hot-reload (``_build_mcp`` reconnects on reload),
so a new server's tools wire in immediately — no restart. The live config is
gitignored, so ``env`` values stay local.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException

from graph.mcp_config import clean_mcp_entry, entries_from_blob
from runtime.state import STATE

log = logging.getLogger(__name__)


# The entry normalizer/validator lives in ``graph.mcp_config`` so the bundle/archetype
# seed path (``graph/workspaces/manager.py``) can reuse it without ``graph/`` importing
# ``operator_api/`` (the import-layering fence). These thin wrappers translate its
# ``ValueError`` into the HTTP 400 the console form expects.
def _clean_entry(body: dict) -> dict:
    """Validate + normalize an mcp.servers entry from the form (400 on bad input)."""
    try:
        return clean_mcp_entry(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _entries_from_blob(data: object) -> list[dict]:
    """Normalize a pasted MCP JSON blob into clean mcp.servers entries (400 if none)."""
    try:
        return entries_from_blob(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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

    @app.get("/api/mcp/catalog")
    async def _catalog():
        """Curated common MCP servers (`config/mcp-catalog.json`, live dir overrides the
        bundle) — each a templated `mcp.servers` entry the console can one-click add. Marks
        `installed` by name so the picker shows what's already configured."""
        import json

        from infra.paths import instance_paths

        ip = instance_paths()
        entries: list[dict] = []
        for base in (ip.config_dir, ip.bundle_dir):
            f = base / "mcp-catalog.json"
            if f.exists():
                try:
                    entries = (json.loads(f.read_text()) or {}).get("servers") or []
                except (json.JSONDecodeError, OSError):
                    log.warning("[mcp] mcp-catalog.json unreadable at %s", f)
                break

        cfg = STATE.graph_config
        configured = {
            str(s.get("name", "")).strip().lower()
            for s in (getattr(cfg, "mcp_servers", []) or [])
            if isinstance(s, dict)
        }
        out: list[dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            tmpl = e.get("template") if isinstance(e.get("template"), dict) else {}
            nm = str(tmpl.get("name") or e.get("id") or "").strip().lower()
            out.append({**e, "installed": bool(nm) and nm in configured})
        return {"servers": out}

    @app.get("/api/mcp/exposed")
    async def _exposed():
        """The tools THIS instance's operator MCP would expose to a foreign MCP client
        (Claude Desktop, Cursor, an ACP brain) — the effective set after the
        ``operator_mcp_profile`` + ``operator_mcp_tools`` allowlist + ``PROTOAGENT_MCP_TRUST``
        resolve. Previously introspectable only by reading the sidecar's boot logs
        (ADR 0075 D2/D3). Read-only; behind the standard operator-API auth gate."""
        from runtime.operator_mcp_tools import resolve_allow, resolve_exposed_names

        cfg = STATE.graph_config
        allow = resolve_allow(cfg)
        names = resolve_exposed_names(cfg)
        profile = str(getattr(cfg, "operator_mcp_profile", "") or "").strip() or None
        return {
            "tools": sorted(names),
            "count": len(names),
            "profile": profile,
            "star": "*" in allow,
            "trust_override": os.environ.get("PROTOAGENT_MCP_TRUST", "").strip().lower() == "full",
        }

    @app.delete("/api/mcp/servers/{name}")
    async def _remove(name: str):
        cfg = STATE.graph_config
        servers = [s for s in (getattr(cfg, "mcp_servers", []) or []) if s.get("name") != name]

        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"servers": servers}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "servers": [s["name"] for s in servers]}

    @app.post("/api/mcp/servers/{name}/promote")
    async def _promote(name: str):
        """Share a configured server to the box commons (ADR 0041): MOVE it from this
        agent's ``mcp.servers`` into ``commons/mcp-servers.json``. With ``mcp.scope:
        layered`` the agent keeps running it (now as the commons tier) and every other
        layered agent on the box picks it up."""
        from tools.mcp_tools import read_mcp_commons, write_mcp_commons

        cfg = STATE.graph_config
        private = [s for s in (getattr(cfg, "mcp_servers", []) or []) if isinstance(s, dict)]
        entry = next((s for s in private if s.get("name") == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no configured server named {name!r}")

        commons = [s for s in read_mcp_commons(cfg) if s.get("name") != name]
        commons.append(entry)
        write_mcp_commons(cfg, commons)

        remaining = [s for s in private if s.get("name") != name]
        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"servers": remaining}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "promoted": True, "name": name}

    @app.post("/api/mcp/servers/{name}/forget")
    async def _forget(name: str):
        """Unshare a commons server (the inverse of promote): MOVE it out of the box
        commons back into this agent's ``mcp.servers``. No other agent on the box will
        run it after this."""
        from tools.mcp_tools import read_mcp_commons, write_mcp_commons

        cfg = STATE.graph_config
        commons = read_mcp_commons(cfg)
        entry = next((s for s in commons if s.get("name") == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no commons server named {name!r}")

        write_mcp_commons(cfg, [s for s in commons if s.get("name") != name])

        private = [s for s in (getattr(cfg, "mcp_servers", []) or []) if isinstance(s, dict) and s.get("name") != name]
        private.append(entry)
        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(config={"mcp": {"enabled": True, "servers": private}})
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "forgotten": True, "name": name}
