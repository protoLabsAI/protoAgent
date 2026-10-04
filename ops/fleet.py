"""Fleet ops (ADR 0075 D2) — the fleet's lifecycle AND its management, as ops.

Thin ops over ``graph.fleet.supervisor`` / ``graph.workspaces.manager`` — the neutral shared
core the fleet CLI (`protoagent fleet …`), the ``/api/fleet`` routes and the deck all
project. They exist so the whole fleet surface is in the op registry: enumerated by
``GET /api/operations``, callable over the operator MCP, each with the right read/write
metadata (``status`` reads; everything else mutates). The underlying calls are blocking
(subprocess + file state), so they run off the loop.

The management ops (#3471) carry the orchestration the routes used to hold inline —
create = overlay the host's model connections + ``manager.create`` + optional start;
remove = stop if running, then retire or purge; a remote add/update probes the peer so the
caller learns ``reachable`` up front — so ``operator_api/fleet_routes.py`` and the offline
CLI verbs share ONE implementation ("one operation, three projections"). Domain errors
(``manager.WorkspaceError`` / ``WorkspaceBusy``, ``supervisor.FleetError``) propagate as
they are; each projection maps them (the route: 400, 409 for a busy workspace).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ops import op


@op(name="fleet.up", mutates=True, summary="Start fleet member agents (named, or all workspaces) as background processes.")
async def up(names: list[str] | None = None) -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.up, names)


@op(name="fleet.down", mutates=True, summary="Stop fleet member agents (named, or all running).")
async def down(names: list[str] | None = None) -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.down, names)


@op(name="fleet.status", mutates=False, summary="List fleet members (host + workspaces + remotes) with live status.")
async def status() -> list[dict]:
    from graph.fleet import supervisor

    return await asyncio.to_thread(supervisor.status)


# ── management (#3471): shared orchestration, sync, run off the loop by the ops ──


def _inherit_model_source(inherit_config: bool) -> str | None:
    """The host's config dir when the new member should get its model connections +
    credentials (the default) — only when the host is configured at all."""
    if not inherit_config:
        return None
    from graph.config_io import config_yaml_path

    cfg_yaml = config_yaml_path()
    return str(cfg_yaml.parent) if cfg_yaml.exists() else None


def create_sync(
    name: str,
    *,
    bundle: str | None = None,
    ref: str | None = None,
    soul: str | None = None,
    port: int | None = None,
    start: bool = True,
    shared_skills: bool = False,
    inherit_config: bool = True,
    inputs: Mapping[str, str] | None = None,
    secrets: list[dict] | None = None,
    config_inputs: Mapping[str, object] | None = None,
    requires_tools: list[str] | None = None,
) -> dict:
    """Create a member (blank Basic, or from a bundle archetype) and start it unless told
    not to. Returns ``{agent, installed, warnings?}`` — the shape ``POST /api/fleet`` answers."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    ws = manager.create(
        name,
        bundle=bundle or None,
        bundle_ref=ref or None,
        port=port,
        shared_skills=shared_skills,
        inherit_model=_inherit_model_source(inherit_config),
        soul=soul or None,
        inputs=inputs,
        secrets=secrets,
        config_inputs=config_inputs,
        requires_tools=requires_tools,
    )
    agent = supervisor.start(name) if start else {"name": name, "id": ws["id"], "port": ws["port"], "running": False}
    out: dict[str, Any] = {"agent": agent, "installed": ws.get("installed", [])}
    if ws.get("warnings"):
        # A credential store that could not join the shared box tier is a note on a
        # SUCCESSFUL create, not a failure — the agent runs on the machine-wide login.
        out["warnings"] = ws["warnings"]
    return out


def remove_sync(ident: str, *, purge: bool = False) -> dict:
    """Stop the member if it runs, then retire it (keep its data) or purge it. A workspace
    that survived the stop raises ``manager.WorkspaceBusy`` — partial, retryable (#2583)."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    try:
        supervisor.stop(ident)  # stop if running (#6)
    except supervisor.FleetError:
        pass
    return manager.remove(ident, purge=purge)


def rename_sync(ident: str, new_name: object) -> dict:
    from graph.workspaces import manager

    new_name = str(new_name).strip() if isinstance(new_name, str) else ""  # None / a non-string is no name
    if not new_name:
        raise manager.WorkspaceError("name is required")
    return manager.rename(ident, new_name)


def remote_add_sync(name: str, url: str, token: str = "", allow_insecure: bool = False) -> dict:
    """Register a remote member and probe it at once, so the caller learns ``reachable``
    now instead of at the next poll — an unreachable peer is NOT rejected (deferred
    registration is intentional)."""
    from graph.fleet import supervisor

    rec = supervisor.add_remote(str(name or ""), str(url or ""), token=str(token or ""), allow_insecure=bool(allow_insecure))
    reachable, version = supervisor.probe_remote(rec["id"])
    return {"agent": rec, "reachable": reachable, "version": version, "auth": _auth_of(rec["id"])}


def remote_update_sync(
    ident: str, *, name: str | None = None, url: str | None = None, token: str | None = None, allow_insecure: bool = False
) -> dict:
    """Edit a remote in place — omitted fields stay, ``token=""`` clears the bearer. Moving
    the url to a new origin without a token CLEARS the stored one (it was issued by the old
    host); the answer then carries ``token_cleared: true`` so every surface can say so."""
    from graph.fleet import supervisor

    rec = supervisor.update_remote(ident, name=name, url=url, token=token, allow_insecure=bool(allow_insecure))
    token_cleared = bool(rec.pop("token_cleared", False))
    reachable, version = supervisor.probe_remote(rec["id"])
    out = {"agent": rec, "reachable": reachable, "version": version, "auth": _auth_of(rec["id"])}
    if token_cleared:
        out["token_cleared"] = True
    return out


def _auth_of(rid: str) -> str:
    """The stored token's verdict (ADR 0113 D5) right after ``probe_remote`` refreshed it —
    read from the full record, since the sanitized one the caller holds has no token."""
    from graph.fleet import supervisor

    return supervisor.remote_auth(supervisor.remote_for_slug(rid) or {"id": rid})


def remote_pair_sync(url: str, code: str, name: str | None = None, allow_insecure: bool = False) -> dict:
    """Pair with a remote by redeeming a code its operator minted (ADR 0113 D1): add it,
    or re-token the member already at that URL. ``{agent, reachable, version, auth, action}``."""
    from graph.fleet import supervisor

    return supervisor.pair_remote(
        str(url or ""), str(code or ""), name=(str(name) if name else None), allow_insecure=bool(allow_insecure)
    )


def remote_remove_sync(ident: str) -> dict:
    from graph.fleet import supervisor

    return supervisor.remove_remote(ident)


def order_sync(order: object) -> list[str]:
    from graph.fleet import supervisor

    return supervisor.set_roster_order(order)


@op(name="fleet.create", mutates=True, summary="Create a fleet member (blank, or from a bundle archetype), inheriting the host's model connections by default, and start it.")
async def create(
    name: str,
    *,
    bundle: str | None = None,
    ref: str | None = None,
    soul: str | None = None,
    port: int | None = None,
    start: bool = True,
    shared_skills: bool = False,
    inherit_config: bool = True,
    inputs: Mapping[str, str] | None = None,
    secrets: list[dict] | None = None,
    config_inputs: Mapping[str, object] | None = None,
    requires_tools: list[str] | None = None,
) -> dict:
    return await asyncio.to_thread(
        create_sync,
        name,
        bundle=bundle,
        ref=ref,
        soul=soul,
        port=port,
        start=start,
        shared_skills=shared_skills,
        inherit_config=inherit_config,
        inputs=inputs,
        secrets=secrets,
        config_inputs=config_inputs,
        requires_tools=requires_tools,
    )


@op(name="fleet.remove", mutates=True, summary="Remove a fleet member: stop it if running, then retire it (data kept) or purge its workspace.")
async def remove(ident: str, *, purge: bool = False) -> dict:
    return await asyncio.to_thread(remove_sync, ident, purge=purge)


@op(name="fleet.rename", mutates=True, summary="Rename a fleet member's display name (its id, URL slug and data scope never change).")
async def rename(ident: str, new_name: str) -> dict:
    return await asyncio.to_thread(rename_sync, ident, new_name)


@op(name="fleet.remotes.add", mutates=True, summary="Register a remote protoAgent as a switchable fleet member (optional bearer, never returned) and probe it.")
async def remotes_add(name: str, url: str, token: str = "", allow_insecure: bool = False) -> dict:
    return await asyncio.to_thread(remote_add_sync, name, url, token, allow_insecure)


@op(name="fleet.remotes.update", mutates=True, summary="Edit a remote member's name / url / token in place (omitted fields stay; an empty token clears it) and re-probe it.")
async def remotes_update(
    ident: str, *, name: str | None = None, url: str | None = None, token: str | None = None, allow_insecure: bool = False
) -> dict:
    return await asyncio.to_thread(remote_update_sync, ident, name=name, url=url, token=token, allow_insecure=allow_insecure)


@op(name="fleet.remotes.pair", mutates=True, summary="Pair with a remote protoAgent by claiming a code its operator generated; stores the minted per-device token (never returned) and adds or re-tokens the member.")
async def remotes_pair(url: str, code: str, name: str | None = None, allow_insecure: bool = False) -> dict:
    return await asyncio.to_thread(remote_pair_sync, url, code, name, allow_insecure)


@op(name="fleet.remotes.remove", mutates=True, summary="Unregister a remote fleet member (the remote agent itself is untouched).")
async def remotes_remove(ident: str) -> dict:
    return await asyncio.to_thread(remote_remove_sync, ident)


@op(name="fleet.order", mutates=True, summary="Persist the fleet roster's display order — a complete permutation of the current member ids.")
async def order(order: list[str]) -> list[str]:
    return await asyncio.to_thread(order_sync, order)
