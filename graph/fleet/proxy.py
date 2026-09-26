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

Bounded: a member that accepts a connection and then stalls gets a 504 rather than parking
the socket forever — see the read-timeout lanes below (#2590).
"""

from __future__ import annotations

import contextlib
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


# Read-timeout lanes for proxied traffic (#2590).
#
# The failure this bounds: a member ACCEPTS the connection and then never answers (its event
# loop is busy — a board agent running the repo's whole gate, say). The client used to be built
# with ``Timeout(None, connect=5.0)`` and a comment claiming the finite connect timeout stopped
# "a peer that accepts then stalls" from hanging things. It does not, and could not: a connect
# timeout bounds the handshake, and "accepts then stalls" IS the read phase. So the hub waited
# forever. The console's board view re-fetches on a ~3s poll, each poll parked another
# connection, and once six were parked the browser's per-origin cap meant it could issue no
# request to the console origin at all — a transient member stall froze the whole app, with
# force-quit the only recovery.
#
# One global value can't serve every shape of proxied request, because httpx applies the read
# timeout to EVERY socket read — including the wait for the next SSE event. So it's chosen per
# request:
_STREAM_TIMEOUT = httpx.Timeout(None, connect=5.0)  # SSE: idle between events is normal
_TURN_TIMEOUT = httpx.Timeout(600.0, connect=5.0)  # a whole agent turn, non-streaming
_READ_TIMEOUT = httpx.Timeout(20.0, connect=5.0)  # views, API reads — the polls that wedged it

# Paths that are long-lived BY DESIGN and must not be bounded like a view read. Matched on the
# proxied sub-path (``/agents/<slug>/<path>`` ⇒ ``a2a``, ``api/events``, ``api/chat``).
#
# Why paths and not just the Accept header: the console streams A2A turns through `fetch`, and
# that request does NOT send ``Accept: text/event-stream`` (only EventSource does). Keying the
# unbounded lane on the header alone would have bounded live member chat at 20s. The header is
# still honored as an ADDITIONAL signal, so a plugin's own well-behaved SSE endpoint streams
# even though it isn't on this list.
_STREAM_PATHS = frozenset({"a2a", "api/events"})
_TURN_PATHS = frozenset({"api/chat"})


def _timeout_for(request, path: str) -> httpx.Timeout:
    """Which read-timeout lane this proxied request belongs to (#2590)."""
    norm = path.strip("/")
    if norm in _STREAM_PATHS or "text/event-stream" in (request.headers.get("accept") or "").lower():
        return _STREAM_TIMEOUT
    if norm in _TURN_PATHS:
        return _TURN_TIMEOUT
    return _READ_TIMEOUT


# Shared client (#8) — one pooled AsyncClient instead of a fresh one (TCP setup + FD churn) per
# request. Its default is the permissive lane, for the few callers that reuse the client
# directly (they pass their own bounded ``timeout=``); every proxied request overrides it with
# ``_timeout_for``.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT)
    return _client


# Per-slug target resolution (ADR 0042 slug routing) — each console window targets an agent by
# URL slug (/agents/<slug>/…) instead of a single global "active". 'host' = this instance; a
# local peer = its workspace port; a REMOTE member = its registered URL (+ its bearer, if one
# was stored — replacing the browser's Authorization, which carries the HUB's token, not the
# remote's). 1s TTL cache, keyed by slug, to keep the proxy hot path cheap.
_slug_cache: dict = {}
# Slugs whose last resolution was a REMOTE member (kept beside the cache rather than in the
# ``(base, extra)`` tuple so every existing caller keeps its shape). A remote with no stored
# token has an EMPTY ``extra`` — indistinguishable from a local peer by the headers alone —
# and ``forward_to`` must never hand such a remote the fleet service token (see there).
_remote_slugs: set[str] = set()


def _resolve_slug(slug: str) -> tuple[str, str, str | None] | None:
    """Uncached ``(kind, base_url, stored_token)`` for a slug, or None when it isn't reachable.

    ``kind`` is ``"host"`` (this instance), ``"local"`` (a live local peer) or ``"remote"`` (a
    registered remote member). Precedence is host → live local → remote, so a running local
    peer shadows a same-slug remote. ``stored_token`` is the remote's stored bearer (None for
    host/local, and for a remote registered without one). One derivation shared by the HTTP
    target below and ``forward_ws`` — the WS path must know the KIND, not guess it from whether
    an Authorization header happened to be attached (a tokenless remote carries none)."""
    if slug == "host":
        from runtime.state import STATE

        port = getattr(STATE, "active_port", None)
        return ("host", f"http://127.0.0.1:{port}", None) if port else None
    rec = supervisor._load_state().get(slug)
    if rec and supervisor._alive(rec.get("pid")):
        return ("local", f"http://127.0.0.1:{rec['port']}", None)
    remote = supervisor.remote_for_slug(slug)
    if remote:
        return ("remote", remote["url"], remote.get("token") or None)
    return None


def _target_for_slug(slug: str) -> tuple[str, dict] | None:
    """``(base_url, extra_headers)`` for a slug, or None when it isn't reachable."""
    now = time.monotonic()
    hit = _slug_cache.get(slug)
    if hit and now - hit[1] < 1.0:
        return hit[0]
    target: tuple[str, dict] | None = None
    resolved = _resolve_slug(slug)
    kind = resolved[0] if resolved is not None else None
    if resolved is not None:
        _kind, base, stored = resolved
        target = (base, {"authorization": f"Bearer {stored}"} if stored else {})
    # ``_remote_slugs`` is driven by the same one derivation ``forward_ws`` uses — the KIND, never
    # "did an Authorization header get attached" (a tokenless remote carries none).
    if kind == "remote":
        _remote_slugs.add(slug)
    else:
        _remote_slugs.discard(slug)
    _slug_cache[slug] = (target, now)
    return target


# Idle keepalive for proxied SSE lanes (Swap & Resume S4) — see _pipe below.
_SSE_KEEPALIVE_S = 30.0


async def _forward_to_base(
    base: str, request, path: str, extra_headers: dict | None = None, *, drop_params: frozenset[str] = frozenset()
):
    """Stream-proxy ``request`` to ``<base>/<path>`` (SSE-safe). ``drop_params`` names query
    params that must not ride upstream (a remote never gets the hub-signed ``?token=``)."""
    url = f"{base}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    if extra_headers:
        # A header in extra REPLACES the caller's — drop any case-variant first, else the
        # upstream carries both (dict keys are case-sensitive, HTTP header names aren't) and
        # a swapped Authorization would sit BESIDE the caller's instead of overriding it.
        # A None value drops the header outright (a tokenless remote's Authorization).
        overridden = {k.lower() for k in extra_headers}
        headers = {k: v for k, v in headers.items() if k.lower() not in overridden}
        headers.update({k: v for k, v in extra_headers.items() if v is not None})

    client = _get_client()
    upstream_req = client.build_request(
        request.method,
        url,
        headers=headers,
        content=body,
        params={k: v for k, v in dict(request.query_params).items() if k not in drop_params},
        timeout=_timeout_for(request, path),
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "agent is not reachable"}, status_code=502)
    except httpx.ReadTimeout:
        # Accepted the connection, then went silent. Answer instead of parking the socket —
        # a parked one is what let a busy member exhaust the browser's per-origin connection
        # cap and freeze the console (#2590). 504 so the panel can render a real error.
        log.warning("[fleet] proxied %s %s timed out waiting for the agent to respond", request.method, url)
        return JSONResponse({"detail": "agent did not respond in time"}, status_code=504)

    # A swapped-away client is only DETECTED on the next downstream write, and the
    # unbounded stream lanes (a2a / api/events) can sit silent for minutes inside a
    # long tool call — an abandoned member stream used to park indefinitely (Swap &
    # Resume S4). For SSE responses, inject a comment keepalive after 30s of upstream
    # silence: the write to a dead client raises, the generator closes, and the
    # member-side connection unwinds within one keepalive period. SSE-only — a
    # comment line is protocol-legal there and corruption anywhere else.
    is_sse = (upstream.headers.get("content-type") or "").startswith("text/event-stream")

    async def _pipe():
        import asyncio

        try:
            if not is_sse:
                async for chunk in upstream.aiter_raw():
                    yield chunk
                return
            done = object()
            queue: asyncio.Queue = asyncio.Queue(maxsize=16)

            async def _reader() -> None:
                try:
                    async for chunk in upstream.aiter_raw():
                        await queue.put(chunk)
                except httpx.ReadTimeout:
                    log.warning(
                        "[fleet] proxied %s %s stalled mid-response — closing the stream", request.method, url
                    )
                except Exception:  # noqa: BLE001 — reader end = stream end; the finally closes upstream
                    pass
                finally:
                    await queue.put(done)

            reader = asyncio.create_task(_reader())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"  # liveness probe: raises when the client left
                        continue
                    if item is done:
                        break
                    yield item
            finally:
                reader.cancel()
                with contextlib.suppress(BaseException):
                    await reader
        except httpx.ReadTimeout:
            # Stalled mid-body (non-SSE lane). The status line is already sent, so the only
            # honest signal left is to end the response rather than hold the connection open.
            log.warning("[fleet] proxied %s %s stalled mid-response — closing the stream", request.method, url)
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
    # nothing (the browser's own header rides through unless we swap it below). A remote
    # with NO stored token also carries nothing, so the headers alone can't tell it from a
    # local peer — ``_remote_slugs`` (set from the remote resolution) does.
    is_remote = bool(extra.get("authorization")) or slug in _remote_slugs
    tier = getattr(state, "trust_tier", None)
    member_public = getattr(state, "member_public", False)
    drop_params: frozenset[str] = frozenset()
    if is_remote:
        # A REMOTE is off this box and outside the hub's trust boundary: nothing the hub holds
        # or the caller presented rides there except the remote's OWN stored bearer, and that
        # only on behalf of an OPERATOR caller the hub already authenticated.
        #  - member_public (#1890) is stamped BEFORE any credential check, off a public list
        #    the REMOTE controls — so the request may still carry the caller's hub bearer or
        #    device token. Forward it with NO Authorization (``None`` drops the caller's; an
        #    empty ``extra`` would have let it ride through — a compromised remote could list a
        #    path as public and harvest the hub's operator token).
        #  - A federation-tier caller (ADR 0066: /a2a, /v1, /plugins only) must not be lent a
        #    stored bearer that is OPERATOR on the remote — that elevates it across the hop.
        #  - A remote the hub holds no token for (unpaired, ADR 0113) gets no credential
        #    rather than the caller's: a D4 delegate presents the loopback-only fleet token,
        #    which must never leave the box. Anonymous is honest — an open remote answers, a
        #    secured one 401s and the delegate error says to pair it.
        # Open hub (no credential configured): every caller is ``operator``, so the stored
        # bearer is lent to anyone who can reach the hub — pre-existing and accepted: an open
        # hub already hands its whole console, remotes included, to whoever reaches it.
        if member_public or tier != "operator" or not extra.get("authorization"):
            extra = {"authorization": None}  # None = drop the caller's header (_forward_to_base)
        # The hub-signed SSE ``?token=`` (30s, HMAC'd with the HUB's bearer) authenticated the
        # caller HERE; forwarded, the remote could replay it against the hub. The proxied call
        # authenticates to the remote with the stored bearer instead (#1607).
        drop_params = frozenset({"token"})
    elif member_public:
        # A LOCAL member's public path: forward without lending the fleet token (#1890). The
        # caller's own header is left as it was — a local member is inside this box's trust
        # boundary (same user, spawned by this hub, already holding the fleet token that
        # outranks anything the caller could carry), so there is nothing to protect by
        # stripping it and a plugin view that reads it would change behavior.
        extra = {k: v for k, v in extra.items() if k.lower() != "authorization"}
    elif tier == "operator":
        # ADR 0089 D3: the hub already authenticated this operator caller. Present a LOCAL
        # member with the fleet service token in place of the caller's credential — a device
        # token the member's own registry (a different instance_root) can't verify, which is
        # why proxied plugin calls to sister agents 401'd. Swap only for the operator tier
        # (never elevate a lesser credential) and only for a local peer.
        from graph.fleet.service_token import resolve_service_token

        extra = {**extra, "authorization": f"Bearer {resolve_service_token()}"}
    if path == "a2a" and request.method == "POST":
        # A turn is starting (or being resumed/queried) on this member — refresh its
        # LRU recency so the warm-cap's grace window (Swap & Resume S4) actually
        # covers agents that are WORKING, not just ones the operator clicked.
        with contextlib.suppress(Exception):
            supervisor.touch(slug)
    if drop_params:
        return await _forward_to_base(base, request, path, extra, drop_params=drop_params)
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
    """Rewrite a WS handshake's query for a HOST or LOCAL-member target (ADR 0089).

    A member's plugin WS (terminal PTY, say) validates a ``?token=`` param against the member's
    OWN inbound bearer — which is now the fleet service token (D5) — but the console opens the
    socket with the *operator* bearer, so post-D5 that token mismatches and the member refuses
    the socket. Mirror what ``forward_to`` does for HTTP: authenticate the presented token at the
    hub and, if it's an operator credential, swap it for the fleet token the member expects.

    Returns ``(query, allowed)``; ``allowed=False`` ⇒ a token was presented that does not
    authenticate as operator — close the socket rather than proxy it. Pass-through unchanged for:
    the ``host`` slug (its plugins expect the operator bearer, not the fleet token) and
    ticket-based plugins that carry no ``token`` param (agent_browser mints a member-side ticket
    over HTTP — already correct — so the hub must not gate them). A REMOTE member never comes
    through here: ``forward_ws`` routes it to ``_remote_ws_handshake``, because the fleet token is
    a loopback-only credential that must never leave this machine (ADR 0089), and a remote gets
    its own stored bearer instead — under stricter rules (ADR 0113 D6)."""
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


def _remote_ws_handshake(
    raw_query: str, auth_header: str | None, stored_token: str | None
) -> tuple[str, dict, str | None]:
    """Authenticate + rewrite a WS handshake bound for a REMOTE member (ADR 0113 D6).

    Returns ``(query, headers, refusal)``: ``refusal`` is a close reason (the caller closes 1008
    and dials nothing) or None; ``headers`` is the COMPLETE header set for the upstream upgrade.

    The one rule: **the hub never attaches the remote's stored bearer to an upgrade on its own.**
    This route runs outside the HTTP auth middleware, so nobody has authenticated the caller —
    #1607 was exactly the hub attaching the stored bearer anyway, which lent an anonymous caller
    a ride into the remote's terminal PTY. The stored bearer now only ever REPLACES a credential
    the caller presented AND the hub authenticated as operator, in the slot it was presented in:

    - **No stored token** → refuse. With nothing to authenticate against, the hub would be a
      blind pipe into an open instance.
    - **``?token=``** → ``bearer_tier`` at the hub. Operator ⇒ swapped for the stored token
      (the remote's plugin validates ``?token=`` against its OWN bearer — the same reason the
      local-member path swaps in the fleet token). Anything else, including an empty value ⇒
      refuse. Never the fleet token: that is loopback-only and must not leave this machine.
    - **``Authorization`` header** → the same treatment. A browser can't set headers on a WS,
      so this is a server-to-server caller (a script holding the hub's bearer). Its header
      carries a HUB credential, which is meaningless at the remote and a leak if forwarded, so
      it is NEVER forwarded as-is. Operator ⇒ replaced by ``Bearer <stored>`` — parity with
      HTTP ``forward_to``, where an authenticated caller's header is replaced by the stored
      bearer — and anything else (wrong token, non-Bearer scheme) ⇒ refuse, rather than
      silently stripping it and proxying the caller as anonymous: a caller that presented a
      credential expects it to count, and a failed one is a policy violation, not a fallback.
    - **Neither** (ticket-based plugins — agent_browser's ``?ticket=``, the terminal's in-band
      ticket frame) → pass through with NO Authorization at all. The ticket was minted over the
      authenticated HTTP proxy, and the remote checks it itself. Unlike the local path, an
      OPEN hub does not inject a credential here either: "open" makes every presented token
      operator, but it never makes the hub present one on the caller's behalf.

    Every presented credential must authenticate — a good ``?token=`` doesn't excuse a bad
    header. So an unauthenticated caller gets nothing through the hub it couldn't get by dialling
    the remote directly (the property #1607 protected). Subprotocols are passed through by the
    caller unchanged and the hub never writes a credential into them (no in-tree plugin carries
    a token in ``Sec-WebSocket-Protocol``; the terminal plugin authenticates in-band)."""
    from urllib.parse import parse_qsl, urlencode

    from a2a_impl.auth import bearer_tier

    if not stored_token:
        return raw_query, {}, "remote member has no stored token; websocket proxying refused"
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    headers: dict = {}
    tokens = [v for (k, v) in pairs if k == "token"]
    if tokens:
        if any(bearer_tier(t or "") != "operator" for t in tokens):
            return raw_query, {}, "unauthorized"
        pairs = [(k, v) for (k, v) in pairs if k != "token"] + [("token", stored_token)]
        raw_query = urlencode(pairs)
    if auth_header is not None:
        scheme, _, cred = auth_header.strip().partition(" ")
        if scheme.lower() != "bearer" or bearer_tier(cred.strip()) != "operator":
            return raw_query, {}, "unauthorized"
        headers["authorization"] = f"Bearer {stored_token}"
    return raw_query, headers, None


async def forward_ws(slug: str, ws, path: str) -> None:
    """Reverse-proxy a **WebSocket** to the agent named by ``slug`` (#883). The HTTP proxy
    above can't carry a WS upgrade (it strips ``Upgrade``/``Connection``), so a plugin's
    live WS — agent_browser's viewport/feed, say — couldn't traverse the hub: HTTP loaded
    the panel but the socket showed "Disconnected". This resolves the slug → member, opens
    a client WS to it (carrying the credential + subprotocols), and pumps frames both ways.

    **Auth.** The hub's default-deny auth is an HTTP middleware (``A2AAuthMiddleware`` is a
    Starlette ``BaseHTTPMiddleware``, which skips non-HTTP scopes), so this ``@app.websocket``
    route runs with NO hub auth, and every credential decision is made here:

    - **host / local member (ADR 0089)** — ``_member_ws_query``: a presented ``?token=`` is
      authenticated at the hub and (for a local member) swapped for the fleet service token the
      member expects; a token that doesn't authenticate is refused. The caller's own
      Authorization header rides through (the hub attaches no stored credential here).
    - **remote member (ADR 0113 D6)** — ``_remote_ws_handshake``: the hub never lends the
      remote's stored bearer on its own; it only swaps it in for a credential it authenticated
      as operator, passes ticket-based sockets through with no Authorization, and refuses a
      remote registered with no token. (#1607 refused remotes outright; D6 re-enables them.)

    There is no WS analog of ``member_public``: that flag marks an HTTP request the middleware
    admitted anonymously, so ``forward_to`` strips the stored bearer from it. Every WS arrives
    unauthenticated, so the remote path already treats a credential-less socket exactly that
    way — forwarded anonymous — and only an authenticated one gets the swap.
    """
    import websockets

    # Uncached, and the URL comes from the same record as the stored token: a cached target
    # that went stale between "which kind is this?" and "where do I dial?" must not be able to
    # send one member's credential to another.
    resolved = _resolve_slug(slug)
    if resolved is None:
        await ws.close(code=1011, reason=f"agent {slug!r} is not running")
        return
    kind, base, stored = resolved
    ws_base = "ws" + base[len("http") :]  # http(s):// → ws(s)://
    auth = ws.headers.get("authorization")

    if kind == "remote":
        query, headers, refusal = _remote_ws_handshake(ws.url.query, auth, stored)
        if refusal is not None:
            log.info("[fleet] refusing WS to remote member %r — %s", slug, refusal)
            await ws.close(code=1008, reason=refusal)
            return
    else:
        # Authenticate + swap the ?token= credential for a local member (ADR 0089): the member's
        # plugin WS validates it against the member's own (fleet) bearer, which the console's
        # operator bearer no longer matches. A presented-but-unauthenticated token is refused.
        query, allowed = _member_ws_query(slug, ws.url.query)
        if not allowed:
            log.info("[fleet] refusing WS to %r — presented token is not an operator credential", slug)
            await ws.close(code=1008, reason="unauthorized")
            return
        headers = {"authorization": auth} if auth else {}
    upstream_url = f"{ws_base}/{path}" + (f"?{query}" if query else "")

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
