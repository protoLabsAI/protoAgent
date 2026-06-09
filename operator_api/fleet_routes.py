"""Fleet control-plane API (ADR 0042 slice 2).

The endpoints the CLI (`python -m server fleet`) and the desktop GUI panels both
drive — list / create / start / stop agents, and list archetypes for the new-agent
picker. Mounted by ``register_fleet_routes(app)``. The reverse proxy (in-place switch
of the *active* agent) is a separate slice; these are the lifecycle + catalog routes.

Errors degrade to HTTP 400 with a readable message (never a 500), so a panel can show
it inline. Blocking work (a bundle clone on create) runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("protoagent.server")


def register_fleet_routes(app) -> None:
    from fastapi import Body, HTTPException

    from graph.fleet import supervisor
    from graph.workspaces import manager

    @app.get("/api/fleet")
    async def _list_fleet():
        """Every workspace agent + live status (running/stopped, port, pid, bundle)."""
        return {"agents": supervisor.status()}

    @app.post("/api/fleet")
    async def _create_agent(body: dict = Body(...)):
        """Create an agent (optionally from a bundle archetype) and start it.

        Body: ``{name, bundle?: <git-url>, port?: int, start?: bool=true,
        shared_skills?: bool}``. A blank ``bundle`` is the built-in **Basic** archetype.
        """
        name = str(body.get("name", "")).strip()
        bundle = (str(body.get("bundle") or "").strip()) or None
        port = body.get("port")
        start = bool(body.get("start", True))
        shared = bool(body.get("shared_skills", False))
        try:
            # create() may clone+install a bundle (subprocess) — keep it off the loop.
            ws = await asyncio.to_thread(
                manager.create, name, bundle=bundle, port=port, shared_skills=shared)
            agent = supervisor.start(name) if start else {
                "name": name, "id": ws["id"], "port": ws["port"], "running": False}
            return {"ok": True, "agent": agent, "installed": ws.get("installed", [])}
        except (manager.WorkspaceError, supervisor.FleetError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/{name}/start")
    async def _start_agent(name: str):
        try:
            return {"ok": True, "agent": supervisor.start(name)}
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/{name}/stop")
    async def _stop_agent(name: str):
        try:
            return {"ok": True, **supervisor.stop(name)}
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/fleet/{name}")
    async def _remove_agent(name: str, purge: bool = False):
        try:
            try:
                supervisor.stop(name)  # stop if running; ignore if not
            except supervisor.FleetError:
                pass
            return {"ok": True, **manager.remove(name, purge=purge)}
        except manager.WorkspaceError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/archetypes")
    async def _list_archetypes():
        """Starter agent types for the new-agent picker: the built-in **Basic** +
        every installed bundle's ``archetype:`` metadata."""
        return {"archetypes": _archetypes()}


def _archetypes() -> list[dict]:
    """Built-in Basic + installed-bundle archetypes (cached in plugins.lock)."""
    out = [{
        "id": "basic", "label": "Basic", "icon": "Sparkles", "bundle": None,
        "blurb": "A blank-slate agent — the core loop + built-in tools, no plugins.",
    }]
    try:
        from graph.plugins.installer import _read_lock
        for b in (_read_lock().get("bundles") or []):
            arch = b.get("archetype") or {}
            if arch.get("label"):
                out.append({
                    "id": b.get("id"), "label": arch.get("label"),
                    "icon": arch.get("icon", "Package"), "blurb": arch.get("blurb", ""),
                    "bundle": b.get("source_url"),
                })
    except Exception:  # noqa: BLE001 — archetype discovery is best-effort
        log.warning("[fleet] archetype discovery failed", exc_info=True)
    return out
