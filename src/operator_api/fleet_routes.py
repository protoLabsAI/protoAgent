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
import re

from fastapi import Request, WebSocket  # module-level so the stringized `request: Request` /
# `ws: WebSocket` annotations on the proxy routes resolve
# (function-local imports don't, under `from __future__ import annotations`).

log = logging.getLogger("protoagent.server")


def register_fleet_routes(app) -> None:
    from fastapi import Body, HTTPException

    from graph.fleet import proxy, supervisor
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    @app.get("/api/fleet")
    async def _list_fleet():
        """Every workspace agent + remote member + live status. The focused agent is the URL
        slug now (ADR 0042 slug routing) — no server-side 'active' pointer. Remote probes are
        TTL-cached + refreshed off the loop, so the 3s console poll stays cheap."""
        await asyncio.to_thread(supervisor.refresh_remote_probes)
        return {"agents": await asyncio.to_thread(supervisor.status)}

    @app.put("/api/fleet/order")
    async def _set_fleet_order(req: dict):
        """Persist the fleet roster DISPLAY order (ADR 0042 hub control-plane). Body:
        ``{order: [<member id>, ...]}`` — a COMPLETE permutation of the current member ids
        (host + local + remote), by IMMUTABLE id (never an editable name/label). The supervisor
        validates it under the state lock against the live roster and rejects a duplicate /
        unknown / missing / malformed list WITHOUT touching the saved order (400); on success,
        subsequent GET /api/fleet reads return members in this order, reconciled as members are
        later added or removed. Hub-only — a member's ``roster.json`` is its own."""
        try:
            order = await fleet_ops.order((req or {}).get("order"))
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "order": order}

    @app.post("/api/fleet/remotes")
    async def _add_remote(req: dict):
        """Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I): it gets a
        slug window like a local peer, with this hub reverse-proxying its console + A2A. An
        optional bearer ``token`` is stored for the proxy to attach (never returned)."""
        try:
            # The op registers AND probes (off the loop) so the response can warn at register
            # time; an unreachable peer is not rejected — deferred registration is intentional.
            out = await fleet_ops.remotes_add(
                str((req or {}).get("name", "")),
                str((req or {}).get("url", "")),
                str((req or {}).get("token", "") or ""),
                (req or {}).get("allow_insecure") is True,
            )
            return {"ok": True, **out}
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/fleet/remotes/pair")
    async def _pair_remote(req: dict):
        """Pair with a remote protoAgent (ADR 0113 D1). Body ``{url, code, name?}``: the hub
        redeems the code (minted on the remote, Settings ▸ Devices / ``protoagent pair``)
        against the remote's ``/api/pairing/claim`` and stores the per-device token it gets
        back as the member's bearer — added, or re-tokened when that URL is already a member
        (the previously-paired device on the remote is then revoked, best effort). ``url`` is
        the agent's BASE URL — a path/query/fragment (e.g. a phone ``#pair=`` link) is a 400.
        Plain ``http://`` off loopback/tailnet is a 400 unless ``allow_insecure: true`` — the
        code and the token would cross the network in cleartext (ADR 0113 D10; the same
        opt-in applies to add/PATCH when a token is being stored).
        Answers the sanitized record + ``reachable`` + ``auth`` (never the token). 400 for a
        bad url/name or an invalid/expired code; 502 when the remote is unreachable or isn't a
        protoAgent that supports pairing. Declared BEFORE the ``{ident}`` routes (no clash —
        those are PATCH/DELETE — but it keeps the literal path obviously first)."""
        body = req or {}
        try:
            out = await fleet_ops.remotes_pair(
                str(body.get("url", "") or ""),
                str(body.get("code", "") or ""),
                (str(body["name"]) if body.get("name") else None),
                body.get("allow_insecure") is True,
            )
            return {"ok": True, **out}
        except supervisor.PairingError as exc:
            raise HTTPException(exc.status, str(exc))
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/fleet/remotes/{ident}")
    async def _update_remote(ident: str, req: dict):
        """Edit a remote member's ``url`` / ``token`` / display ``name`` in place (ADR 0042 §I).

        Omitted fields are left as-is; ``token: ""`` clears the stored bearer (a rotated/wrong
        token is fixed by PATCHing the new one — the recovery path when a proxied member 401s).
        A ``url`` on a different origin (scheme/host/port) with no ``token`` in the same body
        CLEARS the stored bearer — it was issued by the old host and must not be presented to
        the new one — and the response says ``token_cleared: true`` (re-pair or pass a token).
        The id — and so the slug + open windows — never changes. Re-probes so the response
        reports fresh reachability, same shape as add. 400 on a bad url/name/collision."""
        body = req or {}
        try:
            out = await fleet_ops.remotes_update(
                ident,
                name=body.get("name"),
                url=body.get("url"),
                token=body.get("token"),
                allow_insecure=body.get("allow_insecure") is True,
            )
            return {"ok": True, **out}
        except (supervisor.FleetError, manager.WorkspaceError) as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/fleet/remotes/{ident}")
    async def _remove_remote(ident: str):
        """Unregister a remote member (the remote agent itself is untouched)."""
        try:
            return {"ok": True, **await fleet_ops.remotes_remove(ident)}
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

        Body: ``{name, bundle?: <git-url>, ref?: <tag|branch|sha>, soul?: str, port?: int, start?: bool=true,
        shared_skills?: bool, inherit_config?: bool=true, inputs?: {key: value},
        secrets?: [{key, value}], config_inputs?: {dotted_key: value}}``.
        ``soul`` is the archetype's base SOUL.md (persona), written
        into the workspace so a bundle agent gets its persona too. A blank ``bundle`` is the
        built-in **Basic** archetype. By default a new agent is a **blank agent with the host's
        model connections + credentials popped over** (provider registry, gateway secrets,
        and the box-shared OAuth login — NOT the host's plugins/skills), so it boots
        ready-to-chat. A legacy instance-local OAuth login is transferred into the one
        machine-shared box store, never copied. Set ``inherit_config: false`` for a fully
        blank agent you'll set up.

        ``inputs`` are operator-supplied values for the bundle's MCP ``${input}`` placeholders
        (#2041) — an entry seeds ENABLED when its required inputs are filled here rather than
        landing visible-but-inert. ``secrets`` are operator-supplied values for the bundle's
        declared secrets, written to the new member's ``secrets.yaml`` under the bundle section.
        ``config_inputs`` (#2934) are the operator's answers to the bundle's declared
        ``config_inputs:`` prompts, written into the member's config at the declared dotted
        key paths. All apply only on the bundle path and are seeded after install; the
        operator supplies them explicitly — nothing is auto-copied from the host's environment.
        """
        name = str(body.get("name", "")).strip()
        bundle = (str(body.get("bundle") or "").strip()) or None
        # The tag / branch / SHA to install the bundle at — the new-agent "From a bundle URL"
        # source pins one (blank = the default branch). Checked here, before any workspace
        # exists, with the installer's own validator; only meaningful with a bundle.
        ref = (str(body.get("ref") or "").strip()) or None
        if ref and not bundle:
            raise HTTPException(400, "`ref` needs a `bundle` to pin")
        if ref:
            from graph.plugins import installer

            try:
                installer._validate_ref(ref)
            except installer.InstallError as exc:
                raise HTTPException(400, str(exc))
        # Operator-supplied bundle-seed values (#2041): `inputs` fill MCP `${input}` placeholders,
        # `secrets` carry values for the bundle's declared secrets. Coerced to plain str maps/list
        # here so a malformed field degrades to "not supplied" (env-only fallback) rather than 500.
        raw_inputs = body.get("inputs")
        # JSON null means "not provided" — drop it BEFORE str() coercion, or str(None) becomes
        # the truthy literal "None" and bypasses resolve_bundle_mcp_item's env/default fallthrough.
        inputs = {str(k): str(v) for k, v in raw_inputs.items() if v is not None} if isinstance(raw_inputs, dict) else None
        raw_secrets = body.get("secrets")
        secrets = [s for s in raw_secrets if isinstance(s, dict)] if isinstance(raw_secrets, list) else None
        # Operator answers to the bundle's declared config_inputs prompts (#2934). Values
        # pass through untouched (a boolean toggle must arrive as a bool — the seed helper
        # coerces per declared type); JSON nulls drop so "not answered" falls through to
        # the declared default.
        raw_ci = body.get("config_inputs")
        config_inputs = {str(k): v for k, v in raw_ci.items() if v is not None} if isinstance(raw_ci, dict) else None
        # The archetype's base SOUL.md (the persona picked in the new-agent picker), written
        # into the workspace so a bundle agent arrives WITH its persona, not just its tools.
        soul = (str(body.get("soul") or "").strip()) or None
        # The archetype's capability contract (#2277) — the tools its persona commits to
        # performing. Recorded on the workspace so the member can check its own doctrine
        # against the tools that actually bound, instead of narrating actions it can't do.
        raw_req = body.get("requires_tools")
        requires_tools = [str(t) for t in raw_req if str(t).strip()] if isinstance(raw_req, list) else None
        port = body.get("port")
        start = bool(body.get("start", True))
        shared = bool(body.get("shared_skills", False))
        try:
            # The op carries the orchestration (host model overlay + create + start), off the
            # loop — the offline CLI verb and this route share it (#3471).
            out = await fleet_ops.create(
                name,
                bundle=bundle,
                ref=ref,
                soul=soul,
                port=port,
                start=start,
                shared_skills=shared,
                inherit_config=bool(body.get("inherit_config", True)),
                inputs=inputs,
                secrets=secrets,
                config_inputs=config_inputs,
                requires_tools=requires_tools,
            )
            return {"ok": True, **out}
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
            res = await asyncio.to_thread(supervisor.stop, name)  # #6 — off the loop
        except supervisor.FleetError as exc:
            raise HTTPException(400, str(exc))
        # `ok` tracks the OUTCOME, not just "the request was handled" (#2286) — a caller
        # that only checks `ok` must not read a survived process as a successful stop.
        return {"ok": bool(res.get("stopped")), **res}

    @app.post("/api/fleet/down")
    async def _stop_fleet():
        """Shut down the **entire** fleet (every running agent). Mirrors the CLI's
        ``fleet down`` with no args."""
        results = await asyncio.to_thread(supervisor.down)  # busy-waits per agent (#6)
        stopped = [r["name"] for r in results if r.get("stopped")]
        failed = [{"name": r["name"], "reason": r.get("reason", "")} for r in results if not r.get("stopped")]
        return {"ok": not failed, "stopped": stopped, **({"failed": failed} if failed else {})}

    @app.patch("/api/fleet/{name}")
    async def _rename_agent(name: str, req: dict):
        """Rename an agent's DISPLAY name (by id or current name). The id — and so the
        URL slug, the workspace dir and the data scope — never changes; open windows
        and checkpoints survive. A running agent re-reads its identity on restart."""
        try:
            return {"ok": True, **await fleet_ops.rename(name, (req or {}).get("name"))}  # the op owns the check: None/blank → 400
        except manager.WorkspaceError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/fleet/{name}")
    async def _remove_agent(name: str, purge: bool = False):
        try:
            # stop if running, then retire or purge (rmtree) — all blocking, in the op
            return {"ok": True, **await fleet_ops.remove(name, purge=purge)}
        except manager.WorkspaceBusy as exc:
            # Partial, retryable: the member IS stopped, only its workspace survived (#2583).
            # 409, not the 500 an escaping OSError used to produce and not the 400 a rejected
            # request gets — an operator needs to know a destructive op half-landed, and that
            # repeating it is both safe and the fix. Must precede the WorkspaceError arm below,
            # which is its base class.
            raise HTTPException(409, str(exc))
        except manager.WorkspaceError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/archetypes")
    async def _list_archetypes(include_held: bool = False):
        """Starter agent types for the new-agent picker: the built-in **Basic** +
        every installed bundle's ``archetype:`` metadata. ``?include_held=1`` also returns
        the catalog's ``held`` entries (archetypes still being tested), each marked
        ``held: true`` — only the console's opt-in "Show preview archetypes" asks for them."""
        return {"archetypes": _archetypes(include_held=include_held)}

    @app.get("/api/archetypes/from-url")
    async def _archetype_from_url(url: str = "", ref: str = ""):
        """An archetype from a bundle's git URL (+ optional ref) that ISN'T in the catalog —
        the new-agent picker's "From a bundle URL" source. A read-only peek (nothing
        installs): the bundle's ``archetype:`` block shaped like a ``/api/archetypes`` row,
        plus the same ``bundle`` peek ``/preview`` serves (members + refs, builtins,
        config_inputs) so the console can show what it installs before Create. ``trusted``
        says whether the source is official/acked (ADR 0071 D3) — the console asks for an
        explicit "I trust this repository" when it isn't."""
        from graph.plugins import installer
        from ops import plugins as plugin_ops

        try:
            clean_url = _validate_bundle_url(url)
            clean_ref = ref.strip() or None
            if clean_ref:
                installer._validate_ref(clean_ref)
        except (ValueError, installer.InstallError) as exc:
            raise HTTPException(400, str(exc))
        try:
            peek = await plugin_ops.peek_bundle(clean_url, clean_ref)
        except Exception as exc:  # noqa: BLE001 — network/git failure → clean 502
            raise HTTPException(502, f"could not read bundle {clean_url}: {exc}")
        rec = _peek_archetype_record(clean_url, peek)
        if clean_ref:
            rec["ref"] = clean_ref
        return {"id": rec["id"], "archetype": rec, "bundle": peek, **_source_trust(clean_url)}

    @app.get("/api/archetypes/{archetype_id}/preview")
    async def _archetype_preview(archetype_id: str):
        """What picking this archetype actually sets up: for bundle-backed
        archetypes, the bundle's members with each one's identity, skills,
        pip deps, and capabilities — enumerated WITHOUT installing (read-only
        peek, TTL-cached). Code-free archetypes return ``bundle: null``; the
        SOUL text is already in the list payload."""
        # Held entries resolve too: the picker only shows them when the operator opted in,
        # and this is a read-only peek.
        record = next((a for a in _archetypes(include_held=True) if a.get("id") == archetype_id), None)
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
    return re.sub(r"\.git$", "", (u or "").strip().rstrip("/")).lower()


def _norm_tier(value: object) -> str:
    """Picker placement for an archetype card (ADR 0042). ``"advanced"`` files the card
    under the picker's collapsed "Advanced (N)" section; anything else — including a missing
    tag — is ``"standard"`` and renders inline. Normalized here so both the catalog and a
    bundle self-registration hand the console one of exactly two values."""
    return "advanced" if str(value or "").strip().lower() == "advanced" else "standard"


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


def _read_archetype_catalog_doc() -> dict | None:
    """The winning ``archetype-catalog.json`` parsed — the live config dir overrides the
    bundled seed (a fork adds/removes archetypes with NO code change), same lookup order as
    the plugin/MCP catalogs. None when the file is absent or malformed; the live dir wins
    even when broken (never silently falls through to the seed)."""
    import json

    from infra.paths import instance_paths

    ip = instance_paths()
    for base in (ip.config_dir, ip.bundle_dir):
        f = base / "archetype-catalog.json"
        if f.exists():
            try:
                doc = json.loads(f.read_text(encoding="utf-8")) or {}
                return doc if isinstance(doc, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                log.warning("[fleet] archetype-catalog.json unreadable at %s", f)
            return None
    return None


def _load_archetype_catalog() -> list[dict]:
    """Built-in archetype entries from ``archetype-catalog.json`` (see
    ``_read_archetype_catalog_doc``). Falls back to Basic + Custom if the file is absent or
    malformed, so the new-agent picker + wizard never come up empty-handed."""
    entries = (_read_archetype_catalog_doc() or {}).get("archetypes")
    if isinstance(entries, list) and entries:
        return entries
    return _FALLBACK_ARCHETYPES


def _load_held_archetypes() -> list[dict]:
    """The catalog's ``held`` entries — archetypes parked out of the picker until they're
    tested. Same shape as ``archetypes``; served only on an explicit ``include_held`` ask
    (the console's "Show preview archetypes" opt-in), never by default."""
    held = (_read_archetype_catalog_doc() or {}).get("held")
    return [e for e in held if isinstance(e, dict)] if isinstance(held, list) else []


def _catalog_record(entry: dict, aid: str) -> dict:
    """One catalog (or held) entry shaped as the ``/api/archetypes`` row the console reads."""
    from graph.config_io import read_soul_preset

    soul = entry.get("soul") or (read_soul_preset(str(entry["soul_preset"])) if entry.get("soul_preset") else "")
    return {
        "id": aid,
        "label": entry.get("label", aid),
        "icon": entry.get("icon", "Package"),
        "bundle": entry.get("bundle") or None,
        "blurb": entry.get("blurb", ""),
        "soul": soul,
        # Picker placement (ADR 0042): "advanced" archetypes collapse under the picker's
        # "Advanced (N)" toggle; a missing tag normalizes to "standard" (renders inline).
        "tier": _norm_tier(entry.get("tier")),
        # Host capabilities this archetype needs to be USEFUL (#2186 follow-on) —
        # e.g. "python_runtime": cowork's document skills route through execute_code,
        # which on the desktop app needs the managed CPython. The new-agent picker
        # warns at choose-time when a requirement isn't provisioned.
        "requires": list(entry.get("requires") or []),
        # Capability contract (#2277): the tools this archetype's PERSONA commits to
        # performing. Recorded on the created workspace so the member can check its own
        # doctrine against the tools that actually bound — a preset that says it files
        # issues while `github.write` defaults false otherwise narrates the filing.
        "requires_tools": list(entry.get("requires_tools") or []),
    }


def _bundle_archetype_record(bid: str, url: str, arch: dict) -> dict:
    """A bundle's ``archetype:`` manifest block shaped as an ``/api/archetypes`` row — shared
    by installed-bundle self-registration and the "From a bundle URL" peek."""
    from graph.config_io import read_soul_preset

    # A bundle declares its persona inline (`soul`) or names a host preset
    # (`soul_preset`) — the same pair the catalog supports (#2715; before,
    # only inline worked here and a preset-naming bundle silently fell back
    # to the base persona via the console's personaSoul()). An unknown
    # preset name resolves to "" — warn, because the operator sees the
    # fallback persona with no other signal.
    soul = str(arch.get("soul") or "")
    if not soul and arch.get("soul_preset"):
        soul = read_soul_preset(str(arch["soul_preset"]))
        if not soul:
            log.warning(
                "[fleet] bundle %s names soul_preset %r — not found on this host; "
                "the picker will fall back to the base persona",
                bid,
                arch["soul_preset"],
            )
    return {
        "id": bid,
        "label": arch.get("label"),
        "icon": arch.get("icon", "Package"),
        "blurb": arch.get("blurb", ""),
        "bundle": url or None,
        "soul": soul,
        # A bundle can file itself under the picker's "Advanced" toggle too —
        # same optional tag as the catalog field, normalized to standard/advanced.
        "tier": _norm_tier(arch.get("tier")),
        # A bundle's archetype: block can declare host requirements too —
        # same shape as the catalog field (#2186 follow-on).
        "requires": list(arch.get("requires") or []),
        # A bundle's archetype: block declares its capability contract the
        # same way (#2277).
        "requires_tools": list(arch.get("requires_tools") or []),
    }


# A bundle URL the "From a bundle URL" source accepts: an https git-host repo
# (`https://github.com/owner/repo`, optional `.git` / trailing slash; nested groups OK for
# GitLab-style hosts) or the scp-style SSH form (`git@github.com:owner/repo.git`). Stricter
# than the installer's scheme check on purpose — this is a pasted URL, not a local path.
_BUNDLE_URL_RE = re.compile(
    r"^(?:https://[A-Za-z0-9.-]+(?::\d+)?/|git@[A-Za-z0-9.-]+:)"
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?$"
)


def _validate_bundle_url(url: str) -> str:
    """The trimmed bundle URL, or ``ValueError`` with a readable reason."""
    u = (url or "").strip()
    if not u:
        raise ValueError("a bundle URL is required")
    if not _BUNDLE_URL_RE.match(u) or any(seg in (".", "..") for seg in re.split(r"[/:]", u)):
        raise ValueError(
            f"not a git repository URL: {u!r} — use https://github.com/<owner>/<repo> (or git@host:owner/repo.git)"
        )
    return u


def _peek_archetype_record(url: str, peek: dict) -> dict:
    """The ``/api/archetypes`` row for a peeked bundle URL: its ``archetype:`` block, with
    the label / blurb falling back to the bundle's own name / description (or the repo name)
    so a bundle without an archetype block still reads as a card."""
    arch = dict(peek.get("archetype") or {})
    slug = re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    members = peek.get("members") or []
    first = members[0] if members and isinstance(members[0], dict) else {}
    bid = str(peek.get("id") or first.get("id") or slug)
    arch["label"] = arch.get("label") or peek.get("name") or first.get("name") or slug
    arch["blurb"] = arch.get("blurb") or peek.get("description") or first.get("description") or ""
    return _bundle_archetype_record(bid, url, arch)


def _source_trust(url: str) -> dict:
    """Whether ``url`` is an official / already-acked plugin source (ADR 0071 D3) — the same
    predicate the install route's consent gate uses — plus its normalized display form."""
    from graph.plugins.trust import normalize_source, source_trusted
    from runtime.state import STATE

    cfg = STATE.graph_config
    trusted = source_trusted(
        url,
        official=getattr(cfg, "plugins_sources_official", None) if cfg else None,
        acked=getattr(cfg, "plugins_sources_acked", None) if cfg else None,
        trust_unverified=(getattr(cfg, "plugins_trust_unverified", False) is True) if cfg else False,
    )
    return {"trusted": bool(trusted), "source": normalize_source(url)}


def _archetypes(*, include_held: bool = False) -> list[dict]:
    """Starter agent types for the new-agent picker + setup wizard (ADR 0042).

    Data-driven: the built-in set comes from ``archetype-catalog.json`` (see
    ``_load_archetype_catalog``), merged with every installed bundle's ``archetype:``
    manifest metadata (cached in ``plugins.lock``). Each archetype carries an optional
    ``soul`` — a base SOUL.md the persona step seeds when the operator picks it: the catalog
    names a ``soul_preset`` file under ``config/soul-presets/`` (resolved here) or an inline
    ``soul``; a bundle declares it inline in its manifest. The whole list is deduped by id +
    bundle URL (a catalog entry for a stack never doubles up with the same installed bundle),
    and ``custom`` is kept LAST. ``include_held`` appends the catalog's ``held`` entries
    (``held: true``) before Custom — the picker's opt-in preview archetypes.
    """
    out: list[dict] = []
    custom: dict | None = None
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for entry in _load_archetype_catalog():
        aid = str(entry.get("id") or "").strip()
        if not aid or aid in seen_ids:
            continue
        rec = _catalog_record(entry, aid)
        bundle = rec["bundle"]
        seen_ids.add(aid)
        if bundle:
            seen_urls.add(_norm_url(bundle))
        # A renamed bundle repo keeps resolving for installs pinned at the OLD URL
        # (GitHub redirects), but URL-dedupe compares strings — without the alias the
        # catalog card and the self-registered install would double up in the picker
        # (the 2026-08-19 *-stack → *-archetype renames). Dedupe-only: never served.
        for alias in entry.get("bundle_aliases") or []:
            seen_urls.add(_norm_url(str(alias)))
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
            out.append(_bundle_archetype_record(bid, url, arch))
    except Exception:  # noqa: BLE001 — archetype discovery is best-effort
        log.warning("[fleet] archetype discovery failed", exc_info=True)

    # Held catalog entries (archetypes still being tested) — only on an explicit ask, each
    # flagged so the picker can badge it "Preview". Deduped like the rest: once the bundle is
    # installed (or promoted into `archetypes`), the regular row wins and the badge goes.
    if include_held:
        for entry in _load_held_archetypes():
            aid = str(entry.get("id") or "").strip()
            if not aid or aid in seen_ids or aid == "custom":
                continue
            rec = _catalog_record(entry, aid)
            if rec["bundle"] and _norm_url(rec["bundle"]) in seen_urls:
                continue
            seen_ids.add(aid)
            if rec["bundle"]:
                seen_urls.add(_norm_url(rec["bundle"]))
            out.append({**rec, "held": True})

    if custom is not None:
        out.append(custom)  # the catch-all write-your-own persona, always LAST
    return out
