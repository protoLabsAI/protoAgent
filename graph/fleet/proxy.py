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


# Per-slug port resolution (ADR 0042 slug routing) — each console window targets an agent by URL
# slug (/agents/<slug>/…) instead of a single global "active". 'host' = this instance; a peer =
# its workspace port. 1s TTL cache, keyed by slug, to keep the proxy hot path cheap.
_slug_cache: dict = {}


def _port_for_slug(slug: str) -> int | None:
    now = time.monotonic()
    hit = _slug_cache.get(slug)
    if hit and now - hit[1] < 1.0:
        return hit[0]
    if slug == "host":
        from runtime.state import STATE
        port = getattr(STATE, "active_port", None)
    else:
        rec = supervisor._load_state().get(slug)
        port = rec.get("port") if rec and supervisor._alive(rec.get("pid")) else None
    _slug_cache[slug] = (port, now)
    return port


async def _forward_to_port(port: int, request, path: str):
    """Stream-proxy ``request`` to ``/<path>`` on 127.0.0.1:<port> (SSE-safe)."""
    url = f"http://127.0.0.1:{port}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}

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
    routing). ``host`` targets this instance; 409 if the agent isn't running."""
    port = _port_for_slug(slug)
    if port is None:
        return JSONResponse({"detail": f"agent {slug!r} is not running"}, status_code=409)
    return await _forward_to_port(port, request, path)
