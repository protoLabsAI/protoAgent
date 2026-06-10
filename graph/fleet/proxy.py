"""Reverse proxy for the fleet console (ADR 0042 slug routing).

The hub forwards console traffic to a specific agent named by the **URL slug** —
``/agents/<slug>/<path>`` (the slug lives in the console URL ``/app/agent/<slug>/``), so each
console window targets its own agent independently (chat → ``/agents/<slug>/api/chat``, SSE →
``/agents/<slug>/api/events``, A2A → ``/agents/<slug>/a2a``). ``slug == "host"`` is this
instance; any other slug resolves to its workspace port via the supervisor. There is no
server-side "active" pointer — switching agents is just navigating the console URL, so two
windows can't desync (the URL is the source of truth). The slug only resolves while that
agent is actually running.

Streaming-safe: responses (incl. SSE) are piped through unbuffered, and the upstream
client is closed when the stream ends.
"""

from __future__ import annotations

import logging
import time

import httpx
from starlette.responses import JSONResponse, StreamingResponse

from graph.fleet import supervisor

log = logging.getLogger("protoagent.server")

# Headers we must not copy verbatim across the proxy boundary.
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
        "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


# Shared client (#8) — one pooled AsyncClient instead of a fresh one (TCP setup + FD churn) per
# request. Unlimited read/write (SSE streams forever) but a finite connect timeout so a peer
# that accepts then stalls doesn't hang non-streaming requests indefinitely.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    return _client


# Per-slug target resolution (ADR 0042 slug routing) — each console window targets an agent by
# URL slug (/agents/<slug>/…) instead of a single global "active". 'host' = this instance; a
# local peer = its workspace port; a REMOTE member = its registered URL (+ its bearer, if one
# was stored — replacing the browser's Authorization, which carries the HUB's token, not the
# remote's). 1s TTL cache, keyed by slug, to keep the proxy hot path cheap.
_slug_cache: dict = {}


def _target_for_slug(slug: str) -> tuple[str, dict] | None:
    """``(base_url, extra_headers)`` for a slug, or None when it isn't reachable."""
    now = time.monotonic()
    hit = _slug_cache.get(slug)
    if hit and now - hit[1] < 1.0:
        return hit[0]
    target: tuple[str, dict] | None = None
    if slug == "host":
        from runtime.state import STATE
        port = getattr(STATE, "active_port", None)
        target = (f"http://127.0.0.1:{port}", {}) if port else None
    else:
        rec = supervisor._load_state().get(slug)
        if rec and supervisor._alive(rec.get("pid")):
            target = (f"http://127.0.0.1:{rec['port']}", {})
        else:
            remote = supervisor.remote_for_slug(slug)
            if remote:
                extra = {"authorization": f"Bearer {remote['token']}"} if remote.get("token") else {}
                target = (remote["url"], extra)
    _slug_cache[slug] = (target, now)
    return target


async def _forward_to_base(base: str, request, path: str, extra_headers: dict | None = None):
    """Stream-proxy ``request`` to ``<base>/<path>`` (SSE-safe)."""
    url = f"{base}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers.update(extra_headers or {})

    client = _get_client()
    upstream_req = client.build_request(
        request.method, url, headers=headers, content=body,
        params=dict(request.query_params))
    try:
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "agent is not reachable"}, status_code=502)

    async def _pipe():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()  # close the response, not the shared client

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(_pipe(), status_code=upstream.status_code, headers=resp_headers)


async def forward_to(slug: str, request, path: str):
    """Reverse-proxy to the agent named by ``slug`` (/agents/<slug>/* route, ADR 0042 slug
    routing). ``host`` targets this instance; a remote member targets its URL; 409 if the
    agent isn't running/registered."""
    target = _target_for_slug(slug)
    if target is None:
        return JSONResponse({"detail": f"agent {slug!r} is not running"}, status_code=409)
    base, extra = target
    return await _forward_to_base(base, request, path, extra)


async def _pump_ws(client_ws, upstream) -> None:
    """Relay frames between the browser-side Starlette ``WebSocket`` and the upstream
    ``websockets`` client until either side closes. First-to-finish wins; the other
    direction is cancelled and both ends are closed."""
    import asyncio

    async def client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if (t := msg.get("text")) is not None:
                    await upstream.send(t)
                elif (b := msg.get("bytes")) is not None:
                    await upstream.send(b)
        except Exception:  # noqa: BLE001 — a closed/erroring side just ends the relay
            return

    async def upstream_to_client():
        try:
            async for msg in upstream:
                if isinstance(msg, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(msg))
                else:
                    await client_ws.send_text(msg)
        except Exception:  # noqa: BLE001
            return

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def forward_ws(slug: str, ws, path: str) -> None:
    """Reverse-proxy a **WebSocket** to the agent named by ``slug`` (#883). The HTTP proxy
    above can't carry a WS upgrade (it strips ``Upgrade``/``Connection``), so a plugin's
    live WS — agent_browser's viewport/feed, say — couldn't traverse the hub: HTTP loaded
    the panel but the socket showed "Disconnected". This resolves the slug → member, opens
    a client WS to it (carrying the bearer + subprotocols), and pumps frames both ways.
    """
    import websockets

    target = _target_for_slug(slug)
    if target is None:
        await ws.close(code=1011, reason=f"agent {slug!r} is not running")
        return
    base, extra = target
    ws_base = "ws" + base[len("http"):]  # http(s):// → ws(s)://
    query = ws.url.query
    upstream_url = f"{ws_base}/{path}" + (f"?{query}" if query else "")

    headers = dict(extra)  # a remote member's bearer; else carry the browser's
    auth = ws.headers.get("authorization")
    if auth and not any(k.lower() == "authorization" for k in headers):
        headers["authorization"] = auth
    sub = ws.headers.get("sec-websocket-protocol")
    subprotocols = [s.strip() for s in sub.split(",") if s.strip()] if sub else None

    try:
        upstream = await websockets.connect(
            upstream_url, additional_headers=headers or None, subprotocols=subprotocols,
            open_timeout=5, ping_interval=None, max_size=None,
        )
    except Exception as exc:  # noqa: BLE001 — connect refused / handshake failed / not a WS route
        log.info("[fleet] ws proxy to %s (%s) failed: %s", slug, path, exc)
        await ws.close(code=1011, reason="upstream websocket unreachable")
        return
    try:
        await ws.accept(subprotocol=upstream.subprotocol)
        await _pump_ws(ws, upstream)
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
