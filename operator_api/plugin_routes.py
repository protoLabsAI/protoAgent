"""Operator API for git-installed plugins (ADR 0027, PR2).

Backs the console Plugins panel: list installed plugins (with their manifest +
declared capabilities for review), install from a git URL, uninstall, and
enable/disable. Install fetches code only (install ≠ enable). Enable/disable edits
``plugins.enabled`` and hot-reloads.

ENABLE is fully live: tools/middleware/MCP rebuild with the graph, and a plugin's
router — which is what serves a console view (the view iframe just points at a
router route) — is hot-mounted on the same reload (``_mount_plugin_routers`` in
``server.agent_init``, #822). So enabling a view-contributing plugin needs no
restart; ``restart_recommended`` stays False for enable.

DISABLE is the residual restart case: FastAPI has no route-removal API, so a
disabled plugin's view/route lingers on the live app until a process restart
(documented in ``_mount_plugin_routers``). We flag ``restart_recommended`` only
when disabling a plugin that contributed a view/route/surface.
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

    @app.post("/api/plugins/{plugin_id}/enabled")
    async def _set_enabled(plugin_id: str, body: dict | None = None):
        """Enable/disable a plugin by editing `plugins.enabled`/`disabled` + hot-reloading.

        ENABLE is fully live: tools / subagents / middleware / MCP rebuild with the graph,
        and the plugin's router (which serves any **console view** — the view iframe just
        points at a router route) is hot-mounted on the same reload (#822). So a freshly
        enabled view-contributing plugin works immediately; ``restart_recommended`` is False.

        DISABLE can't tear a router back down (FastAPI has no route-removal API), so a
        disabled plugin's view/route/surface lingers until a process restart — only that
        path flags ``restart_recommended`` so the UI can say so.
        """
        want = bool((body or {}).get("enabled"))
        cfg = STATE.graph_config
        enabled = [p for p in (getattr(cfg, "plugins_enabled", []) or []) if p != plugin_id]
        disabled = [p for p in (getattr(cfg, "plugins_disabled", []) or []) if p != plugin_id]
        # Snapshot the plugin's pre-reload meta — on DISABLE the reload clears its views
        # from STATE.plugin_meta, so we must read "did it contribute a surface?" first.
        prev_meta = next((p for p in (STATE.plugin_meta or []) if p.get("id") == plugin_id), None)
        if want:
            enabled.append(plugin_id)
        else:
            disabled.append(plugin_id)

        from server.agent_init import _apply_settings_changes

        ok, messages = _apply_settings_changes(
            config={"plugins": {"enabled": enabled, "disabled": disabled}},
        )
        if not ok:
            raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")

        # Enabling hot-mounts the router that serves the view (#822) — fully live, no
        # restart. Only DISABLE leaves a stale route behind (no FastAPI unmount), so we
        # recommend a restart when turning OFF a plugin that contributed a view/route/surface.
        def _has_surface(m: dict | None) -> bool:
            return bool(m and (m.get("views") or m.get("routers") or m.get("surfaces")))

        restart = bool(not want and _has_surface(prev_meta))
        return {"ok": True, "enabled": want, "reloaded": True, "restart_recommended": restart}


    @app.delete("/api/plugins/{plugin_id}")
    async def _uninstall(plugin_id: str, purge: bool = False):
        # purge=true also removes the plugin's config section + secrets (ADR 0027).
        try:
            report = installer.uninstall(plugin_id, purge=purge)
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **report}
