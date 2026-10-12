"""Operator API for MCP servers — add/remove from the console (hot reload).

Backs the Agent → MCP tab: edit ``mcp.servers`` without hand-editing YAML. Both
endpoints persist the change and hot-reload (``_build_mcp`` reconnects on reload),
so a new server's tools wire in immediately — no restart. The live config is
gitignored, so ``env`` values stay local.
"""

from __future__ import annotations

import asyncio
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


def _current_servers(current) -> list[dict]:
    return [s for s in (getattr(current, "mcp_servers", []) or []) if isinstance(s, dict)]


async def _apply_servers(build, *, enable: bool) -> list[dict]:
    """Rewrite ``mcp.servers`` from the CURRENT list and hot-reload. Returns the list written.

    ``build(current_servers) -> new_servers`` runs inside the config write lock, against
    the config the previous write committed (#2743's mechanism). Every route here used to
    build its list from a copy read BEFORE the lock, so two edits at once — an add racing
    an import, a promote racing a remove — each wrote its own version and one server
    silently vanished. And it runs off the event loop: these routes called the applier
    inline, freezing the whole server for the length of the reload."""
    from server.agent_init import _apply_settings_changes

    written: list[dict] = []
    ok, messages = await asyncio.to_thread(_apply_settings_changes, config=_servers_patch(build, enable, written))
    if not ok:
        raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
    return written


def _servers_patch(build, enable: bool, written: list[dict]):
    """The `(current_config) -> updates` callable the applier resolves inside its lock;
    records the list it wrote into ``written``."""

    def _updates(current) -> dict:
        written[:] = build(_current_servers(current))
        mcp: dict = {"servers": list(written)}
        if enable:
            mcp["enabled"] = True
        return {"mcp": mcp}

    return _updates


def _move_between_tiers(name: str, *, to_commons: bool) -> tuple[bool, list[str]]:
    """Promote (this agent → the box commons) or forget (commons → this agent) as ONE unit.

    It is two writes — the commons file and this agent's `mcp.servers` — and the applier
    rolls the agent's config back when the reload fails. Done as two separate steps, the
    commons write stayed while the config rolled back: a failed forget left the server in
    NEITHER tier. So both run under the config write lock, from state read inside it (the
    source-tier check included), and a failed apply restores the commons file too.
    Blocking: run it in a worker thread. Raises LookupError when `name` isn't in the
    source tier. (The commons file is box-wide, so another agent PROCESS editing it at
    the same moment is outside what an in-process lock can cover.)"""
    from graph.config_io import CONFIG_WRITE_LOCK
    from server.agent_init import _apply_settings_changes
    from tools.mcp_tools import read_mcp_commons, write_mcp_commons

    with CONFIG_WRITE_LOCK:
        cfg = STATE.graph_config
        commons_before = read_mcp_commons(cfg)
        if to_commons:
            entry = next((s for s in _current_servers(cfg) if s.get("name") == name), None)
            if entry is None:
                raise LookupError(f"no configured server named {name!r}")
            commons_after = [s for s in commons_before if s.get("name") != name] + [entry]

            def build(cur):
                return [s for s in cur if s.get("name") != name]
        else:
            entry = next((s for s in commons_before if s.get("name") == name), None)
            if entry is None:
                raise LookupError(f"no commons server named {name!r}")
            commons_after = [s for s in commons_before if s.get("name") != name]

            def build(cur):
                return [s for s in cur if s.get("name") != name] + [entry]

        write_mcp_commons(cfg, commons_after)
        try:
            ok, messages = _apply_settings_changes(config=_servers_patch(build, not to_commons, []))
        except BaseException:
            write_mcp_commons(cfg, commons_before)
            raise
        if not ok:
            write_mcp_commons(cfg, commons_before)  # the agent's config rolled back; so does this
        return ok, messages


def register_mcp_routes(app) -> None:
    """Register add / import / delete for `mcp.servers`."""

    @app.post("/api/mcp/servers")
    async def _add(body: dict | None = None):
        entry = _clean_entry(body or {})
        # enabling MCP + replacing the servers list; _build_mcp reconnects on reload.
        servers = await _apply_servers(
            lambda cur: [s for s in cur if s.get("name") != entry["name"]] + [entry], enable=True
        )
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
        servers = await _apply_servers(lambda cur: [s for s in cur if s.get("name") not in names] + entries, enable=True)
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
                    entries = (json.loads(f.read_text(encoding="utf-8")) or {}).get("servers") or []
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
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
        from runtime.operator_mcp_tools import (
            acp_operator_allowlist,
            resolve_allow,
            resolve_exposed_names,
            sidecar_exposed_names,
        )

        cfg = STATE.graph_config
        # Under an ACP runtime the client is the brain's sidecar, which is handed the ACP
        # default allowlist ("*" when unset) and boots its own stores — report THAT set,
        # the one shared derivation the runtime itself uses (#3248), not the host's view.
        acp_brain = str(getattr(cfg, "agent_runtime", "") or "").strip().lower().startswith("acp")
        if acp_brain:
            allow = resolve_allow(cfg, tools=acp_operator_allowlist(cfg))
            names = sidecar_exposed_names(cfg, allow=allow)
        else:
            allow = resolve_allow(cfg)
            names = resolve_exposed_names(cfg)
        profile = str(getattr(cfg, "operator_mcp_profile", "") or "").strip() or None
        return {
            "tools": sorted(names),
            "count": len(names),
            "profile": profile,
            "star": "*" in allow,
            "acp_default": acp_brain,
            "trust_override": os.environ.get("PROTOAGENT_MCP_TRUST", "").strip().lower() == "full",
        }

    @app.delete("/api/mcp/servers/{name}")
    async def _remove(name: str):
        servers = await _apply_servers(lambda cur: [s for s in cur if s.get("name") != name], enable=False)
        return {"ok": True, "servers": [s["name"] for s in servers]}

    @app.post("/api/mcp/servers/{name}/promote")
    async def _promote(name: str):
        """Share a configured server to the box commons (ADR 0041): MOVE it from this
        agent's ``mcp.servers`` into ``commons/mcp-servers.json``. With ``mcp.scope:
        layered`` the agent keeps running it (now as the commons tier) and every other
        layered agent on the box picks it up."""
        try:
            ok, messages = await asyncio.to_thread(_move_between_tiers, name, to_commons=True)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "promoted": True, "name": name}

    @app.post("/api/mcp/servers/{name}/forget")
    async def _forget(name: str):
        """Unshare a commons server (the inverse of promote): MOVE it out of the box
        commons back into this agent's ``mcp.servers``. No other agent on the box will
        run it after this."""
        try:
            ok, messages = await asyncio.to_thread(_move_between_tiers, name, to_commons=False)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
        return {"ok": True, "forgotten": True, "name": name}
