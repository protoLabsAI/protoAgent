"""Operator API for git-installed plugins (ADR 0027, PR2).

Backs the console Plugins panel: list installed plugins (with their manifest +
declared capabilities for review), install from a git URL, and uninstall. Install
fetches code only — enabling stays a config + restart decision (install ≠ enable).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from graph.plugins import installer
from graph.plugins.manifest import load_manifest
from runtime.state import STATE

log = logging.getLogger(__name__)


def _sources_allowlist() -> list[str] | None:
    """`plugins.sources.allow` from config, if a fork locked installs down (PR3
    wires the config field; None = open)."""
    cfg = STATE.graph_config
    allow = getattr(cfg, "plugins_sources_allow", None) if cfg else None
    return list(allow) if allow else None


def register_plugin_routes(app) -> None:
    """Register `/api/plugins/installed`, `/api/plugins/install`, `/api/plugins/{id}`."""

    @app.get("/api/plugins/installed")
    async def _installed():
        # enabled state comes from the loader's per-plugin meta (id → enabled)
        enabled = {p["id"]: bool(p.get("enabled")) for p in (STATE.plugin_meta or [])}
        root = installer.live_plugins_dir()
        out = []
        for e in installer.list_installed():
            item = {**e, "enabled": enabled.get(e["id"], False)}
            m = load_manifest(root / e["id"]) if e.get("present") else None
            if m is not None:
                item["manifest"] = {
                    "name": m.name, "version": m.version, "description": m.description,
                    "repository": m.repository, "homepage": m.homepage,
                    "capabilities": m.capabilities, "requires_env": m.requires_env,
                    "requires_pip": m.requires_pip,
                    "views": [v.get("label") for v in m.views],
                    "secrets": m.secrets,
                }
            out.append(item)
        return {"plugins": out}

    @app.post("/api/plugins/install")
    async def _install(body: dict | None = None):
        body = body or {}
        url = str(body.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        ref = (str(body.get("ref", "")).strip() or None)
        force = bool(body.get("force"))
        try:
            summary = installer.install(
                url, ref, force=force, by="console", allow=_sources_allowlist(),
            )
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # install ≠ enable: the new plugin's routes/surfaces mount at init, so it
        # needs a restart + plugins.enabled to take effect.
        return {"installed": summary, "restart_required": True}

    @app.delete("/api/plugins/{plugin_id}")
    async def _uninstall(plugin_id: str, purge: bool = False):
        # purge=true also removes the plugin's config section + secrets (ADR 0027).
        try:
            report = installer.uninstall(plugin_id, purge=purge)
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **report}
