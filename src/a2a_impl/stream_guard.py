"""Revocation reaches long-lived streams (ADR 0121 D5).

A new request is refused the moment a device is revoked, because every request re-reads
``devices.json``. A stream is different: it was authorised once, at open, and then stays
open for hours — the console's event bus (``/api/events``), the attendance stream
(``/api/chat/attend``), an A2A ``message/stream``, a chat turn, a fleet-proxied SSE lane.
Without this, revoking a stolen phone would leave its open streams flowing.

``DeviceStreamGuard`` is ONE generic hook for every streamed HTTP response, rather than a
check in each stream's loop. It wraps ``send``: each streamed body chunk is a tick, and every
one of these streams already emits a chunk at least once per keepalive (``: keepalive`` on
the bus and attendance streams every 15 s, sse-starlette's ping on A2A every 15 s, the fleet
proxy's 30 s keepalive). So no new timer exists: at the first chunk at least
``STREAM_RECHECK_SECONDS`` after the last check, the guard re-verifies the device the auth
middleware stamped on the request (``request.state.device_id``). When the device is gone —
revoked, reuse-revoked or idle-expired — the guard ends the response and tells the app the
client left, so the stream closes within one keepalive.

It re-verifies the DEVICE, not the token: a stream opened with a token that has since been
rotated out must not trip reuse detection by being re-presented, and an SSE stream holds no
device token at all (it was opened with a 30 s query token carrying the device id).

Only device credentials are checked. The shared bearer, an API key, the fleet token and open
mode carry no device id, so their streams pass through untouched.

WebSockets are not HTTP responses and are not covered here: ``a2a_impl.socket_guard`` closes
a revoked device's open WebSockets.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Below the 15 s keepalive of the core streams, so EVERY keepalive tick re-checks (a check
# exactly at the interval could miss a tick to scheduling jitter), while a busy stream still
# reads devices.json at most once per this many seconds.
STREAM_RECHECK_SECONDS = 10.0


def _device_id(scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    did = state.get("device_id")
    return did if isinstance(did, str) and did else None


def _spec_at_least_2_4(scope) -> bool:
    raw = (scope.get("asgi") or {}).get("spec_version", "2.0")
    try:
        return tuple(int(p) for p in str(raw).split(".")[:2]) >= (2, 4)
    except ValueError:
        return False


class DeviceStreamGuard:
    """Pure-ASGI wrapper: closes a device's streamed response once the device stops being live.

    Installed outside ``A2AAuthMiddleware`` (which writes ``device_id`` into the shared
    ``scope["state"]``); the guard reads it lazily, at the first streamed chunk, by which time
    the middleware has run."""

    def __init__(self, app, *, recheck_seconds: float | None = None, is_live=None) -> None:
        self.app = app
        self._recheck = recheck_seconds
        self._is_live = is_live

    def _live(self, device_id: str) -> bool:
        if self._is_live is not None:
            return bool(self._is_live(device_id))
        from a2a_impl.auth import device_is_live

        return device_is_live(device_id)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        interval = self._recheck if self._recheck is not None else STREAM_RECHECK_SECONDS
        loop = asyncio.get_running_loop()
        closed = asyncio.Event()
        last_check: list[float | None] = [None]

        async def guarded_send(message) -> None:
            if closed.is_set():
                # The response is already finished. Under ASGI spec 2.4 a send after the client
                # left raises OSError (Starlette ends the stream on it); under 2.3 the server
                # drops it and the disconnect from ``guarded_receive`` ends the stream.
                if _spec_at_least_2_4(scope):
                    raise OSError("device credential revoked")
                return
            if message.get("type") == "http.response.body" and message.get("more_body"):
                device_id = _device_id(scope)
                if device_id is not None:
                    now = loop.time()
                    if last_check[0] is None:
                        last_check[0] = now  # the auth middleware verified it moments ago
                    elif now - last_check[0] >= interval:
                        last_check[0] = now
                        if not self._live(device_id):
                            closed.set()
                            logger.info(
                                "[auth] closing a stream for device %s: revoked or expired (%s)",
                                device_id,
                                scope.get("path", ""),
                            )
                            await send({"type": "http.response.body", "body": b"", "more_body": False})
                            if _spec_at_least_2_4(scope):
                                raise OSError("device credential revoked")
                            return
            await send(message)

        # A receive in flight when the waiter wins (or when the caller is cancelled — Starlette's
        # ``is_disconnected`` probes with an already-cancelled scope) is KEPT for the next call
        # rather than cancelled: cancelling it could drop a message the server already handed
        # over (a body chunk, the real disconnect).
        pending: list[asyncio.Future | None] = [None]

        async def guarded_receive():
            if closed.is_set():
                return {"type": "http.disconnect"}
            if pending[0] is None and _device_id(scope) is None:
                return await receive()  # not a device credential: nothing can close it early
            if pending[0] is None:
                pending[0] = asyncio.ensure_future(receive())
            recv = pending[0]
            waiter = asyncio.ensure_future(closed.wait())
            try:
                await asyncio.wait({recv, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
            if recv.done():
                pending[0] = None
                return recv.result()
            return {"type": "http.disconnect"}

        try:
            await self.app(scope, guarded_receive, guarded_send)
        finally:
            if pending[0] is not None and not pending[0].done():
                pending[0].cancel()
