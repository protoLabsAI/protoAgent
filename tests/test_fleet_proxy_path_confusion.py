"""Fleet-proxy path-confusion defence (security).

The ``/agents/<slug>/{path:path}`` route captures the sub-path DECODED, and httpx (HTTP) /
``websockets`` (WS) then re-handle it before dialling the member. That double-handling used to
split the path the hub makes its auth/exemption decision on from the path the member's router
dispatches — e.g. the hub admits ``/agents/<slug>/plugins/foo/%2e%2e/%2e%2e/api/config``
anonymously (it *starts with* the public ``/plugins/foo/`` prefix), httpx collapses the ``..``,
and the member receives ``/api/config``. The member's own default-deny auth re-checks and 401s,
so it was contained — but the hub should never green-light one resource and forward another.

These tests drive the REAL proxy against a REAL member ASGI app on a real socket (a uvicorn
server in a background thread), proving the hub now (a) refuses ambiguous encodings with 400
before any dial and (b) forwards the raw path so the member decodes it exactly once — the same
bytes the hub authorised, identical to a direct caller.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from a2a_impl import auth
from graph.fleet import member_public, proxy


@pytest.fixture(autouse=True)
def _trusted_host(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_TRUSTED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _clear_caches():
    # The proxy caches one httpx AsyncClient in a module global; TestClient spins a fresh event
    # loop per instance, so a client bound to a previous loop raises "Event loop is closed" when
    # its pool unwinds. Drop it around each test (prod reuses one loop; this is harness hygiene).
    proxy._client = None
    member_public._cache.clear()
    proxy._slug_cache.clear()
    yield
    proxy._client = None
    member_public._cache.clear()
    proxy._slug_cache.clear()


def _member_app() -> FastAPI:
    """A real member: a public plugin-view subtree, a well-known public-paths list, and an echo
    catch-all that reports the path its OWN router dispatched (so a test can see exactly what
    reached the member)."""
    app = FastAPI()

    @app.get("/.well-known/protoagent/public-paths", include_in_schema=False)
    async def _wk():
        return JSONResponse({"public_paths": ["/plugins/foo/"]})

    @app.api_route("/{p:path}", methods=["GET", "POST"])
    async def _echo(p: str, request: Request):
        return {"dispatched": "/" + p, "raw": request.scope.get("raw_path", b"").decode("latin-1")}

    return app


@pytest.fixture
def member():
    """Serve the member app on a free port in a background thread; yield its base URL."""
    config = uvicorn.Config(_member_app(), host="127.0.0.1", port=0, log_level="error", loop="asyncio")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # not the main thread
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.started and server.servers and server.servers[0].sockets:
                break
            time.sleep(0.02)
        assert server.started, "member server didn't start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _hub(monkeypatch, member_base: str):
    """A hub whose proxy resolves slug ``m`` → the real member, fronted by the real auth
    middleware with an operator bearer configured."""
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (member_base, {}) if slug == "m" else None)
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", member_base, None) if slug == "m" else None)
    auth.set_bearer_token("hub-tok")
    auth.set_member_public_resolver(member_public.is_member_public)

    app = FastAPI()

    @app.api_route("/agents/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _p(slug: str, path: str, request: Request):
        return await proxy.forward_to(slug, request, path)

    @app.websocket("/agents/{slug}/{path:path}")
    async def _ws(ws: WebSocket, slug: str, path: str):
        await proxy.forward_ws(slug, ws, path)

    auth.install(app, bearer_token="hub-tok", api_key="", allowed_origins_raw="")
    return app


# ── the headline confusion case ────────────────────────────────────────────────

def test_dotdot_in_public_prefix_cannot_reach_api_config(monkeypatch, member):
    """``/plugins/foo/%2e%2e/%2e%2e/api/config`` — admitted by the hub's member-public prefix,
    would collapse to ``/api/config`` at the member. The hub must refuse it outright (400),
    never forward it."""
    client = TestClient(_hub(monkeypatch, member))
    r = client.post("/agents/m/plugins/foo/%2e%2e/%2e%2e/api/config")
    assert r.status_code == 400, r.text
    assert "ambiguous path" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "target",
    [
        "/agents/m/plugins/foo/%2e%2e/api/config",  # encoded dot-dot
        "/agents/m/plugins/foo/bar%2fbaz",  # encoded slash
        "/agents/m/plugins/foo/bar%5cbaz",  # encoded backslash
        "/agents/m/api/plugins/%2563ampaign/x",  # double-encoding (%25)
        "/agents/m/plugins/foo/bar%00baz",  # encoded NUL
    ],
)
def test_ambiguous_encodings_refused_with_400(monkeypatch, member, target):
    client = TestClient(_hub(monkeypatch, member))
    # Present the operator bearer: proves the 400 is the path guard, not an auth failure.
    r = client.get(target, headers={"Authorization": "Bearer hub-tok"})
    assert r.status_code == 400, r.text


def test_literal_backslash_refused(monkeypatch, member):
    client = TestClient(_hub(monkeypatch, member))
    r = client.get("/agents/m/plugins/foo/a\\b", headers={"Authorization": "Bearer hub-tok"})
    assert r.status_code == 400, r.text


# ── no regression: legitimate paths forward raw and decode exactly once ─────────

def test_member_public_page_forwards_unchanged(monkeypatch, member):
    """A real anonymous member-public view page still proxies and dispatches at the member."""
    client = TestClient(_hub(monkeypatch, member))
    r = client.get("/agents/m/plugins/foo/view")
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] == "/plugins/foo/view"


def test_encoded_space_is_forwarded_raw_and_decoded_once(monkeypatch, member):
    """A legitimate ``%20`` rides through as the raw bytes and the member decodes it exactly
    once — proof the proxy forwards the raw path rather than a hub-decoded-then-re-encoded one."""
    client = TestClient(_hub(monkeypatch, member))
    r = client.get("/agents/m/plugins/foo/a%20b")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dispatched"] == "/plugins/foo/a b"  # member decoded once → a space
    assert body["raw"] == "/plugins/foo/a%20b"  # arrived still-encoded (single decode)


def test_authed_api_still_proxies(monkeypatch, member):
    client = TestClient(_hub(monkeypatch, member))
    r = client.get("/agents/m/api/config", headers={"Authorization": "Bearer hub-tok"})
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] == "/api/config"


# ── WebSocket lane: same contract ───────────────────────────────────────────────

def test_ws_ambiguous_path_is_refused(monkeypatch, member):
    """The WS proxy refuses the same encodings before dialling (close, no upstream connect)."""
    app = _hub(monkeypatch, member)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/agents/m/plugins/foo/%2e%2e/%2e%2e/api/events"):
            pass
