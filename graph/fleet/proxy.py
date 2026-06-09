"""Reverse proxy for the in-place switch (ADR 0042 slice 2b).

The hub forwards console traffic to the **active** agent under the ``/active/<path>``
prefix — so switching agents is a single ``activate`` call that re-points where
``/active/*`` lands (chat → ``/active/api/chat``, SSE → ``/active/api/events``, A2A →
``/active/a2a``). The active pointer lives in ``<workspaces_root>/fleet-active`` so it
survives restarts; it only resolves while that agent is actually running.

Streaming-safe: responses (incl. SSE) are piped through unbuffered, and the upstream
client is closed when the stream ends.
"""

from __future__ import annotations

import logging
import time

import httpx
from starlette.responses import JSONResponse, StreamingResponse

from graph.fleet import supervisor
from graph.workspaces import manager

log = logging.getLogger("protoagent.server")

# Headers we must not copy verbatim across the proxy boundary.
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
        "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


def _active_path():
    return manager.workspaces_root() / "fleet-active"


def get_active() -> str | None:
    """The active agent name, or None — only if it's recorded *and* still running."""
    f = _active_path()
    if not f.exists():
        return None
    name = f.read_text().strip()
    return name if name and supervisor.is_running(name) else None


def set_active(name: str) -> dict:
    """Point the proxy at a running agent. Raises FleetError if it isn't running."""
    name = manager._safe(name)
    if not supervisor.is_running(name):
        raise supervisor.FleetError(f"{name!r} is not running — start it first")
    _active_path().write_text(name)
    invalidate_port_cache()  # a switch must take effect at once (#7 cache)
    log.info("[fleet] active agent → %s", name)
    return {"active": name}


def clear_active() -> dict:
    """Focus the host (this instance) — drop the proxy pointer so the console talks to
    ``/api`` directly again, with no peer focused (ADR 0042)."""
    f = _active_path()
    if f.exists():
        f.unlink()
    invalidate_port_cache()  # focusing the host must take effect at once (#7 cache)
    log.info("[fleet] active agent → host (cleared)")
    return {"active": None}


# Active-port cache (#7) — forward() runs per proxied request (every chat POST, panel fetch,
# SSE), so don't pay a full supervisor.status() scan (workspaces dir + YAML parse + os.kill per
# pid) each time. Resolve the port straight from the active record with a 1s TTL, invalidated
# immediately on a switch so a focus change still takes effect at once.
_port_cache: dict = {"name": None, "port": None, "at": 0.0}


def invalidate_port_cache() -> None:
    _port_cache["at"] = 0.0


def _active_port() -> int | None:
    now = time.monotonic()
    if now - _port_cache["at"] < 1.0:
        return _port_cache["port"]
    name = get_active()  # verifies the agent is still running
    port = (supervisor._load_state().get(name) or {}).get("port") if name else None
    _port_cache.update(name=name, port=port, at=now)
    return port


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


async def forward(request, path: str):
    """Reverse-proxy to the single active agent (back-compat /active/* route, superseded by
    /agents/<slug>/* slug routing). 409 if no agent is active."""
    port = _active_port()
    if port is None:
        return JSONResponse(
            {"detail": "no active agent — start one and POST /api/fleet/{name}/activate"},
            status_code=409)
    return await _forward_to_port(port, request, path)
