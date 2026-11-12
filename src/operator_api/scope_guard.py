"""Config-write half of the ADR 0121 D4 credential-plane ceiling.

The route half (pairing, plugin lifecycle, secrets, OAuth) is enforced by URL in the auth
middleware (``a2a_impl.credential_scope``). Config writes can't be: one ``POST /api/settings``
can carry ``identity.name`` and ``auth.token`` together, so they are checked by KEY here, at
each config write route, against ``graph.settings_schema.credential_plane_keys``. A
``device``-scope caller (a paired phone) writing any credential-plane key gets a 403 that names
the key; every other key still saves.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def credential_plane_refusal(request: Any, updates: Any, *, nested: bool = False) -> JSONResponse | None:
    """A 403 when a non-``full``-scope caller's config write touches a credential-plane key,
    else None. ``updates`` / ``nested`` as for ``credential_plane_keys``.

    The scope comes from ``request.state.credential_scope`` (stamped by the auth middleware).
    Unstamped (an app assembled without the middleware, open mode never reaches here unstamped)
    reads as ``full`` ONLY when no credential is configured at all — otherwise as the narrow
    scope, so a missing stamp can't widen anything."""
    if _is_full(request):
        return None
    from graph.settings_schema import credential_plane_keys

    keys = credential_plane_keys(updates, nested=nested)
    if not keys:
        return None
    named = ", ".join(keys)
    return _refusal(f"This device can't change {named} — use a full-access device.", keys)


#: Watch verifier types that run a command on the server host (``graph.goals.verifiers``).
HOST_COMMAND_VERIFIERS = frozenset({"command", "test", "ci"})


def host_command_refusal(request: Any, body: Any) -> JSONResponse | None:
    """A 403 when a non-``full``-scope caller's watch create/edit sets a verifier that runs a
    command on the host, else None. A watch re-runs its verifier on an interval for as long as
    it lives, so such a watch is host code that outlives a revoke — the same reason MCP servers
    and ``lifecycle_hooks`` are on the ceiling (ADR 0121 D4). Other verifiers, and every other
    watch field, stay open to a device."""
    verifier = body.get("verifier") if isinstance(body, dict) else None
    vtype = verifier.get("type") if isinstance(verifier, dict) else None
    # A non-string type is not one of ours (and may be unhashable): leave it to the route's own
    # validation rather than fail here.
    if not isinstance(vtype, str) or vtype not in HOST_COMMAND_VERIFIERS or _is_full(request):
        return None
    return _refusal(
        f"This device can't set a {vtype!r} check, which runs on this computer — use a full-access device.", []
    )


def _is_full(request: Any) -> bool:
    """Whether ``request`` carries a ``full``-scope credential (see ``credential_plane_refusal``)."""
    from a2a_impl.credential_scope import SCOPE_FULL

    scope = getattr(getattr(request, "state", None), "credential_scope", None)
    if scope is None:
        from a2a_impl.auth import open_mode

        scope = SCOPE_FULL if open_mode() else "device"
    return scope == SCOPE_FULL


def _refusal(reason: str, keys: list[str]) -> JSONResponse:
    from a2a_impl.credential_scope import DEVICE_SCOPE_CODE

    return JSONResponse(
        {
            "ok": False,
            "error": DEVICE_SCOPE_CODE,
            "messages": [reason],
            "keys": keys,
            "detail": {"code": DEVICE_SCOPE_CODE, "reason": reason, "keys": keys},
        },
        status_code=403,
    )
