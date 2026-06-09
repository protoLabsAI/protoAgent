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
    log.info("[fleet] active agent → %s", name)
    return {"active": name}


def _active_port() -> int | None:
    name = get_active()
    if not name:
        return None
    return next((w["port"] for w in supervisor.status()
                 if w["name"] == name and w["running"]), None)


async def forward(request, path: str):
    """Reverse-proxy ``request`` to ``/<path>`` on the active agent, streaming the
    response back (SSE-safe). 409 if no agent is active."""
    port = _active_port()
    if port is None:
        return JSONResponse(
            {"detail": "no active agent — start one and POST /api/fleet/{name}/activate"},
            status_code=409)

    url = f"http://127.0.0.1:{port}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}

    client = httpx.AsyncClient(timeout=httpx.Timeout(None))
    upstream_req = client.build_request(
        request.method, url, headers=headers, content=body,
        params=dict(request.query_params))
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        await client.aclose()
        return JSONResponse({"detail": "active agent is not reachable"}, status_code=502)

    async def _pipe():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(_pipe(), status_code=upstream.status_code, headers=resp_headers)
