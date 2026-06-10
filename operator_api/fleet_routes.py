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

from fastapi import Request  # module-level so the stringized `request: Request` annotation
                             # on the proxy route resolves (function-local imports don't,
                             # under `from __future__ import annotations`).

log = logging.getLogger("protoagent.server")


def register_fleet_routes(app) -> None:
    from fastapi import Body, HTTPException

    from graph.fleet import proxy, supervisor
    from graph.workspaces import manager

    @app.get("/api/fleet")
    async def _list_fleet():
        """Every workspace agent + remote member + live status. The focused agent is the URL
        slug now (ADR 0042 slug routing) — no server-side 'active' pointer. Remote probes are
        TTL-cached + refreshed off the loop, so the 3s console poll stays cheap."""
        await asyncio.to_thread(supervisor.refresh_remote_probes)
        return {"agents": supervisor.status()}

    @app.post("/api/fleet/remotes")
    async def _add_remote(req: dict):
        """Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I): it gets a
        slug window like a local peer, with this hub reverse-proxying its console + A2A. An
        optional bearer ``token`` is stored for the proxy to attach (never returned)."""
        try:
            rec = supervisor.add_remote(str((req or {}).get("name", "")),
                                        str((req or {}).get("url", "")),
                                        token=str((req or {}).get("token", "") or ""))
            return {"ok": True, "agent": rec}
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/fleet/remotes/{ident}")
    async def _remove_remote(ident: str):
        """Unregister a remote member (the remote agent itself is untouched)."""
        try:
            return {"ok": True, **supervisor.remove_remote(ident)}
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/fleet/discover")
    async def _discover():
        """Discover OTHER protoAgents on the box + LAN + tailnet (ADR 0042 §I) — candidates to
        add as remote delegates or remote fleet members. Excludes agents already in this fleet
        (+ self + registered remotes)."""
        from urllib.parse import urlparse

        from graph.fleet import discovery
        fleet = supervisor.status()
        known = {("127.0.0.1", a["port"]) for a in fleet if a.get("port")}
        for a in fleet:  # registered remote members aren't 'discoveries' either
            if a.get("remote") and a.get("url"):
                u = urlparse(a["url"])
                if u.hostname and u.port:
                    known.add((u.hostname, u.port))
        try:  # also exclude our own mDNS self-advert (LAN ip + the host's port)
            host_port = next((a["port"] for a in fleet if a.get("host")), None)
            if host_port:
                known.add((discovery._local_ip(), host_port))
        except Exception:  # noqa: BLE001
            pass
        return {"discovered": await discovery.discover(known=known)}

    @app.post("/api/fleet/{name}/activate")
    async def _activate(name: str):
        """Ensure an agent is running + mark it most-recently-active (keep-N-warm). Call this when
        a console window navigates to an agent (ADR 0042 slug routing): it resumes a cold agent
        from its checkpoint, then evicts the least-recently-used agents beyond the warm cap (their
        sessions persist + resume on a later visit). The host is this instance (always up) → no-op.
        """
        try:
            agents = supervisor.status()
            host = next((a for a in agents if a.get("host")), None)
            if host and name in (host["name"], host["id"]):
                return {"ok": True, "evicted": []}
            # A remote member can't be started/evicted from here — reachability is its
            # own deployment's business. No-op so slug navigation stays uniform.
            if any(a.get("remote") and name in (a["name"], a["id"]) for a in agents):
                return {"ok": True, "evicted": []}
            if not supervisor.is_running(name):
                await asyncio.to_thread(supervisor.start, name)  # resume from checkpoint
            supervisor.touch(name)
            # Eviction can busy-wait on a SIGTERM (#6) — off the loop.
            evicted = await asyncio.to_thread(supervisor.enforce_warm_cap, protect=name)
            return {"ok": True, "evicted": evicted}
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.api_route("/agents/{slug}/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _agent_proxy(slug: str, path: str, request: Request):
        """Reverse-proxy the console to a specific agent BY SLUG (ADR 0042 slug routing).

        The slug lives in the console URL (``/app/agent/<slug>/``), so each window targets its
        own agent — two agents can be open in two windows at once, and a reload can't desync
        (the URL is the source of truth). ``host`` = this instance; any other slug resolves to
        its workspace port via the supervisor.
        """
        return await proxy.forward_to(slug, request, path)

    @app.post("/api/fleet")
    async def _create_agent(body: dict = Body(...)):
        """Create an agent (optionally from a bundle archetype) and start it.

        Body: ``{name, bundle?: <git-url>, port?: int, start?: bool=true,
        shared_skills?: bool, inherit_config?: bool=true}``. A blank ``bundle`` is the built-in
        **Basic** archetype. By default a new agent is a **blank agent with the host's model
        config + secrets popped over** (the gateway only — NOT the host's plugins/skills), so it
        boots ready-to-chat. Set ``inherit_config: false`` for a fully blank agent you'll set up.
        """
        name = str(body.get("name", "")).strip()
        bundle = (str(body.get("bundle") or "").strip()) or None
        port = body.get("port")
        start = bool(body.get("start", True))
        shared = bool(body.get("shared_skills", False))
        # Carry the host's MODEL only (gateway) so a new agent works immediately without inheriting
        # its plugins — only if the host is actually configured (fresh host → plain blank template).
        inherit_model = None
        if bool(body.get("inherit_config", True)):
            from graph.config_io import _live_config_dir
            cfg_dir = _live_config_dir()
            if (cfg_dir / "langgraph-config.yaml").exists():
                inherit_model = str(cfg_dir)
        try:
            # create() may overlay the host model + install a bundle (subprocess) — off the loop.
            ws = await asyncio.to_thread(
                manager.create, name, bundle=bundle, port=port, shared_skills=shared,
                inherit_model=inherit_model)
            agent = (await asyncio.to_thread(supervisor.start, name)) if start else {
                "name": name, "id": ws["id"], "port": ws["port"], "running": False}
            return {"ok": True, "agent": agent, "installed": ws.get("installed", [])}
        except (manager.WorkspaceError, supervisor.FleetError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/{name}/start")
    async def _start_agent(name: str):
        try:
            return {"ok": True, "agent": await asyncio.to_thread(supervisor.start, name)}
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/{name}/stop")
    async def _stop_agent(name: str):
        try:
            return {"ok": True, **await asyncio.to_thread(supervisor.stop, name)}  # #6 — off the loop
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/down")
    async def _stop_fleet():
        """Shut down the **entire** fleet (every running agent). Mirrors the CLI's
        ``fleet down`` with no args."""
        stopped = await asyncio.to_thread(supervisor.down)  # busy-waits per agent (#6)
        return {"ok": True, "stopped": [r["name"] for r in stopped]}

    @app.patch("/api/fleet/{name}")
    async def _rename_agent(name: str, req: dict):
        """Rename an agent's DISPLAY name (by id or current name). The id — and so the
        URL slug, the workspace dir and the data scope — never changes; open windows
        and checkpoints survive. A running agent re-reads its identity on restart."""
        new_name = str((req or {}).get("name", "")).strip()
        if not new_name:
            raise HTTPException(400, "name is required")
        try:
            return {"ok": True, **manager.rename(name, new_name)}
        except manager.WorkspaceError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/fleet/{name}")
    async def _remove_agent(name: str, purge: bool = False):
        try:
            try:
                await asyncio.to_thread(supervisor.stop, name)  # stop if running (#6)
            except supervisor.FleetError:
                pass
            # remove() rmtree's the workspace (purge) — also blocking.
            return {"ok": True, **await asyncio.to_thread(manager.remove, name, purge=purge)}
        except manager.WorkspaceError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/archetypes")
    async def _list_archetypes():
        """Starter agent types for the new-agent picker: the built-in **Basic** +
        every installed bundle's ``archetype:`` metadata."""
        return {"archetypes": _archetypes()}


def _archetypes() -> list[dict]:
    """Built-in Basic + installed-bundle archetypes (cached in plugins.lock)."""
    out = [
        {
            "id": "basic", "label": "Basic", "icon": "Sparkles", "bundle": None,
            "blurb": "A blank-slate agent — the core loop + built-in tools, no plugins.",
        },
        {
            # Built-in PM archetype — installed FRESH from the git URL on each create (no pin),
            # so a new PM agent always gets the latest pm-stack.
            "id": "pm-stack", "label": "Project Manager", "icon": "LayoutGrid",
            "bundle": "https://github.com/protoLabsAI/pm-stack",
            "blurb": "Project-management tools + board — clones the latest pm-stack on create.",
        },
    ]
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
