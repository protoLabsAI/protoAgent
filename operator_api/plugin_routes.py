"""Operator API for git-installed plugins (ADR 0027, PR2).

Backs the console Plugins panel: list installed plugins (with their manifest +
declared capabilities for review), install from a git URL, uninstall, and
enable/disable. **Installing AUTO-ENABLES + runs the plugin** (trust-by-default — the
console flashes a one-time "this runs code" confirm for unofficial sources first; opt
out with ``PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1`` for strict install ≠ enable).
Enable/disable edits ``plugins.enabled`` and hot-reloads.

ENABLE is fully live: tools/middleware/MCP rebuild with the graph, and a plugin's
router — which is what serves a console view (the view iframe just points at a
router route) — is hot-mounted on the same reload (``_mount_plugin_routers`` in
``server.agent_init``, #822). So enabling a view-contributing plugin needs no
restart; ``restart_recommended`` stays False for enable.

DISABLE is the residual restart case: FastAPI has no route-removal API, so a
disabled plugin's view/route lingers on the live app until a process restart
(documented in ``_mount_plugin_routers``). We flag ``restart_recommended`` only
when disabling a plugin that contributed a view/route/surface.

FORCE RE-INSTALL (and UPDATE, which is a force re-install at the recorded ref) is
the other residual case (#942): the reload re-registers the plugin's router, but the
mount keeps the FIRST one (FastAPI can't swap in place) — the freshly installed
routes don't serve until a process restart, so both routes flag it.
"""

from __future__ import annotations

import asyncio
import logging
import re

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


def _install_no_enable() -> bool:
    """Opt out of auto-enable-on-install — back to ADR 0027's strict install ≠ enable.
    Default off: installing a plugin enables + runs it (trust-by-default; the console
    flashes a one-time "this runs code" confirm for unofficial sources first)."""
    import os

    return os.environ.get("PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE", "").strip().lower() in ("1", "true", "yes")


def _enabled_ids_from_summary(summary: dict) -> list[str]:
    """The plugin id(s) to enable for an install summary: a single plugin → its id; a
    bundle → its declared ``enabled`` set (else every installed member)."""
    if "bundle" in summary:
        suggested = [str(x) for x in (summary.get("enabled") or [])]
        members = [str(s["id"]) for s in (summary.get("installed") or []) if s.get("id")]
        return suggested or members
    pid = summary.get("id")
    return [str(pid)] if pid else []


def _installed_ids_from_summary(summary: dict) -> list[str]:
    """The plugin id(s) whose CODE this install just placed on disk — for a bundle,
    its fetched members only (``builtin`` members aren't fetched, so they can't have
    been replaced under a live mount)."""
    if "bundle" in summary:
        return [str(s["id"]) for s in (summary.get("installed") or []) if s.get("id")]
    pid = summary.get("id")
    return [str(pid)] if pid else []


def _has_surface(meta: dict | None) -> bool:
    """True when the plugin contributed a view / router / background surface — the
    contributions FastAPI can't unmount or swap in place (restart territory)."""
    return bool(meta and (meta.get("views") or meta.get("routers") or meta.get("surfaces")))


def _mounted_router_ids() -> set[str]:
    """Plugin ids with a router currently mounted on the live app. This is the mount
    ground truth (``_mount_plugin_routers``'s registry) — unlike ``plugin_meta`` it
    survives a disable, whose router lingers mounted with no meta entry."""
    keys = getattr(STATE, "plugin_router_keys", None) or set()
    return {pid for (pid, _prefix) in keys}


def _purge_plugin_modules(plugin_id: str) -> None:
    """Drop a plugin's module subtree from ``sys.modules`` so the next reload
    re-execs every file from disk. The loader re-execs the entry ``__init__`` each
    reload, but a multi-file plugin's ``from .tools import …`` resolves the SUBMODULE
    through ``sys.modules`` — which still holds the OLD code after a force
    re-install. Scoped to the plugin's own prefix; the reload rebuilds it."""
    import sys

    from graph.plugins.loader import _plugin_module_name

    prefix = _plugin_module_name(plugin_id)
    for name in [n for n in list(sys.modules) if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(name, None)


def register_plugin_routes(app) -> None:
    """Register `/api/plugins/installed`, `/install`, `/updates`, `/{id}/enabled`,
    `/{id}/update`, and DELETE `/{id}`."""

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
                    "name": m.name,
                    "version": m.version,
                    "description": m.description,
                    "repository": m.repository,
                    "homepage": m.homepage,
                    "capabilities": m.capabilities,
                    "requires_env": m.requires_env,
                    "requires_pip": m.requires_pip,
                    "views": [v.get("label") for v in m.views],
                    "secrets": m.secrets,
                }
            out.append(item)
        return {"plugins": out}

    @app.get("/api/plugins/catalog")
    async def _catalog():
        """The curated official-plugin directory (ADR 0059) — `config/plugin-catalog.json`
        (live dir overrides the bundle), merged with install state so the Discover UI can
        show **Available / Installed / Bundled**. State is matched by `repo` URL (robust)
        or id; one-click install runs `plugin install <repo>` (ADR 0058)."""
        import json

        from graph.config_io import _BUNDLE_CONFIG_DIR, _live_config_dir

        entries: list[dict] = []
        for base in (_live_config_dir(), _BUNDLE_CONFIG_DIR):
            f = base / "plugin-catalog.json"
            if f.exists():
                try:
                    entries = (json.loads(f.read_text()) or {}).get("plugins") or []
                except (json.JSONDecodeError, OSError):
                    log.warning("[plugins] plugin-catalog.json unreadable at %s", f)
                break

        def _norm(u: str | None) -> str:
            return re.sub(r"\.git$", "", (u or "").strip().rstrip("/")).lower()

        installed = installer.list_installed()
        by_url = {_norm(e.get("source_url")): e["id"] for e in installed if e.get("source_url")}
        by_id = {e["id"] for e in installed}
        enabled = {p["id"]: bool(p.get("enabled")) for p in (STATE.plugin_meta or [])}

        out = []
        for entry in entries:
            eid = entry.get("id") or ""
            repo = entry.get("repo") or entry.get("install_url") or ""
            # Bundled built-in (still in the repo's plugins/ tree) — already present, can't
            # be git-installed over (the installer's built-in guard); show as "Bundled".
            bundled = bool(eid) and (installer.REPO_ROOT / "plugins" / eid).exists()
            inst_id = by_url.get(_norm(repo)) or (eid if eid in by_id else None)
            out.append(
                {
                    **entry,
                    "bundled": bundled,
                    "installed": inst_id is not None,
                    "enabled": enabled.get(inst_id, False) if inst_id else False,
                }
            )
        return {"plugins": out}

    @app.post("/api/plugins/install")
    async def _install(body: dict | None = None):
        body = body or {}
        url = str(body.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        ref = str(body.get("ref", "")).strip() or None
        force = bool(body.get("force"))
        try:
            # git clone + dep work is blocking — offload so it can't stall the
            # event loop (and with it every chat/A2A/scheduler request) (#DoS).
            summary = await asyncio.to_thread(
                installer.install, url, ref, force=force, by="console", allow=_sources_allowlist()
            )
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Install AUTO-ENABLES + runs the code (ADR 0027, trust-by-default posture):
        # installing IS the consent — the console flashes a one-time "this runs code"
        # confirm for unofficial sources first. Add the plugin (or every bundle member)
        # to plugins.enabled and hot-reload, so its tools / views / surfaces go live with
        # NO separate enable step and NO restart (the router hot-mounts, #822). Opt out
        # with PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1 (back to strict install ≠ enable).
        ids = _enabled_ids_from_summary(summary)
        # Snapshot BEFORE the reload: which just-(re)installed plugins were already
        # LIVE — a mounted router (mount registry; survives disable) or a loaded
        # view/router/surface (meta). For those, the reload can't deliver the fresh
        # routes: the re-registered router is dropped in favour of the mounted one
        # (FastAPI can't swap in place), so the OLD code keeps serving (#942).
        fresh = _installed_ids_from_summary(summary)
        mounted = _mounted_router_ids()
        prev_meta = {p.get("id"): p for p in (STATE.plugin_meta or [])}
        stale_after_reload = [pid for pid in fresh if pid in mounted or _has_surface(prev_meta.get(pid))]
        # Same parity for code the reload CAN swap: drop each re-installed plugin's
        # module subtree so tools/middleware re-exec from the fresh checkout (the
        # update route's multi-file fix; a first install is a no-op here).
        for pid in fresh:
            _purge_plugin_modules(pid)

        enabled_now: list[str] = []
        reloaded = False
        enable_error: str | None = None
        if ids and not _install_no_enable():
            cfg = STATE.graph_config
            enabled = list(getattr(cfg, "plugins_enabled", []) or [])
            disabled = [p for p in (getattr(cfg, "plugins_disabled", []) or []) if p not in ids]
            for pid in ids:
                if pid not in enabled:
                    enabled.append(pid)
            from server.agent_init import _apply_settings_changes

            config_updates: dict = {"plugins": {"enabled": enabled, "disabled": disabled}}
            # Seed the bundle's recommended per-plugin config defaults (#1350) — same trust
            # gate as auto-enable. Defaults only: reduce against the current YAML config so an
            # operator value (or a key they've already set) is never clobbered.
            bundle_config = summary.get("config") if "bundle" in summary else None
            if bundle_config:
                from graph.config_io import load_yaml_doc
                from graph.plugins.installer import bundle_config_overlay

                import graph.config_io as _cio

                current = load_yaml_doc(_cio.CONFIG_YAML_PATH)
                overlay = bundle_config_overlay(bundle_config, current if isinstance(current, dict) else {})
                config_updates.update(overlay)

            ok, messages = _apply_settings_changes(config=config_updates)
            if ok:
                reloaded, enabled_now = True, ids
            else:
                # The install itself succeeded (code is on disk + locked); surface the
                # enable-reload failure without 500ing — it can be enabled manually.
                enable_error = "; ".join(messages) or "reload failed"
                log.warning("[plugins] installed %s but auto-enable reload failed: %s", ids, enable_error)

        return {
            "installed": summary,
            "enabled": enabled_now,  # the ids now live
            "reloaded": reloaded,
            # A FIRST install hot-mounts fully live (#822); a force re-install over a
            # live router serves stale routes until restart (#942).
            "restart_recommended": bool(stale_after_reload),
            "enable_error": enable_error,
        }

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
        # A builtin (core runtime infrastructure, e.g. the delegate registry) always
        # loads regardless of plugins.disabled — refuse to disable it rather than write a
        # config entry the loader silently ignores.
        if not want and prev_meta and prev_meta.get("builtin"):
            raise HTTPException(
                status_code=400,
                detail=f"{plugin_id!r} is a built-in plugin and can't be disabled",
            )
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
        restart = bool(not want and _has_surface(prev_meta))
        return {"ok": True, "enabled": want, "reloaded": True, "restart_recommended": restart}

    @app.get("/api/plugins/updates")
    async def _updates():
        """Per-plugin update status (behind / up-to-date / pinned / error).

        Pinned-to-SHA plugins skip the network; the rest ls-remote their ref
        (TTL-cached + timeout-bounded so the poll can't hang). Errors are
        non-fatal per entry — surfaced in each row's ``error``."""
        return {"plugins": await asyncio.to_thread(installer.check_updates)}

    @app.post("/api/plugins/sync")
    async def _sync():
        """Re-clone every locked plugin that's missing on disk (the console's
        "missing" state; previously CLI-only as `python -m server plugin sync`).

        The lock is the source of truth: each missing plugin re-installs at its
        recorded ``resolved_sha`` — fetch, not update, and fetch ≠ enable (ADR
        0027). If anything was fetched and is already in ``plugins.enabled``
        (e.g. a restored data dir whose config still enables it), hot-reload so
        it comes up live — a previously-missing plugin has no mounted router, so
        the hot-mount path applies and no restart is needed."""
        results = await asyncio.to_thread(installer.sync, allow=_sources_allowlist())
        fetched = {r["id"] for r in results if r.get("status") == "installed"}

        reloaded = False
        cfg = STATE.graph_config
        enabled_now = set(getattr(cfg, "plugins_enabled", []) or [])
        if fetched & enabled_now:
            from server.agent_init import _apply_settings_changes

            ok, messages = _apply_settings_changes(
                config={
                    "plugins": {
                        "enabled": sorted(enabled_now),
                        "disabled": list(getattr(cfg, "plugins_disabled", []) or []),
                    }
                },
            )
            if not ok:
                # The fetch itself succeeded — surface the reload failure per row
                # semantics rather than 500ing (mirrors the install route).
                return {"plugins": results, "reloaded": False, "reload_error": "; ".join(messages) or "reload failed"}
            reloaded = True
        return {"plugins": results, "reloaded": reloaded, "reload_error": None}

    @app.post("/api/plugins/{plugin_id}/update")
    async def _update(plugin_id: str):
        """Pull the latest code for an installed plugin at its recorded ref, then
        hot-reload via the SAME path the enable toggle uses so the new code mounts.

        Re-installs ``source_url`` at ``requested_ref`` (force) — this rewrites the
        lock with the new ``resolved_sha``. If the plugin is currently ENABLED we
        reload (so tools/middleware/MCP rebuild and the router re-mounts, #822); if
        it's installed-but-disabled we just re-install (nothing to reload yet).
        """
        entry = next((e for e in installer.list_installed() if e.get("id") == plugin_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} is not installed")
        source_url = entry.get("source_url", "")
        if not source_url:
            raise HTTPException(
                status_code=400,
                detail=f"plugin {plugin_id!r} has no source_url — cannot update",
            )
        ref = entry.get("requested_ref", "") or None
        try:
            summary = await asyncio.to_thread(
                installer.install, source_url, ref, force=True, by="console", allow=_sources_allowlist()
            )
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cfg = STATE.graph_config
        is_enabled = plugin_id in (getattr(cfg, "plugins_enabled", []) or [])
        meta = next((p for p in (STATE.plugin_meta or []) if p.get("id") == plugin_id), None)

        reloaded = False
        if is_enabled:
            # Force a genuinely fresh import of the just-pulled code before the
            # reload — what makes UPDATE deliver fresh code for a multi-file plugin
            # where the enable path's hot-mount alone wouldn't.
            _purge_plugin_modules(plugin_id)

            # Reload through the enable route's path so the freshly pulled code
            # hot-mounts (router re-mount, tools/middleware/MCP rebuild — #822).
            from server.agent_init import _apply_settings_changes

            enabled = list(getattr(cfg, "plugins_enabled", []) or [])
            disabled = list(getattr(cfg, "plugins_disabled", []) or [])
            ok, messages = _apply_settings_changes(
                config={"plugins": {"enabled": enabled, "disabled": disabled}},
            )
            if not ok:
                raise HTTPException(status_code=500, detail="; ".join(messages) or "reload failed")
            reloaded = True

        # FastAPI can't swap an already-mounted router in place, so a view/route-
        # contributing plugin's OLD route lingers until a process restart — flag it.
        # The mount registry catches the disabled-but-still-mounted case meta misses.
        return {
            "ok": True,
            "id": plugin_id,
            "version": summary.get("version"),
            "resolved_sha": summary.get("resolved_sha"),
            "reloaded": reloaded,
            "restart_recommended": bool(_has_surface(meta) or plugin_id in _mounted_router_ids()),
        }

    @app.delete("/api/plugins/{plugin_id}")
    async def _uninstall(plugin_id: str, purge: bool = False):
        # purge=true also removes the plugin's config section + secrets (ADR 0027).
        try:
            report = installer.uninstall(plugin_id, purge=purge)
        except installer.InstallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **report}
