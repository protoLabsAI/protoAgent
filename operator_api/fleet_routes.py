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

from fastapi import Request, WebSocket  # module-level so the stringized `request: Request` /
# `ws: WebSocket` annotations on the proxy routes resolve
# (function-local imports don't, under `from __future__ import annotations`).

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
        return {"agents": await asyncio.to_thread(supervisor.status)}

    @app.post("/api/fleet/remotes")
    async def _add_remote(req: dict):
        """Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I): it gets a
        slug window like a local peer, with this hub reverse-proxying its console + A2A. An
        optional bearer ``token`` is stored for the proxy to attach (never returned)."""
        try:
            rec = supervisor.add_remote(
                str((req or {}).get("name", "")),
                str((req or {}).get("url", "")),
                token=str((req or {}).get("token", "") or ""),
            )
            # Probe the new remote's agent card immediately (off the loop — it's a network
            # call) so the response can warn at register time. We DON'T reject an unreachable
            # peer — deferred registration is intentional (it can come online later); the
            # caller just learns `reachable:false` now instead of waiting for the next poll.
            reachable, version = await asyncio.to_thread(supervisor.probe_remote, rec["id"])
            return {"ok": True, "agent": rec, "reachable": reachable, "version": version}
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/fleet/remotes/{ident}")
    async def _update_remote(ident: str, req: dict):
        """Edit a remote member's ``url`` / ``token`` / display ``name`` in place (ADR 0042 §I).

        Omitted fields are left as-is; ``token: ""`` clears the stored bearer (a rotated/wrong
        token is fixed by PATCHing the new one — the recovery path when a proxied member 401s).
        The id — and so the slug + open windows — never changes. Re-probes so the response
        reports fresh reachability, same shape as add. 400 on a bad url/name/collision."""
        body = req or {}
        try:
            rec = await asyncio.to_thread(
                supervisor.update_remote,
                ident,
                name=body.get("name"),
                url=body.get("url"),
                token=body.get("token"),
            )
            reachable, version = await asyncio.to_thread(supervisor.probe_remote, rec["id"])
            return {"ok": True, "agent": rec, "reachable": reachable, "version": version}
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

    @app.api_route("/agents/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _agent_proxy(slug: str, path: str, request: Request):
        """Reverse-proxy the console to a specific agent BY SLUG (ADR 0042 slug routing).

        The slug lives in the console URL (``/app/agent/<slug>/``), so each window targets its
        own agent — two agents can be open in two windows at once, and a reload can't desync
        (the URL is the source of truth). ``host`` = this instance; any other slug resolves to
        its workspace port via the supervisor.
        """
        return await proxy.forward_to(slug, request, path)

    @app.websocket("/agents/{slug}/{path:path}")
    async def _agent_ws_proxy(ws: WebSocket, slug: str, path: str):
        """Reverse-proxy a WebSocket to the agent by slug (#883). Same slug routing as the
        HTTP proxy above, but for WS upgrades — so a plugin's live socket (agent_browser's
        viewport/feed) traverses the hub instead of showing "Disconnected" behind it."""
        await proxy.forward_ws(slug, ws, path)

    @app.post("/api/fleet")
    async def _create_agent(body: dict = Body(...)):
        """Create an agent (optionally from a bundle archetype) and start it.

        Body: ``{name, bundle?: <git-url>, soul?: str, port?: int, start?: bool=true,
        shared_skills?: bool, inherit_config?: bool=true, inputs?: {key: value},
        secrets?: [{key, value}]}``. ``soul`` is the archetype's base SOUL.md (persona), written
        into the workspace so a bundle agent gets its persona too. A blank ``bundle`` is the
        built-in **Basic** archetype. By default a new agent is a **blank agent with the host's
        model config + secrets popped over** (the gateway only — NOT the host's plugins/skills),
        so it boots ready-to-chat. Set ``inherit_config: false`` for a fully blank agent you'll
        set up.

        ``inputs`` are operator-supplied values for the bundle's MCP ``${input}`` placeholders
        (#2041) — an entry seeds ENABLED when its required inputs are filled here rather than
        landing visible-but-inert. ``secrets`` are operator-supplied values for the bundle's
        declared secrets, written to the new member's ``secrets.yaml`` under the bundle section.
        Both apply only on the bundle path and are seeded after install; the operator supplies
        them explicitly — nothing is auto-copied from the host's environment.
        """
        name = str(body.get("name", "")).strip()
        bundle = (str(body.get("bundle") or "").strip()) or None
        # Operator-supplied bundle-seed values (#2041): `inputs` fill MCP `${input}` placeholders,
        # `secrets` carry values for the bundle's declared secrets. Coerced to plain str maps/list
        # here so a malformed field degrades to "not supplied" (env-only fallback) rather than 500.
        raw_inputs = body.get("inputs")
        # JSON null means "not provided" — drop it BEFORE str() coercion, or str(None) becomes
        # the truthy literal "None" and bypasses resolve_bundle_mcp_item's env/default fallthrough.
        inputs = {str(k): str(v) for k, v in raw_inputs.items() if v is not None} if isinstance(raw_inputs, dict) else None
        raw_secrets = body.get("secrets")
        secrets = [s for s in raw_secrets if isinstance(s, dict)] if isinstance(raw_secrets, list) else None
        # The archetype's base SOUL.md (the persona picked in the new-agent picker), written
        # into the workspace so a bundle agent arrives WITH its persona, not just its tools.
        soul = (str(body.get("soul") or "").strip()) or None
        port = body.get("port")
        start = bool(body.get("start", True))
        shared = bool(body.get("shared_skills", False))
        # Carry the host's MODEL only (gateway) so a new agent works immediately without inheriting
        # its plugins — only if the host is actually configured (fresh host → plain blank template).
        inherit_model = None
        if bool(body.get("inherit_config", True)):
            from graph.config_io import config_yaml_path

            cfg_yaml = config_yaml_path()
            if cfg_yaml.exists():
                inherit_model = str(cfg_yaml.parent)
        try:
            # create() may overlay the host model + install a bundle (subprocess) — off the loop.
            ws = await asyncio.to_thread(
                manager.create,
                name,
                bundle=bundle,
                port=port,
                shared_skills=shared,
                inherit_model=inherit_model,
                soul=soul,
                inputs=inputs,
                secrets=secrets,
            )
            agent = (
                (await asyncio.to_thread(supervisor.start, name))
                if start
                else {"name": name, "id": ws["id"], "port": ws["port"], "running": False}
            )
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

    @app.get("/api/archetypes/{archetype_id}/preview")
    async def _archetype_preview(archetype_id: str):
        """What picking this archetype actually sets up: for bundle-backed
        archetypes, the bundle's members with each one's identity, skills,
        pip deps, and capabilities — enumerated WITHOUT installing (read-only
        peek, TTL-cached). Code-free archetypes return ``bundle: null``; the
        SOUL text is already in the list payload."""
        record = next((a for a in _archetypes() if a.get("id") == archetype_id), None)
        if record is None:
            raise HTTPException(404, f"unknown archetype: {archetype_id}")
        if not record.get("bundle"):
            return {"id": archetype_id, "bundle": None}
        from ops import plugins as plugin_ops

        try:
            peek = await plugin_ops.peek_bundle(record["bundle"])
        except Exception as exc:  # noqa: BLE001 — network/git failure → clean 502
            raise HTTPException(502, f"could not read bundle {record['bundle']}: {exc}")
        return {"id": archetype_id, "bundle": peek}


def _norm_url(u: str | None) -> str:
    """Canonicalize a git URL for dedupe (drop trailing ``.git`` / ``/``, lowercase) —
    the same normalization the plugin catalog uses to match install state by URL."""
    import re

    return re.sub(r"\.git$", "", (u or "").strip().rstrip("/")).lower()


# Last-resort archetypes if ``archetype-catalog.json`` is missing or unreadable — the two
# code-free personas, so the picker + wizard always work even on a broken/forked config.
_FALLBACK_ARCHETYPES = [
    {
        "id": "basic",
        "label": "Basic",
        "icon": "Sparkles",
        "bundle": None,
        "blurb": "A blank-slate agent — the core loop + built-in tools, no plugins.",
        "soul_preset": "base",
    },
    {
        "id": "custom",
        "label": "Custom",
        "icon": "PenLine",
        "bundle": None,
        "blurb": "Write your own — start from a SOUL template and fill it in.",
        "soul_preset": "blank",
    },
]


def _load_archetype_catalog() -> list[dict]:
    """Built-in archetype entries from ``archetype-catalog.json`` — the live config dir
    overrides the bundled seed (a fork adds/removes archetypes with NO code change), same
    lookup order as the plugin/MCP catalogs. Falls back to Basic + Custom if the file is
    absent or malformed, so the new-agent picker + wizard never come up empty-handed."""
    import json

    from infra.paths import instance_paths

    ip = instance_paths()
    for base in (ip.config_dir, ip.bundle_dir):
        f = base / "archetype-catalog.json"
        if f.exists():
            try:
                entries = (json.loads(f.read_text()) or {}).get("archetypes")
                if isinstance(entries, list) and entries:
                    return entries
            except (json.JSONDecodeError, OSError):
                log.warning("[fleet] archetype-catalog.json unreadable at %s", f)
            break  # live dir wins even if broken — don't silently fall through to the seed
    return _FALLBACK_ARCHETYPES


def _archetypes() -> list[dict]:
    """Starter agent types for the new-agent picker + setup wizard (ADR 0042).

    Data-driven: the built-in set comes from ``archetype-catalog.json`` (see
    ``_load_archetype_catalog``), merged with every installed bundle's ``archetype:``
    manifest metadata (cached in ``plugins.lock``). Each archetype carries an optional
    ``soul`` — a base SOUL.md the persona step seeds when the operator picks it: the catalog
    names a ``soul_preset`` file under ``config/soul-presets/`` (resolved here) or an inline
    ``soul``; a bundle declares it inline in its manifest. The whole list is deduped by id +
    bundle URL (a catalog entry for a stack never doubles up with the same installed bundle),
    and ``custom`` is kept LAST.
    """
    from graph.config_io import read_soul_preset

    out: list[dict] = []
    custom: dict | None = None
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for entry in _load_archetype_catalog():
        aid = str(entry.get("id") or "").strip()
        if not aid or aid in seen_ids:
            continue
        soul = entry.get("soul") or (read_soul_preset(str(entry["soul_preset"])) if entry.get("soul_preset") else "")
        bundle = entry.get("bundle") or None
        rec = {
            "id": aid,
            "label": entry.get("label", aid),
            "icon": entry.get("icon", "Package"),
            "bundle": bundle,
            "blurb": entry.get("blurb", ""),
            "soul": soul,
            # Host capabilities this archetype needs to be USEFUL (#2186 follow-on) —
            # e.g. "python_runtime": cowork's document skills route through execute_code,
            # which on the desktop app needs the managed CPython. The new-agent picker
            # warns at choose-time when a requirement isn't provisioned.
            "requires": list(entry.get("requires") or []),
        }
        seen_ids.add(aid)
        if bundle:
            seen_urls.add(_norm_url(bundle))
        if aid == "custom":
            custom = rec  # hold it back so it stays last after bundle archetypes append
        else:
            out.append(rec)

    # Installed bundles that declare `archetype:` metadata self-register as starter types —
    # appended after the catalog, deduped by id + normalized bundle URL so a catalog entry
    # for the same stack (or a bundle listed twice) never produces a duplicate RadioCard.
    try:
        from graph.plugins.installer import _read_lock

        for b in _read_lock().get("bundles") or []:
            arch = b.get("archetype") or {}
            bid = str(b.get("id") or "").strip()
            url = b.get("source_url") or ""
            if not arch.get("label") or not bid:
                continue
            if bid in seen_ids or (url and _norm_url(url) in seen_urls):
                continue
            seen_ids.add(bid)
            if url:
                seen_urls.add(_norm_url(url))
            out.append(
                {
                    "id": bid,
                    "label": arch.get("label"),
                    "icon": arch.get("icon", "Package"),
                    "blurb": arch.get("blurb", ""),
                    "bundle": url or None,
                    "soul": arch.get("soul", ""),
                    # A bundle's archetype: block can declare host requirements too —
                    # same shape as the catalog field (#2186 follow-on).
                    "requires": list(arch.get("requires") or []),
                }
            )
    except Exception:  # noqa: BLE001 — archetype discovery is best-effort
        log.warning("[fleet] archetype discovery failed", exc_info=True)

    if custom is not None:
        out.append(custom)  # the catch-all write-your-own persona, always LAST
    return out
