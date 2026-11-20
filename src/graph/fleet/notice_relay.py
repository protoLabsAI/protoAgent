"""A member's notice reaches the operator's devices through its hub (ADR 0135 W2, #5614).

The hub owns the operator's paired devices and the only Web Push sender, so a member
registers no sender of its own. Without this module a member's notice (a decision it
asked for) stopped inside the member's own process and never reached a phone.

Two halves, one per side of the loopback call:

* **Member** (:func:`hand_to_hub`): a notice adapter behind ``notify_chat_surfaces``. It
  posts the notice's identity to the hub at ``POST /api/fleet/notices`` with the fleet
  service token (ADR 0089) the supervisor already gave it. The hub's address arrives by
  env at spawn (:data:`HUB_URL_ENV`); a member started without one hands nothing over.
* **Hub** (:func:`accept_member_notice`): checks the member is one of its own
  workspaces, remembers which agent the notice came from, and queues it on the same
  push ledger its own notices use.

Only the notice's identity crosses: id, kind, importance and times. No text, no link.
The hub's quiet hours apply to a member's notice as to its own: a routine one is held
for the hub's digest, under the hub's own wording ("A notice from <member>").
The origin is kept on the hub and read back after authentication (``GET
/api/notices/<id>``), so the push payload still names no member (ADR 0135, "What leaves
the machine").
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from graph.plugins.chat_surface import _NOTICE_LIFETIMES, ChatNotice

logger = logging.getLogger(__name__)

#: The hub's own loopback address, exported by the hub at boot and inherited by the
#: members it spawns. Never a public or tailnet address: the handoff stays on the box.
HUB_URL_ENV = "PROTOAGENT_FLEET_HUB_URL"
ADAPTER_KEY = "fleet-hub-relay"
ROUTE = "/api/fleet/notices"

_NOTICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_IMPORTANCES = ("low", "normal", "high")
_RETRY_DELAYS = (1.0, 4.0)  # after the first attempt; a decision's notice is worth 3 tries
_TIMEOUT = 5.0
_CLOCK_SKEW = 300.0  # same box, same clock: anything further off is not a real timestamp


def loopback_hub_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def hub_url() -> str:
    """The hub's loopback origin from the environment, or ``""`` when absent or not loopback."""
    raw = (os.environ.get(HUB_URL_ENV) or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1") or not port:
        return ""
    if parts.path or parts.query or parts.fragment or parts.username:
        return ""
    return raw


def member_id() -> str | None:
    """This process's workspace id, read from its instance root's ``workspace.yaml``; None
    when there is no record (a hub or standalone instance has none, ADR 0042)."""
    from graph.workspaces import manager
    from infra.paths import instance_paths

    record = manager._read_record(instance_paths().instance_root)
    wid = record.get("id") if record else None
    return wid if isinstance(wid, str) and wid else None


async def hand_to_hub(notice: ChatNotice, *, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Member side: give one notice's identity to the hub. True when the hub took it.

    Never raises into the notice seam. A refusal (4xx) is final; a network failure or a
    5xx is retried a couple of times, since the hub may be mid-restart.
    """
    from graph.fleet.service_token import resolve_service_token

    base, wid = hub_url(), member_id()
    if not base or not wid or not notice.notice_id:
        return False
    body = {
        "member": wid, "notice_id": notice.notice_id, "kind": notice.kind,
        "importance": notice.importance, "created_at": notice.created_at, "expires_at": notice.expires_at,
    }
    headers = {"Authorization": f"Bearer {resolve_service_token()}"}
    for delay in (0.0, *_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
                response = await client.post(base + ROUTE, json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("[notice-relay] hub not reached: %s", type(exc).__name__)
            continue
        if 200 <= response.status_code < 300:
            return True
        if response.status_code < 500:
            logger.warning("[notice-relay] hub refused the notice: HTTP %s", response.status_code)
            return False
        logger.warning("[notice-relay] hub answered HTTP %s", response.status_code)
    return False


class NoticeRefused(ValueError):
    """The hub will not take this notice; the message is safe to return to the member."""


async def accept_member_notice(payload: dict, *, clock=time.time) -> bool:
    """Hub side: queue a member's notice for the operator's devices.

    True when it was queued or held by the hub's quiet hours (``graph.notice_hold``; a
    question or high-importance notice is never held), False when this hub has no push
    sender running (nothing to do, not an error). Raises :class:`NoticeRefused` for a member this hub does not run
    or a notice that is not well formed. Times are clamped to the kind's own lifetime: a
    member can shorten a notice's life, never extend it.
    """
    from graph import notice_hold, push_transport
    from graph.workspaces import manager

    member = payload.get("member")
    if not isinstance(member, str) or not member:
        raise NoticeRefused("member is required")
    try:
        workspace = manager._find(manager._safe(member))
    except Exception as exc:  # noqa: BLE001 — an id the registry rejects is just unknown
        raise NoticeRefused("unknown member") from exc
    if workspace is None or workspace.get("id") != member:
        raise NoticeRefused("unknown member")
    notice_id, kind, importance = payload.get("notice_id"), payload.get("kind"), payload.get("importance")
    if not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id):
        raise NoticeRefused("notice_id is not an opaque identifier")
    # isinstance first: a JSON list or object is unhashable, and `in` on the dict would raise.
    if not isinstance(kind, str) or kind not in _NOTICE_LIFETIMES or importance not in _IMPORTANCES:
        raise NoticeRefused("unknown notice kind or importance")
    now = clock()
    try:
        created_at, expires_at = float(payload["created_at"]), float(payload["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NoticeRefused("notice times are required") from exc
    if not (math.isfinite(created_at) and math.isfinite(expires_at)):
        raise NoticeRefused("notice times are not finite")
    if abs(created_at - now) > _CLOCK_SKEW:
        created_at = now
    expires_at = min(expires_at, now + _NOTICE_LIFETIMES[kind])
    if expires_at <= now:
        raise NoticeRefused("notice has already expired")
    transport = push_transport.active_transport()
    if transport is None:
        return False
    await asyncio.to_thread(transport.remember_origin, notice_id, member, expires_at)
    # The text is the hub's own wording and names the member only: nothing the member
    # sent is in it. The push payload never carries it; the quiet-hours hold keeps it
    # for the digest the hub sends when the window ends.
    notice = ChatNotice(
        kind=kind, importance=importance, text=f"A notice from {workspace.get('name') or member}", link="",
        notice_id=notice_id, created_at=created_at, expires_at=expires_at,
    )
    # The hub's quiet hours hold a member's routine notice as they hold its own (#5648).
    # Held is still taken: the member must not retry it.
    if await asyncio.to_thread(notice_hold.DELIVERY.hold, notice):
        return True
    await transport.accept(notice)
    return True
