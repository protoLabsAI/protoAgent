"""Open-mode request hardening (#3668): the instance-wide Host allowlist and the JSON
content-type rule on the consumer surfaces, as installed by ``auth.install``.

Runs against a real app assembled the way the server does it (``auth.install`` over routes,
a static mount and WebSocket routes), so the tests pin the ASGI-level behavior — HTTP and
WebSocket scopes both — not just the helper.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from a2a_impl import auth, hosts


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # The conftest trusts ``testserver`` suite-wide; these tests own the allowlist.
    monkeypatch.delenv("PROTOAGENT_TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(hosts, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(hosts, "_own_names", lambda: {"joshs-mbp.local"})
    monkeypatch.setattr(hosts, "_LOGGED_HOSTS", set())
    for name in ("_BEARER", "_FEDERATION", "_FLEET", "_API_KEY", "_ALLOWED_ORIGINS"):
        monkeypatch.setattr(auth, name, list(getattr(auth, name)))


def _app(tmp_path, *, bearer: str = "", origins: str = "") -> FastAPI:
    app = FastAPI()
    (tmp_path / "index.html").write_text("<html>console</html>")

    @app.get("/api/config")
    async def _cfg():
        return {"ok": True}

    @app.post("/a2a")
    async def _a2a(request: Request):
        return {"got": (await request.body()).decode()}

    @app.post("/v1/chat/completions")
    async def _v1(request: Request):
        return {"got": (await request.body()).decode()}

    @app.post("/agents/{slug}/a2a")
    async def _proxied(slug: str):
        return {"slug": slug}

    @app.post("/api/restart")
    async def _restart():
        return {"restarted": True}

    @app.post("/api/echo")
    async def _echo(request: Request):
        return {"got": (await request.body()).decode()}

    @app.websocket("/agents/{slug}/{path:path}")
    async def _ws(ws: WebSocket, slug: str, path: str):
        await ws.accept()
        await ws.send_text(f"hi {slug}")
        await ws.close()

    app.mount("/app", StaticFiles(directory=str(tmp_path), html=True), name="app")
    auth.install(app, bearer_token=bearer, api_key="", allowed_origins_raw=origins)
    return app


async def _req(app, method, path, host, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7870") as c:
        headers = {"host": host, **kw.pop("headers", {})} if host is not None else kw.pop("headers", {})
        return await c.request(method, path, headers=headers, **kw)


_JSON = {"content-type": "application/json"}

# ── Host allowlist ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path, kw",
    [
        ("GET", "/api/config", {}),
        ("POST", "/a2a", {"headers": _JSON, "content": b"{}"}),
        ("POST", "/v1/chat/completions", {"headers": _JSON, "content": b"{}"}),
        ("GET", "/app/", {}),  # the static console
        ("GET", "/healthz", {}),  # a public path is still behind the Host gate
        (
            "OPTIONS",
            "/api/config",
            {"headers": {"origin": "http://evil.example", "access-control-request-method": "POST"}},
        ),
    ],
)
@pytest.mark.parametrize("host", ["evil.example", "evil.example:7870", "localhost.evil.example", "joshs-mbp"])
async def test_open_instance_refuses_an_untrusted_host_everywhere(tmp_path, method, path, kw, host):
    r = await _req(_app(tmp_path), method, path, host, **kw)
    assert r.status_code == 403
    assert r.json() == {"detail": "Forbidden: untrusted Host header"}


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:7870",
        "127.0.0.1.",
        "[::1]:7870",
        "[fe80::1%25en0]:7870",  # RFC 6874 zone id
        "192.168.1.20:7870",
        "100.101.189.45",
        "localhost",
        "LocalHost.:5173",
        "app.localhost:7870",
        "box.tail1234.ts.net",
        "JOSHS-MBP.local:7870",
    ],
)
async def test_open_instance_answers_its_own_hosts(tmp_path, host):
    app = _app(tmp_path)
    assert (await _req(app, "GET", "/api/config", host)).status_code == 200
    r = await _req(app, "GET", "/app/", host)
    assert r.status_code == 200 and "console" in r.text


async def test_trusted_hosts_env_allowed_origins_and_bind_name(tmp_path, monkeypatch):
    app = _app(tmp_path, origins="https://console.example.com")
    assert (await _req(app, "GET", "/api/config", "agent")).status_code == 403
    monkeypatch.setenv("PROTOAGENT_TRUSTED_HOSTS", "Agent, other.example.")
    assert (await _req(app, "GET", "/api/config", "agent:7870")).status_code == 200
    assert (await _req(app, "GET", "/api/config", "OTHER.example")).status_code == 200
    assert (await _req(app, "GET", "/api/config", "console.example.com")).status_code == 200
    assert (await _req(app, "GET", "/api/config", "hub.lan")).status_code == 403
    hosts.set_bind_host("Hub.LAN")
    assert (await _req(app, "GET", "/api/config", "hub.lan:7870")).status_code == 200


def test_missing_host_is_a_non_browser_client_and_passes():
    assert hosts.host_allowed(None)
    assert hosts.host_allowed("  ")


async def test_refusal_is_logged_once_per_host(tmp_path, caplog):
    import logging

    app = _app(tmp_path)
    with caplog.at_level(logging.WARNING, logger="a2a_impl.hosts"):
        for _ in range(3):
            await _req(app, "GET", "/api/config", "evil.example")
    lines = [r for r in caplog.records if "untrusted Host" in r.getMessage()]
    assert len(lines) == 1 and "PROTOAGENT_TRUSTED_HOSTS" in lines[0].getMessage()


# ── WebSocket scopes (no HTTP middleware runs for them) ───────────────────────────────────


# TestClient's WebSocket handshake ignores ``base_url`` (it always says ``testserver``), so
# every test here names its Host explicitly.


@pytest.mark.parametrize("host", ["evil.example", "evil.example:7870", "testserver"])
def test_open_instance_refuses_a_websocket_with_an_untrusted_host(tmp_path, host):
    client = TestClient(_app(tmp_path))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/agents/alice/api/plugins/terminal/ws", headers={"host": host}):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("host", ["127.0.0.1:7870", "localhost:5173", "box.tail1234.ts.net"])
def test_open_instance_accepts_a_websocket_on_its_own_host(tmp_path, host):
    client = TestClient(_app(tmp_path))
    with client.websocket_connect("/agents/alice/x", headers={"host": host}) as ws:
        assert ws.receive_text() == "hi alice"


# ── JSON content type on /a2a + /v1 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/a2a", "/v1/chat/completions", "/agents/alice/a2a"])
@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/plain"},
        {"content-type": "text/plain;charset=UTF-8"},
        {"content-type": "application/x-www-form-urlencoded"},
        {"content-type": "multipart/form-data; boundary=x"},
        {},  # a Blob body with no type sends no Content-Type at all
    ],
)
async def test_open_instance_requires_json_on_the_consumer_surfaces(tmp_path, path, headers):
    r = await _req(_app(tmp_path), "POST", path, "127.0.0.1:7870", headers=headers, content=b'{"jsonrpc":"2.0"}')
    assert r.status_code == 415
    assert "application/json" in r.json()["detail"]


@pytest.mark.parametrize(
    "ctype",
    ["application/json", "Application/JSON; charset=utf-8", "application/a2a+json", "application/json-seq+json"],
)
@pytest.mark.parametrize("path", ["/a2a", "/v1/chat/completions", "/agents/alice/a2a"])
async def test_json_content_types_pass(tmp_path, path, ctype):
    r = await _req(_app(tmp_path), "POST", path, "127.0.0.1:7870", headers={"content-type": ctype}, content=b"{}")
    assert r.status_code == 200


async def test_content_type_rule_is_scoped_to_the_consumer_surfaces(tmp_path):
    app = _app(tmp_path)
    # /api is out of scope for this rule (FastAPI models reject non-JSON bodies on their own).
    r = await _req(app, "POST", "/api/echo", "127.0.0.1", headers={"content-type": "text/plain"}, content=b"x")
    assert r.status_code == 200
    # a lookalike prefix isn't the surface
    assert not hosts._json_surface("/a2ax")
    assert not hosts._json_surface("/v1")
    assert not hosts._json_surface("/agents//a2a")
    assert hosts._json_surface("/a2a/v1/message:send")


# ── Cross-site state-changing requests + WebSockets ──────────────────────────────────────

_H = "127.0.0.1:7870"


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors"},  # blind no-cors POST, no Origin
        {"origin": "https://evil.example"},
        {"origin": "https://evil.example", "sec-fetch-site": "same-origin"},  # Origin decides
        {"origin": "null"},  # a fully sandboxed frame
        {"origin": "http://127.0.0.1:7870.evil.example"},
        {"origin": "tauri://evil"},
    ],
)
@pytest.mark.parametrize("path", ["/api/restart", "/api/echo", "/a2a"])
async def test_open_instance_refuses_cross_site_posts(tmp_path, headers, path):
    # Empty body: FastAPI never parses it, so without the gate a bodyless route just runs.
    r = await _req(_app(tmp_path), "POST", path, _H, headers={**headers, **_JSON})
    assert r.status_code == 403
    assert r.json() == {"detail": "Forbidden: cross-site request"}


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl / SDKs / the hub's loopback calls: no Origin, no Fetch Metadata
        {"origin": "http://127.0.0.1:7870", "sec-fetch-site": "same-origin"},  # the console + its plugin iframes
        {"origin": "https://127.0.0.1:7870"},  # scheme-agnostic same-origin (TLS front)
        {"origin": "tauri://localhost", "sec-fetch-site": "cross-site"},  # desktop (macOS/Linux)
        {"origin": "http://tauri.localhost", "sec-fetch-site": "cross-site"},  # desktop (Windows)
        {"origin": "http://localhost:5173", "sec-fetch-site": "same-site"},  # Vite dev server origin
        {"origin": "http://127.0.0.1:7871"},  # a sibling console the CORS policy already grants
        {"sec-fetch-site": "same-origin"},
        {"sec-fetch-site": "none"},
    ],
)
async def test_open_instance_accepts_its_own_posts(tmp_path, headers):
    r = await _req(_app(tmp_path), "POST", "/api/restart", _H, headers=headers)
    assert r.status_code == 200 and r.json() == {"restarted": True}


async def test_allowed_origins_list_admits_its_origin(tmp_path):
    app = _app(tmp_path, origins="https://console.example.com")
    ok = await _req(app, "POST", "/api/restart", _H, headers={"origin": "https://console.example.com"})
    assert ok.status_code == 200
    bad = await _req(app, "POST", "/api/restart", _H, headers={"origin": "https://other.example"})
    assert bad.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
        {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"},
        {"origin": "https://evil.example"},  # a GET must not change state; CORS guards the read
    ],
)
async def test_gets_stay_open_to_navigation_and_subresources(tmp_path, headers):
    r = await _req(_app(tmp_path), "GET", "/api/config", _H, headers=headers)
    assert r.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost.evil.example"])
def test_open_instance_refuses_a_cross_site_websocket(tmp_path, origin):
    client = TestClient(_app(tmp_path))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/agents/alice/x", headers={"host": _H, "origin": origin}):
            pass
    assert exc.value.code == 1008


def test_a_websocket_with_no_origin_but_cross_site_fetch_metadata_is_refused(tmp_path):
    client = TestClient(_app(tmp_path))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agents/alice/x", headers={"host": _H, "sec-fetch-site": "cross-site"}):
            pass


@pytest.mark.parametrize(
    "origin", [None, "http://127.0.0.1:7870", "tauri://localhost", "http://tauri.localhost", "http://localhost:5173"]
)
def test_console_websockets_still_open(tmp_path, origin):
    client = TestClient(_app(tmp_path))
    headers = {"host": _H, **({"origin": origin} if origin else {})}
    with client.websocket_connect("/agents/alice/x", headers=headers) as ws:
        assert ws.receive_text() == "hi alice"


def test_host_guard_is_the_outermost_middleware(tmp_path):
    app = _app(tmp_path)
    assert app.user_middleware[0].cls is hosts.HostGuardMiddleware
    assert app.user_middleware[1].cls is auth.A2AAuthMiddleware


# ── Gated instance: unchanged ─────────────────────────────────────────────────────────────


async def test_gated_instance_is_unchanged(tmp_path):
    app = _app(tmp_path, bearer="s3cret")
    bearer = {"authorization": "Bearer s3cret"}
    # Any Host, with the credential → served (reverse proxies forward arbitrary names).
    assert (await _req(app, "GET", "/api/config", "evil.example", headers=bearer)).status_code == 200
    # Without it → the credential gate's 401, not the Host gate's 403.
    assert (await _req(app, "GET", "/api/config", "evil.example")).status_code == 401
    # No content-type rule either: the a2a-sdk handles its own body parsing.
    r = await _req(app, "POST", "/a2a", "evil.example", headers={**bearer, "content-type": "text/plain"}, content=b"{}")
    assert r.status_code == 200
    client = TestClient(app)
    with client.websocket_connect(
        "/agents/alice/x", headers={"host": "evil.example", "origin": "https://evil.example"}
    ) as ws:
        assert ws.receive_text() == "hi alice"
    # …and no cross-site rule: the credential gate (401) answers, never the cross-site 403.
    xs = await _req(app, "POST", "/api/restart", _H, headers={"origin": "https://evil.example"})
    assert xs.status_code == 401


async def test_open_mode_is_read_per_request(tmp_path):
    app = _app(tmp_path, bearer="s3cret")
    assert (await _req(app, "GET", "/api/config", "evil.example")).status_code == 401
    auth.set_bearer_token(None)
    assert (await _req(app, "GET", "/api/config", "evil.example")).status_code == 403


def test_refusal_log_is_bounded_under_a_flood_of_distinct_hosts(monkeypatch, caplog):
    """One WARNING per new name up to the cap, one cap notice, then silence — distinct
    Host values must not turn into one log line per request."""
    import logging

    from a2a_impl import hosts

    monkeypatch.setattr(hosts, "_LOGGED_HOSTS", set())
    monkeypatch.setattr(hosts, "_LOGGED_HOSTS_MAX", 3)
    with caplog.at_level(logging.WARNING, logger=hosts.logger.name):
        for i in range(10):
            hosts._log_host_refusal(f"evil{i}.example", "/api/x")
    lines = [r.getMessage() for r in caplog.records]
    assert sum("untrusted Host" in m for m in lines) == 3
    assert sum("log cap reached" in m for m in lines) == 1
