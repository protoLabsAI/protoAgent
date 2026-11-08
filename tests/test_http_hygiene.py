"""HTTP hygiene found by the v0.231.0 stress test (2026-11-07).

* A client that leaves before the response starts is not an ASGI error. ``A2AAuthMiddleware``
  was a Starlette ``BaseHTTPMiddleware``; its ``call_next`` raises
  ``RuntimeError("No response returned.")`` whenever the app ends without starting a response,
  which is what a ``StreamingResponse`` does when it sees the disconnect first. uvicorn logged
  each one as "Exception in ASGI application" with a full traceback (114 on the hub in one run).
  These tests run a REAL uvicorn on a real socket and abort the request, the way the stress
  test did. They also pin the other side: an app that returns no response to a client that is
  still connected is still logged as an error and answered 500.
* The fleet proxy's catch-all no longer produces a "Duplicate Operation ID" warning.
* ``POST``/``PATCH`` ``/api/devices`` answer a body that isn't a JSON object with
  ``400 body must be a JSON object``, not ``name must be a string``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import socket
import threading
import time
import warnings

import pytest

_SECRET = "hygiene-secret"


# ── A real server on a real socket ────────────────────────────────────────────────────────
class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.records.append(record)

    def errors(self) -> list[logging.LogRecord]:
        with self._lock:
            return [r for r in self.records if r.levelno >= logging.ERROR]

    def messages(self) -> list[str]:
        with self._lock:
            return [r.getMessage() for r in self.records]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def auth():
    from a2a_impl import auth as mod

    mod.configure(bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    yield mod
    mod.configure(bearer_token=None, api_key="", allowed_origins_raw="")


@pytest.fixture
def logs():
    """Capture uvicorn's error log and the auth middleware's log, DEBUG and up."""
    handler = _Records()
    loggers = [logging.getLogger("uvicorn.error"), logging.getLogger("a2a_impl.auth")]
    saved = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in zip(loggers, saved):
        lg.removeHandler(handler)
        lg.setLevel(level)


@pytest.fixture
def server(auth):
    """A uvicorn hub behind the real ``A2AAuthMiddleware``, in its own thread and loop.

    Routes:
    * ``/api/silent`` (raw ASGI) waits until the client has gone, then returns WITHOUT sending a
      response: exactly what a ``StreamingResponse`` does when its disconnect listener wins;
    * ``/api/stream`` waits until the client has gone, then returns a real ``StreamingResponse``;
    * ``/api/broken`` returns without a response while the client is still waiting (an app bug);
    * ``/api/whoami`` answers what the middleware stamped on the request.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    done = {"silent": threading.Event(), "stream": threading.Event()}

    async def silent(scope, receive, send):
        try:
            while (await receive())["type"] != "http.disconnect":
                pass
        finally:
            done["silent"].set()

    async def stream(request):
        try:
            while not await request.is_disconnected():
                await asyncio.sleep(0.005)

            async def gen():
                yield b"data: never seen\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        finally:
            done["stream"].set()

    async def broken(scope, receive, send):
        await receive()  # reads the request, then forgets to answer it

    class _Raw:  # a non-function endpoint is mounted as a raw ASGI app, not a request handler
        def __init__(self, fn) -> None:
            self.fn = fn

        async def __call__(self, scope, receive, send) -> None:
            await self.fn(scope, receive, send)

    async def whoami(request):
        return JSONResponse(
            {
                "tier": getattr(request.state, "trust_tier", None),
                "scope": getattr(request.state, "credential_scope", None),
            }
        )

    app = Starlette(
        routes=[
            Route("/api/silent", _Raw(silent)),
            Route("/api/stream", stream),
            Route("/api/broken", _Raw(broken)),
            Route("/api/whoami", whoami),
        ]
    )
    app.add_middleware(auth.A2AAuthMiddleware)
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="off")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert srv.started, "uvicorn did not start"
    yield port, done
    srv.should_exit = True
    thread.join(timeout=5)


def _request(path: str, *, bearer: str | None = _SECRET) -> bytes:
    auth_line = f"Authorization: Bearer {bearer}\r\n" if bearer else ""
    return f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_line}Connection: close\r\n\r\n".encode()


def _send_and_leave(port: int, path: str) -> None:
    """Send a whole request, then abort the socket before any response byte arrives."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(_request(path))


def _send_and_read(port: int, path: str, *, bearer: str | None = _SECRET) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(_request(path, bearer=bearer))
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def _settle(seconds: float = 0.3) -> None:
    """Give the server loop time to finish the request and log whatever it is going to log."""
    time.sleep(seconds)


def test_a_client_that_leaves_before_the_response_logs_no_error(server, logs):
    port, done = server
    _send_and_leave(port, "/api/silent")
    assert done["silent"].wait(5), "the app never saw the disconnect"
    _settle()
    assert [r.getMessage() for r in logs.errors()] == []
    assert not any("No response returned" in m for m in logs.messages())
    # It is still visible at debug level, for anyone chasing a client that keeps leaving.
    assert any("/api/silent ended without a response" in m for m in logs.messages())


def test_streams_abandoned_before_their_headers_log_no_error(server, logs):
    """The stress test's shape: an SSE stream whose client aborts before the headers arrive."""
    port, done = server
    for _ in range(10):
        done["stream"].clear()
        _send_and_leave(port, "/api/stream")
        assert done["stream"].wait(5), "the app never saw the disconnect"
    _settle()
    assert [r.getMessage() for r in logs.errors()] == []


def test_an_app_that_returns_no_response_to_a_waiting_client_is_still_an_error(server, logs):
    """The quiet path is only for a client that has gone: a real app bug stays loud."""
    port, _ = server
    raw = _send_and_read(port, "/api/broken")
    assert raw.startswith(b"HTTP/1.1 500"), raw[:80]
    _settle(0.1)
    errors = [r.getMessage() for r in logs.errors()]
    assert any("without starting response" in m or "No response returned" in m for m in errors), errors


def test_auth_still_refuses_and_stamps_over_a_real_socket(server):
    port, _ = server
    refused = _send_and_read(port, "/api/whoami", bearer=None)
    assert refused.startswith(b"HTTP/1.1 401"), refused[:80]
    wrong = _send_and_read(port, "/api/whoami", bearer="not-the-secret")
    assert wrong.startswith(b"HTTP/1.1 401"), wrong[:80]
    ok = _send_and_read(port, "/api/whoami")
    assert ok.startswith(b"HTTP/1.1 200"), ok[:80]
    assert b'"tier":"operator"' in ok and b'"scope":"full"' in ok


def test_websocket_scopes_pass_straight_through(auth):
    """Non-HTTP scopes skip HTTP auth, as they did under BaseHTTPMiddleware: WebSocket routes
    authenticate themselves (the fleet WS proxy, the terminal)."""
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = auth.A2AAuthMiddleware(inner)
    asyncio.run(mw({"type": "websocket", "path": "/api/x", "headers": []}, None, None))
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["websocket", "lifespan"]


# ── The fleet proxy's catch-all in /openapi.json ──────────────────────────────────────────
def test_the_fleet_proxy_adds_no_duplicate_operation_ids():
    from fastapi import FastAPI

    from operator_api.fleet_routes import register_fleet_routes

    app = FastAPI()
    register_fleet_routes(app)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = app.openapi()
    dupes = [str(w.message) for w in caught if "Duplicate Operation ID" in str(w.message)]
    assert dupes == []
    ids = [op["operationId"] for item in schema["paths"].values() for op in item.values() if "operationId" in op]
    assert len(ids) == len(set(ids))
    # The route itself still answers every method it did (HEAD included, #4258).
    (route,) = [
        r for r in app.routes if getattr(r, "path", "") == "/agents/{slug}/{path:path}" and hasattr(r, "methods")
    ]
    assert {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} <= route.methods


# ── /api/devices with a body that isn't a JSON object ─────────────────────────────────────
@pytest.fixture
def devices_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-http-hygiene")
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as devices

    importlib.reload(devices)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr
    from a2a_impl import auth

    auth.configure(bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    app.add_middleware(auth.A2AAuthMiddleware)
    yield TestClient(app), devices
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    infra.paths.reset_instance_paths()


_BAD_BODIES = [b"notjson", b"", b"[]", b'"phone"', b"null", b"\xff\xfe"]
_HDR = {"Authorization": f"Bearer {_SECRET}", "Content-Type": "application/json"}


@pytest.mark.parametrize("raw", _BAD_BODIES)
def test_minting_a_device_with_a_bad_body_says_so(devices_client, raw):
    client, devices = devices_client
    resp = client.post("/api/devices", headers=_HDR, content=raw)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "body must be a JSON object"
    assert devices.list_devices() == []


@pytest.mark.parametrize("raw", _BAD_BODIES)
def test_updating_a_device_with_a_bad_body_says_so(devices_client, raw):
    client, devices = devices_client
    minted = client.post("/api/devices", headers=_HDR, json={"name": "laptop"})
    assert minted.status_code == 200, minted.text
    device_id = minted.json()["device"]["id"]
    resp = client.patch(f"/api/devices/{device_id}", headers=_HDR, content=raw)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "body must be a JSON object"


def test_field_errors_still_name_the_field(devices_client):
    client, _ = devices_client
    resp = client.post("/api/devices", headers=_HDR, json={})
    assert resp.status_code == 400 and resp.json()["detail"] == "name must be a string"
    resp = client.post("/api/devices", headers=_HDR, json={"name": 5})
    assert resp.status_code == 400 and resp.json()["detail"] == "name must be a string"
