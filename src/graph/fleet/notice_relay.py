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
  workspaces, remembers which agent the notice came from, and raises it through the
  hub's own notice seam (``notify_chat_surfaces``). So it is published on the hub's
  event stream (the desktop app's native banner reads it there) and queued to every
  notice adapter the hub runs: its chat plugins and the push ledger its own notices use.

Only the notice's identity crosses: id, kind, importance and times. No text, no link.
The hub's quiet hours apply to a member's notice: a routine one is held for the hub's
digest, under the hub's own wording ("A notice from <member>"), and while held it is
kept off the event stream too, so the desktop app raises no banner for it.
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

from graph.plugins.chat_surface import _NOTICE_LIFETIMES, ChatNotice, notify_chat_surfaces

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
    """Hub side: raise a member's notice through the hub's own notice seam.

    The seam (``notify_chat_surfaces``) publishes it on the hub bus as a ``notice``
    event and queues it to every registered notice adapter, the push ledger included.
    True when it was raised or held by the hub's quiet hours (``graph.notice_hold``; a
    question or high-importance notice is never held, and a held one reaches neither
    the bus nor an adapter until the hub's digest), False when this hub has no push
    sender running (nothing to do, not an error). Raises :class:`NoticeRefused` for a
    member this hub does not run or a notice that is not well formed. Times are clamped
    to the kind's own lifetime: a member can shorten a notice's life, never extend it.
    The hub stamps the creation time itself. An adapter that fails is logged by the seam
    and does not fail the handoff.
    """
    from graph import push_transport
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
    expires_at = min(expires_at, now + _NOTICE_LIFETIMES[kind])
    if expires_at <= now:
        raise NoticeRefused("notice has already expired")
    transport = push_transport.active_transport()
    if transport is None:
        return False
    await asyncio.to_thread(transport.remember_origin, notice_id, member, expires_at)
    # Raised through the hub's own notice seam, off the event loop (the seam reads the
    # quiet-hours store). The seam applies the hub's quiet hours (#5648), publishes the
    # bus event the desktop app reads and queues every adapter, the push ledger included.
    # publish_held=False: a routine notice held by quiet hours stays off the bus too,
    # because the desktop app shows every `notice` event as a native banner with no
    # quiet-hours check of its own. The text is the hub's own wording and names the
    # member only: nothing the member sent is in it, and the link is empty. Held is
    # still taken: the member must not retry it. No Home entry: Home is not part of
    # the relay.
    await asyncio.to_thread(
        notify_chat_surfaces, f"A notice from {workspace.get('name') or member}", "",
        kind=kind, importance=importance, notice_id=notice_id,
        ttl_seconds=max(0.0, expires_at - now), source="fleet-relay", home_entry=False,
        publish_held=False,
    )
    return True
