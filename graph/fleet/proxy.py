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
_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}


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
    if extra_headers:
        # A header in extra REPLACES the caller's — drop any case-variant first, else the
        # upstream carries both (dict keys are case-sensitive, HTTP header names aren't) and
        # a swapped Authorization would sit BESIDE the caller's instead of overriding it.
        overridden = {k.lower() for k in extra_headers}
        headers = {k: v for k, v in headers.items() if k.lower() not in overridden}
        headers.update(extra_headers)

    client = _get_client()
    upstream_req = client.build_request(
        request.method, url, headers=headers, content=body, params=dict(request.query_params)
    )
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
    state = getattr(request, "state", None)
    # A remote member carries its own stored bearer in ``extra``; a host/local peer carries
    # nothing (the browser's own header rides through unless we swap it below).
    is_local = not extra.get("authorization")
    if getattr(state, "member_public", False):
        # A request the hub admitted off the MEMBER's public list (#1890 — the auth middleware
        # stamps ``member_public``) arrived anonymous; forward it anonymous. Lending EITHER the
        # stored remote bearer OR the fleet service token would hand an unauthenticated caller a
        # credential (same rule as the remote-WS refusal in forward_ws).
        extra = {k: v for k, v in extra.items() if k.lower() != "authorization"}
    elif is_local and getattr(state, "trust_tier", None) == "operator":
        # ADR 0089 D3: the hub already authenticated this operator caller. Present a LOCAL
        # member with the fleet service token in place of the caller's credential — a device
        # token the member's own registry (a different instance_root) can't verify, which is
        # why proxied plugin calls to sister agents 401'd. Swap only for the operator tier
        # (never elevate a lesser credential) and only for a local peer (a remote member keeps
        # its own stored bearer, resolved above).
        from graph.fleet.service_token import resolve_service_token

        extra = {**extra, "authorization": f"Bearer {resolve_service_token()}"}
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


def _member_ws_query(slug: str, raw_query: str) -> tuple[str, bool]:
    """Rewrite a WS handshake's query for a LOCAL-member target (ADR 0089).

    A member's plugin WS (terminal PTY, say) validates a ``?token=`` param against the member's
    OWN inbound bearer — which is now the fleet service token (D5) — but the console opens the
    socket with the *operator* bearer, so post-D5 that token mismatches and the member refuses
    the socket. Mirror what ``forward_to`` does for HTTP: authenticate the presented token at the
    hub and, if it's an operator credential, swap it for the fleet token the member expects.

    Returns ``(query, allowed)``; ``allowed=False`` ⇒ a token was presented that does not
    authenticate as operator — close the socket rather than proxy it. Pass-through unchanged for:
    the ``host`` slug (its plugins expect the operator bearer, not the fleet token) and
    ticket-based plugins that carry no ``token`` param (agent_browser mints a member-side ticket
    over HTTP — already correct — so the hub must not gate them). Callers refuse remote members
    before this (their stored bearer must never be lent to an unauthenticated WS caller)."""
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(raw_query, keep_blank_values=True)
    if slug == "host":
        return raw_query, True
    from a2a_impl.auth import bearer_tier

    tok = next((v for (k, v) in pairs if k == "token"), None)
    if bearer_tier(tok or "") == "operator":
        from graph.fleet.service_token import resolve_service_token

        fleet = resolve_service_token()
        pairs = [(k, v) for (k, v) in pairs if k != "token"] + [("token", fleet)]
        return urlencode(pairs), True
    if tok is not None:
        return raw_query, False  # a token was offered and it isn't operator — don't lend the socket
    return raw_query, True  # no token (ticket-based plugin) — the member self-authenticates


async def forward_ws(slug: str, ws, path: str) -> None:
    """Reverse-proxy a **WebSocket** to the agent named by ``slug`` (#883). The HTTP proxy
    above can't carry a WS upgrade (it strips ``Upgrade``/``Connection``), so a plugin's
    live WS — agent_browser's viewport/feed, say — couldn't traverse the hub: HTTP loaded
    the panel but the socket showed "Disconnected". This resolves the slug → member, opens
    a client WS to it (carrying the bearer + subprotocols), and pumps frames both ways.

    **Auth (ADR 0089).** The hub's default-deny auth is an HTTP middleware
    (``A2AAuthMiddleware`` is a Starlette ``BaseHTTPMiddleware``, which skips non-HTTP scopes),
    so this ``@app.websocket`` route runs with NO hub auth. ``_member_ws_query`` restores it for
    a LOCAL member: a presented ``?token=`` is authenticated at the hub and swapped for the fleet
    service token the member expects (its plugin WS validates against the member's own bearer,
    now the fleet token); a token that doesn't authenticate is refused. **Remote members are NOT
    proxied over WS**: the hub would attach the remote's stored bearer (``_target_for_slug``) and
    lend an unauthenticated caller a ride into the remote's authed sockets (e.g. a terminal
    plugin's PTY); until a remote handshake is authenticated end-to-end, use ``delegate_to`` /
    a direct connection to the remote instead.
    """
    import websockets

    # Refuse WS to a remote member (see docstring). A live LOCAL peer takes precedence over a
    # same-slug remote in _target_for_slug, so only refuse when the slug resolves to a remote
    # (not a running local process). host/local peers fall through and proxy as before.
    live_local = supervisor._load_state().get(slug)
    if not (live_local and supervisor._alive(live_local.get("pid"))) and supervisor.remote_for_slug(slug):
        log.info("[fleet] refusing WS proxy to remote member %r (hub auth is HTTP-only)", slug)
        await ws.close(code=1008, reason="websocket proxying to a remote member is disabled")
        return

    target = _target_for_slug(slug)
    if target is None:
        await ws.close(code=1011, reason=f"agent {slug!r} is not running")
        return
    base, extra = target
    ws_base = "ws" + base[len("http") :]  # http(s):// → ws(s)://
    # Authenticate + swap the ?token= credential for a local member (ADR 0089): the member's
    # plugin WS validates it against the member's own (fleet) bearer, which the console's
    # operator bearer no longer matches. A presented-but-unauthenticated token is refused here.
    query, allowed = _member_ws_query(slug, ws.url.query)
    if not allowed:
        log.info("[fleet] refusing WS to %r — presented token is not an operator credential", slug)
        await ws.close(code=1008, reason="unauthorized")
        return
    upstream_url = f"{ws_base}/{path}" + (f"?{query}" if query else "")

    headers = dict(extra)  # a remote member's bearer; else carry the browser's
    auth = ws.headers.get("authorization")
    if auth and not any(k.lower() == "authorization" for k in headers):
        headers["authorization"] = auth
    sub = ws.headers.get("sec-websocket-protocol")
    subprotocols = [s.strip() for s in sub.split(",") if s.strip()] if sub else None

    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=headers or None,
            subprotocols=subprotocols,
            open_timeout=5,
            ping_interval=None,
            max_size=None,
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
