"""Delegate CRUD REST API (ADR 0025, PR2).

Mounted on the app at ``/api/delegates*`` (operator-console surface — same posture
as ``/api/config``: localhost-default bind + bearer-when-exposed, no per-route
auth). Drives the panel (PR3): list / create / update / delete / test + the
type schema. Mutations write config + secrets, then hot-reload the graph so the
new roster is live on the next turn.
"""

from __future__ import annotations

import logging

from . import store
from .adapters import ADAPTERS, DelegateError, delegate_types, is_secretish

log = logging.getLogger("protoagent.plugins.delegates")

# ``is_secretish`` (shared with the store) marks a config key — or a per-delegate
# ``env`` var NAME — as secret-bearing; its value is never returned by the read API.
_is_secretish = is_secretish


def _redact_env(env: dict, secret_keys: set | None = None) -> dict:
    """Redact env values whose name looks secret-bearing (e.g. OPENAI_API_KEY) OR
    that carry a stored per-row secret (``secret_keys``, from the overlay) — so a
    secret with an innocuous name (an explicit per-row toggle) is masked too."""
    keys = secret_keys or set()
    return {k: ("***" if (k in keys or _is_secretish(k)) else v) for k, v in env.items()}


def _public_view(raw: dict) -> dict:
    """A delegate as the panel sees it: config fields minus any secret, plus a
    ``configured`` flag (does it parse?) and ``has_secret`` (is one stored?)."""
    adapter = ADAPTERS.get(str(raw.get("type", "")))
    configured, error = True, None
    if adapter is None:
        configured, error = False, f"unknown type {raw.get('type')!r}"
    else:
        try:
            adapter.parse(dict(raw))
        except DelegateError as e:
            configured, error = False, str(e)
    name = raw.get("name")
    overlay = store.secret_overlay()
    has_secret = bool(adapter and adapter.secret_field and overlay.get(f"{name}.{adapter.secret_field}"))
    # Per-env secret var names stored for this delegate (`<name>.env.<VAR>`) — masked
    # in the returned env so the form shows them set-but-masked (#2114).
    env_secret_keys = store.env_secret_values(overlay, name).keys() if name else set()
    # Drop any secret-bearing top-level field (api_key, *_token, auth, …).
    view = {k: v for k, v in raw.items() if not _is_secretish(k)}
    # keep auth.scheme (not the token) for a2a so the form can prefill it
    if isinstance(raw.get("auth"), dict) and raw["auth"].get("scheme"):
        view["auth"] = {"scheme": raw["auth"]["scheme"]}
    # the env dict is free-form — redact secret-named + per-row-secret values inside it.
    if isinstance(view.get("env"), dict):
        view["env"] = _redact_env(view["env"], set(env_secret_keys))
    view.update(
        {
            "name": name,
            "type": raw.get("type"),
            "description": raw.get("description", ""),
            "configured": configured,
            "error": error,
            "has_secret": has_secret,
            "has_env_secrets": bool(env_secret_keys),
        }
    )
    return view


def _list_payload() -> dict:
    from .health import health_snapshot

    health = health_snapshot()
    out = []
    for r in store.read_delegates_raw():
        if not isinstance(r, dict):
            continue
        view = _public_view(r)
        h = health.get(view.get("name"))
        if h:
            view["health"] = h
        out.append(view)
    return {"delegates": out}


def _validate(entry: dict):
    name = str(entry.get("name", "")).strip()
    if not name:
        raise ValueError("delegate needs a name")
    adapter = ADAPTERS.get(str(entry.get("type", "")))
    if adapter is None:
        raise ValueError(f"unknown type {entry.get('type')!r} (want one of {', '.join(ADAPTERS)})")
    try:
        adapter.parse(dict(entry))  # secrets not required to validate shape
    except DelegateError as e:
        raise ValueError(str(e))
    return name, adapter


def _inject_stored_secret(entry: dict, adapter) -> dict:
    """For Test: if the entry omits its secret, fill it from the stored overlay so
    the probe uses the saved credential."""
    if not adapter.secret_field:
        return entry
    import copy

    entry = copy.deepcopy(entry)
    if store._pop_dotted(copy.deepcopy(entry), adapter.secret_field):
        return entry  # caller supplied one
    val = store.secret_overlay().get(f"{entry.get('name')}.{adapter.secret_field}")
    if val:
        store._set_dotted(entry, adapter.secret_field, val)
    return entry


async def _reload():
    import asyncio

    from server.agent_init import _reload_langgraph_agent

    return await asyncio.to_thread(_reload_langgraph_agent)


def build_router():
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter()

    @router.get("/api/delegate-types")
    async def _types():
        return {"types": delegate_types()}

    @router.get("/api/delegates")
    async def _list():
        return _list_payload()

    @router.post("/api/delegates/test")
    async def _test(entry: dict = Body(...)):
        # Testing a SAVED delegate (the per-row button sends just {name, type}) →
        # probe its stored config. Testing a draft from the form → use the draft
        # (its fields override the stored base). Either way, fill in the secret.
        name = str(entry.get("name", "")).strip()
        if name:
            stored = next(
                (e for e in store.read_delegates_raw() if isinstance(e, dict) and e.get("name") == name),
                None,
            )
            if stored:
                entry = {**stored, **entry}
        adapter = ADAPTERS.get(str(entry.get("type", "")))
        if adapter is None:
            raise HTTPException(400, f"unknown type {entry.get('type')!r}")
        merged = _inject_stored_secret(entry, adapter)
        try:
            d = adapter.parse(merged)
        except DelegateError as e:
            raise HTTPException(400, str(e))
        return await adapter.probe(d)

    @router.post("/api/delegates")
    async def _create(entry: dict = Body(...)):
        try:
            name, _ = _validate(entry)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if any(isinstance(e, dict) and e.get("name") == name for e in store.read_delegates_raw()):
            raise HTTPException(409, f"delegate {name!r} already exists")
        store.upsert_delegate(entry)
        ok, msg = await _reload()
        return {"ok": ok, "message": msg, **_list_payload()}

    @router.put("/api/delegates/{name}")
    async def _update(name: str, entry: dict = Body(...)):
        if entry.get("name") and entry["name"] != name:
            raise HTTPException(400, "name in body must match the path")
        entry["name"] = name
        try:
            _validate(entry)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not any(isinstance(e, dict) and e.get("name") == name for e in store.read_delegates_raw()):
            raise HTTPException(404, f"delegate {name!r} not found")
        store.upsert_delegate(entry)
        ok, msg = await _reload()
        return {"ok": ok, "message": msg, **_list_payload()}

    @router.delete("/api/delegates/{name}")
    async def _delete(name: str):
        store.delete_delegate(name)
        ok, msg = await _reload()
        return {"ok": ok, "message": msg, **_list_payload()}

    return router
