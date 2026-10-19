"""Revocation reaches open WebSockets (ADR 0121 D5).

``a2a_impl.stream_guard.DeviceStreamGuard`` closes a paired device's streamed HTTP responses
within one keepalive of a revoke. A WebSocket is a different ASGI scope, and it authenticates
once, at open: the fleet proxy checks a ``?token=`` at the handshake, a plugin socket redeems a
single-use ticket that an authenticated HTTP call minted (agent_browser's ``?ticket=``, the
terminal's in-band ticket frame). After that nothing looks again, so without this a revoked
phone's open terminal kept its shell.

``DeviceSocketGuard`` is ONE pure-ASGI wrapper around every ``websocket`` scope, the core
routes and every plugin's alike, so no handler has to remember a re-check. While the socket is
open it runs one timer per socket (``SOCKET_RECHECK_SECONDS``, no per-message work) that
re-verifies the device stamped on the scope (``scope["state"]["device_id"]``) with
``a2a_impl.auth.device_is_live``. When the device is gone (revoked, reuse-revoked or
idle-expired) the guard sends the client a close with code 1008 (policy violation), hands the
app a ``websocket.disconnect`` and makes its further sends fail, so the handler unwinds the way
it does when a client leaves. A handler that doesn't notice within a short grace is cancelled.

Where the device id comes from:

- **A credential presented at the handshake.** At open the guard classifies a ``?token=`` query
  value and an ``Authorization: Bearer`` header (``auth.presented_device_id``): a paired
  device's token, or a stream token (``/api/sse-token``) a device minted. This covers the hub's
  fleet WS proxy, which swaps a device's ``?token=`` for the fleet token or a remote's stored
  bearer: the hub-side socket closes, ``_pump_ws`` ends, and ``forward_ws`` closes the
  member-side socket with it.
- **A ticket.** A socket that authenticates with a ticket carries no credential the guard can
  read. The plugin records the minting device with the ticket (``request_device_id`` on the
  gated HTTP mint route) and, on redeeming it, calls ``bind_device(ws, device_id)``. The guard
  picks the binding up at its next tick, so binding after ``accept`` (an in-band ticket frame)
  works too.

Only device credentials are checked. The shared bearer, an API key, the fleet token, a
federation token and open mode carry no device id, so their sockets are never closed by this.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# Inside ADR 0121 D5's 10-15 s band: a revoked phone's socket closes within about this long.
SOCKET_RECHECK_SECONDS = 12.0

# How long a handler gets to unwind on its own after the close before it is cancelled.
_UNWIND_GRACE_SECONDS = 2.0

POLICY_VIOLATION = 1008
_CLOSE_REASON = "device revoked or expired"


def _state(scope) -> dict:
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    return state


def _device_id(scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    did = state.get("device_id")
    return did if isinstance(did, str) and did else None


def bind_device(conn, device_id: str | None) -> None:
    """Tie an open WebSocket to the paired device that authorised it, so the guard closes the
    socket when that device is revoked or expires.

    ``conn`` is a Starlette/FastAPI ``WebSocket`` or a raw ASGI scope. For a ticket-authenticated
    socket, pass the device id recorded when the ticket was minted (``request_device_id``). A
    falsy id (a full-scope minter: the shared bearer, the fleet token) binds nothing. A socket
    already bound keeps its first device: a later bind can't swap it for another."""
    if not device_id:
        return
    scope = conn if isinstance(conn, dict) else getattr(conn, "scope", None)
    if not isinstance(scope, dict):
        return
    _state(scope).setdefault("device_id", str(device_id))


def request_device_id(request) -> str | None:
    """The paired device an authenticated HTTP request came from, or None for a full-scope
    credential. Call it on the gated route that mints a WebSocket ticket and store the result
    with the ticket; ``bind_device`` it when the ticket is redeemed."""
    did = getattr(getattr(request, "state", None), "device_id", None)
    return did if isinstance(did, str) and did else None


def _presented_credentials(scope) -> list[str]:
    from urllib.parse import parse_qsl

    out: list[str] = []
    raw = scope.get("query_string") or b""
    try:
        query = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:  # noqa: BLE001
        query = ""
    out.extend(v for (k, v) in parse_qsl(query, keep_blank_values=False) if k == "token" and v)
    for name, value in scope.get("headers") or ():
        if bytes(name).lower() != b"authorization":
            continue
        scheme, _, cred = bytes(value).decode("latin-1").strip().partition(" ")
        if scheme.lower() == "bearer" and cred.strip():
            out.append(cred.strip())
    return out


class DeviceSocketGuard:
    """Pure-ASGI wrapper: closes a device's WebSocket once the device stops being live.

    Installed by ``a2a_impl.auth.install`` next to ``DeviceStreamGuard``. Non-websocket scopes
    pass straight through. ``recheck_seconds``, ``is_live`` and ``presented`` are test seams."""

    def __init__(
        self,
        app,
        *,
        recheck_seconds: float | None = None,
        is_live=None,
        presented=None,
        unwind_grace: float | None = None,
    ) -> None:
        self.app = app
        self._recheck = recheck_seconds
        self._is_live = is_live
        self._presented = presented
        self._grace = unwind_grace

    def _live(self, device_id: str) -> bool:
        if self._is_live is not None:
            return bool(self._is_live(device_id))
        from a2a_impl.auth import device_is_live

        return device_is_live(device_id)

    def _device_from_handshake(self, scope) -> str | None:
        if self._presented is not None:
            return self._presented(scope)
        from a2a_impl.auth import presented_device_id

        for cred in _presented_credentials(scope):
            did = presented_device_id(cred)
            if did:
                return did
        return None

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "websocket":
            await self.app(scope, receive, send)
            return

        # Created up front so the dict the handler's ``ws.state`` writes into is the one this
        # guard reads (routing copies of the scope share it).
        _state(scope)
        if _device_id(scope) is None:
            try:
                did = self._device_from_handshake(scope)
            except Exception:  # noqa: BLE001 — classification must never break a handshake
                logger.exception("[auth] could not classify a websocket credential")
                did = None
            bind_device(scope, did)

        interval = self._recheck if self._recheck is not None else SOCKET_RECHECK_SECONDS
        grace = self._grace if self._grace is not None else _UNWIND_GRACE_SECONDS
        closed = asyncio.Event()
        cut = [False]  # the guard cancelled the app (not the server)

        async def guarded_send(message) -> None:
            if closed.is_set():
                if message.get("type") == "websocket.close":
                    return  # already closed for the device; the handler's own close is a no-op
                # Starlette turns this into ``WebSocketDisconnect`` for the handler.
                raise OSError("device credential revoked")
            await send(message)

        async def guarded_receive():
            if closed.is_set():
                return {"type": "websocket.disconnect", "code": POLICY_VIOLATION}
            message = await receive()
            if closed.is_set() and message.get("type") != "websocket.connect":
                return {"type": "websocket.disconnect", "code": POLICY_VIOLATION}
            return message

        app_task = asyncio.ensure_future(self.app(scope, guarded_receive, guarded_send))

        async def watch() -> None:
            while True:
                await asyncio.sleep(interval)
                did = _device_id(scope)
                if did is None or self._live(did):
                    continue
                logger.info(
                    "[auth] closing a websocket for device %s: revoked or expired (%s)",
                    did,
                    scope.get("path", ""),
                )
                closed.set()
                with contextlib.suppress(Exception):
                    await send({"type": "websocket.close", "code": POLICY_VIOLATION, "reason": _CLOSE_REASON})
                # The handler normally unwinds now: its next send fails and the server hands its
                # pending receive the disconnect. One stuck elsewhere is cancelled.
                done, _ = await asyncio.wait({app_task}, timeout=grace)
                if not done:
                    cut[0] = True
                    app_task.cancel()
                return

        watcher = asyncio.ensure_future(watch())
        try:
            await app_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not cut[0] or (current is not None and current.cancelling()):
                raise
        finally:
            watcher.cancel()
            if not app_task.done():
                app_task.cancel()
