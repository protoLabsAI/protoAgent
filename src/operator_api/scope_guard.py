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
    from a2a_impl.credential_scope import DEVICE_SCOPE_CODE, SCOPE_FULL

    scope = getattr(getattr(request, "state", None), "credential_scope", None)
    if scope is None:
        from a2a_impl.auth import open_mode

        scope = SCOPE_FULL if open_mode() else "device"
    if scope == SCOPE_FULL:
        return None
    from graph.settings_schema import credential_plane_keys

    keys = credential_plane_keys(updates, nested=nested)
    if not keys:
        return None
    named = ", ".join(keys)
    reason = f"This device can't change {named} — use a full-access device."
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
