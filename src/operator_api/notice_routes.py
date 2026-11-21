"""Notice routes for the fleet (ADR 0135 W2, #5614).

    POST /api/fleet/notices  → a member hands its hub a notice for the operator's devices
    GET  /api/notices/{id}   → which agent raised a notice (for routing a clicked one)

The hub owns the operator's paired devices and the only Web Push sender, so a member's
notice reaches a phone by being handed to the hub (``graph.fleet.notice_relay``). The
handoff carries the notice's identity only — no text, no link — and is accepted from the
fleet service token alone (ADR 0089): the credential every member of this hub already
holds, and nothing outside the box does.

The read is for the console: a push payload names no member (ADR 0135, "What leaves the
machine"), so after a click the console asks which agent's console holds the decision.
Like the decision routes it has no check of its own: the global auth middleware gates
it, and any operator credential (a paired phone included) may read it.
"""

from __future__ import annotations

import hmac
import logging
import re

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger("protoagent.server.notices")

_NOTICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _is_fleet_caller(request: Request) -> bool:
    """True when the caller presented this instance's fleet service token.

    Compared here, not read from the middleware's classification: a hub with no bearer
    configured runs open and classifies nobody, and this route must not then accept
    any local process.
    """
    from graph.fleet.service_token import resolve_service_token

    scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return False
    return hmac.compare_digest(presented.strip().encode(), resolve_service_token().encode())


def register_notice_routes(app) -> None:
    @app.post("/api/fleet/notices")
    async def _member_notice(request: Request):  # noqa: ANN202
        from graph.fleet import notice_relay

        if not _is_fleet_caller(request):
            return JSONResponse({"ok": False, "error": "not_a_member"}, status_code=403)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — any unreadable body is the same refusal
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid_notice"}, status_code=400)
        try:
            queued = await notice_relay.accept_member_notice(payload)
        except notice_relay.NoticeRefused as exc:
            return JSONResponse({"ok": False, "error": "invalid_notice", "detail": str(exc)}, status_code=400)
        return {"ok": True, "queued": queued}

    @app.get("/api/notices/{notice_id}")
    async def _notice_origin(notice_id: str):  # noqa: ANN202
        """``{ok, agent}``: the member that raised the notice, or null for this agent's own
        (or one it no longer remembers). Never an error for an unknown id: the console
        then simply opens the notice where it is."""
        import asyncio

        from graph import push_transport

        transport = push_transport.active_transport()
        agent = None
        if transport is not None and _NOTICE_ID.fullmatch(notice_id):
            agent = await asyncio.to_thread(transport.origin_of, notice_id)
        return JSONResponse({"ok": True, "agent": agent}, headers={"Cache-Control": "no-store"})
