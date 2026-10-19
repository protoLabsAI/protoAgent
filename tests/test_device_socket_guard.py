"""Revocation reaches open WebSockets (ADR 0121 D5, #4336).

``a2a_impl.socket_guard.DeviceSocketGuard`` re-verifies the paired device behind an open
WebSocket on its own timer and closes the socket with 1008 within one interval of a revoke or
an idle expiry. Covered here: a device-token socket, an expired device, full-scope sockets (left
alone), the hub's fleet WS proxy (the member-side socket closes too), and ticket-authenticated
sockets (agent_browser's ``?ticket=`` and the terminal's in-band ticket shape).

The sockets are driven in-process over raw ASGI (an inbox/outbox per socket) so every wait is
bounded by ``asyncio.wait_for``; the fleet member is a real ``websockets`` server.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

# Writes devices.json (chmod + os.replace) through security.devices.
pytestmark = pytest.mark.platform_sensitive

_SECRET = "op-secret"
_FLEET = "fleet-secret"
DAY = 86400.0
TICK = 0.05  # the injected recheck interval


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-device-socket-guard")

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(devices, monkeypatch):
    c = Clock()
    monkeypatch.setattr(devices, "_now", c)
    return c


@pytest.fixture
def ttl(monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(device_idle_ttl_days=30))


@pytest.fixture
def guard(monkeypatch):
    import a2a_impl.socket_guard as mod

    monkeypatch.setattr(mod, "SOCKET_RECHECK_SECONDS", TICK)
    monkeypatch.setattr(mod, "_UNWIND_GRACE_SECONDS", TICK * 4)
    return mod


@pytest.fixture
def auth(devices, guard):
    from a2a_impl import auth as mod

    mod.configure(bearer_token=_SECRET, api_key="", allowed_origins_raw="", fleet_token=_FLEET)
    yield mod


def _phone(devices, name="phone"):
    code, _ = devices.start_pairing()
    return devices.claim_pairing(code, name)


def _install(app, auth):
    auth.install(app, bearer_token=_SECRET, api_key="", allowed_origins_raw="", fleet_token=_FLEET)
    return app


# ── a tiny in-process ASGI websocket client ─────────────────────────────────────────────


class Socket:
    def __init__(self, app, path: str, query: str = "", headers: dict | None = None) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        hdrs = [(b"host", b"testserver")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": hdrs,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self.task = asyncio.ensure_future(app(self.scope, self.inbox.get, self.outbox.put))
        self.inbox.put_nowait({"type": "websocket.connect"})

    async def next(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.outbox.get(), timeout)

    async def accepted(self) -> None:
        msg = await self.next()
        assert msg["type"] == "websocket.accept", msg

    def send_text(self, text: str) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": text})

    async def until_closed(self, timeout: float) -> dict:
        while True:
            msg = await self.next(timeout)
            if msg["type"] == "websocket.close":
                # What a server does next: tell the app the connection is gone.
                self.inbox.put_nowait({"type": "websocket.disconnect", "code": msg.get("code", 1000)})
                return msg

    async def finished(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(asyncio.shield(self.task), timeout)

    async def hang_up(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await self.finished()


def _echo_app(auth):
    app = FastAPI()

    @app.websocket("/ws")
    async def _echo(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass

    return _install(app, auth)


# ── a device-token socket ───────────────────────────────────────────────────────────────


async def test_a_device_socket_closes_1008_within_one_interval_of_a_revoke(auth, devices, clock, ttl):
    device, token = _phone(devices)
    sock = Socket(_echo_app(auth), "/ws", f"token={token}")
    await sock.accepted()
    sock.send_text("hi")
    assert (await sock.next())["text"] == "hi"  # a live, working socket

    loop = asyncio.get_running_loop()
    devices.revoke_device(device["id"])
    t0 = loop.time()
    msg = await sock.until_closed(timeout=TICK * 10)
    assert msg["code"] == 1008
    assert loop.time() - t0 < TICK * 2.5  # one interval, plus scheduling slack
    await sock.finished()  # the handler unwound


async def test_a_bearer_header_device_socket_closes_on_revoke(auth, devices, clock, ttl):
    device, token = _phone(devices)
    sock = Socket(_echo_app(auth), "/ws", headers={"authorization": f"Bearer {token}"})
    await sock.accepted()
    devices.revoke_device(device["id"])
    assert (await sock.until_closed(timeout=TICK * 10))["code"] == 1008
    await sock.finished()


async def test_a_device_socket_closes_within_one_interval_of_expiry(auth, devices, clock, ttl):
    device, token = _phone(devices)
    sock = Socket(_echo_app(auth), "/ws", f"token={token}")
    await sock.accepted()
    await asyncio.sleep(TICK * 3)
    assert sock.outbox.empty()  # still live: nothing closed it

    loop = asyncio.get_running_loop()
    clock.advance(31 * DAY)  # past the 30-day idle TTL, with no request in between
    t0 = loop.time()
    assert (await sock.until_closed(timeout=TICK * 10))["code"] == 1008
    assert loop.time() - t0 < TICK * 2.5
    await sock.finished()


async def test_a_handler_that_never_reads_is_still_unwound(auth, devices, clock, ttl):
    """A handler stuck somewhere the close can't reach is cancelled after a short grace."""
    app = FastAPI()

    @app.websocket("/stuck")
    async def _stuck(ws: WebSocket):
        await ws.accept()
        await asyncio.Event().wait()

    device, token = _phone(devices)
    sock = Socket(_install(app, auth), "/stuck", f"token={token}")
    await sock.accepted()
    devices.revoke_device(device["id"])
    msg = await sock.next(timeout=TICK * 10)
    assert msg["type"] == "websocket.close" and msg["code"] == 1008
    await sock.finished(timeout=TICK * 20)  # no disconnect fed in: cancelled by the guard


# ── full-scope sockets are never closed ─────────────────────────────────────────────────


@pytest.mark.parametrize("cred", ["query-bearer", "header-bearer", "fleet", "none"])
async def test_a_full_scope_socket_stays_open(auth, devices, clock, ttl, cred):
    device, _token = _phone(devices)
    query, headers = {
        "query-bearer": (f"token={_SECRET}", None),
        "header-bearer": ("", {"authorization": f"Bearer {_SECRET}"}),
        "fleet": (f"token={_FLEET}", None),
        "none": ("", None),
    }[cred]
    sock = Socket(_echo_app(auth), "/ws", query, headers)
    await sock.accepted()
    devices.revoke_device(device["id"])  # some OTHER device going away changes nothing
    clock.advance(31 * DAY)
    await asyncio.sleep(TICK * 8)
    sock.send_text("still here")
    assert (await sock.next())["text"] == "still here"
    await sock.hang_up()


async def test_a_device_socket_stays_open_while_the_device_is_live(auth, devices, clock, ttl):
    _device, token = _phone(devices)
    sock = Socket(_echo_app(auth), "/ws", f"token={token}")
    await sock.accepted()
    await asyncio.sleep(TICK * 8)
    sock.send_text("x")
    assert (await sock.next())["text"] == "x"
    await sock.hang_up()


def test_presented_device_id_names_only_device_credentials(auth, devices, clock, ttl):
    device, token = _phone(devices)
    assert auth.presented_device_id(token) == device["id"]
    sse = auth.generate_sse_token(scope="device", device_id=device["id"])
    assert auth.presented_device_id(sse) == device["id"]
    for full in (_SECRET, _FLEET, auth.generate_sse_token(scope="full"), "garbage", ""):
        assert auth.presented_device_id(full) is None


# ── the hub's fleet WS proxy ────────────────────────────────────────────────────────────


def _member_ws_server():
    """A real echo WebSocket "member" in a background thread. Records the query each
    connection arrived with and sets ``closed`` when the hub hangs up on it."""
    import websockets

    holder: dict = {"queries": []}
    ready, closed = threading.Event(), threading.Event()
    loop = asyncio.new_event_loop()

    async def _echo(conn):
        holder["queries"].append(conn.request.path)
        try:
            async for msg in conn:
                await conn.send(msg)
        finally:
            closed.set()

    async def _main():
        server = await websockets.serve(_echo, "127.0.0.1", 0)
        holder["port"] = server.sockets[0].getsockname()[1]
        ready.set()
        await asyncio.Future()

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except (asyncio.CancelledError, RuntimeError):
            pass

    threading.Thread(target=_run, daemon=True).start()
    assert ready.wait(5), "member ws server didn't start"
    return holder, closed, (lambda: loop.call_soon_threadsafe(loop.stop))


def _hub_app(auth):
    from graph.fleet import proxy

    app = FastAPI()

    @app.websocket("/agents/{slug}/{path:path}")
    async def _p(ws: WebSocket, slug: str, path: str):
        await proxy.forward_ws(slug, ws, path)

    return _install(app, auth)


async def test_the_fleet_proxy_closes_both_ends_on_a_hub_device_revoke(auth, devices, clock, ttl, monkeypatch):
    from graph.fleet import proxy

    holder, member_closed, stop = _member_ws_server()
    try:
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", f"http://127.0.0.1:{holder['port']}", None))
        monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _FLEET)
        device, token = _phone(devices)
        sock = Socket(_hub_app(auth), "/agents/peer/plugins/terminal/ws", f"token={token}")
        await sock.accepted()
        sock.send_text("ping")
        assert (await sock.next())["text"] == "ping"  # relayed through the hub
        assert f"token={_FLEET}" in holder["queries"][0]  # the member got the fleet token

        devices.revoke_device(device["id"])
        assert (await sock.until_closed(timeout=TICK * 10))["code"] == 1008  # hub side
        await sock.finished()
        assert await asyncio.to_thread(member_closed.wait, 5)  # and the member side with it
    finally:
        stop()


async def test_the_fleet_proxy_keeps_a_full_scope_socket(auth, devices, clock, ttl, monkeypatch):
    from graph.fleet import proxy

    holder, member_closed, stop = _member_ws_server()
    try:
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", f"http://127.0.0.1:{holder['port']}", None))
        monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _FLEET)
        device, _token = _phone(devices)
        sock = Socket(_hub_app(auth), "/agents/peer/live", f"token={_SECRET}")
        await sock.accepted()
        devices.revoke_device(device["id"])
        await asyncio.sleep(TICK * 6)
        sock.send_text("ping")
        assert (await sock.next())["text"] == "ping"
        assert not member_closed.is_set()
        await sock.hang_up()
    finally:
        stop()


# ── ticket-authenticated sockets ────────────────────────────────────────────────────────


async def _mint(app, path: str, token: str) -> str:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post(path, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _in_band_ticket_app(auth):
    """The terminal plugin's shape: a gated HTTP mint records the minting device; the socket
    accepts first, then redeems the ticket from its first frame and binds the device."""
    import secrets as _secrets

    from a2a_impl.socket_guard import bind_device, request_device_id

    app = FastAPI()
    tickets: dict[str, str | None] = {}

    @app.post("/api/plugins/term/ticket")
    async def _mint_route(request: Request):
        t = _secrets.token_urlsafe(16)
        tickets[t] = request_device_id(request)
        return {"ticket": t}

    @app.websocket("/plugins/term/ws")
    async def _ws(ws: WebSocket):
        await ws.accept()
        first = await ws.receive_json()
        if first.get("ticket") not in tickets:
            await ws.close(code=4001)
            return
        bind_device(ws, tickets.pop(first["ticket"]))
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass

    return _install(app, auth)


async def test_an_in_band_ticket_socket_minted_by_a_device_closes_on_revoke(auth, devices, clock, ttl):
    import json

    app = _in_band_ticket_app(auth)
    device, token = _phone(devices)
    ticket = await _mint(app, "/api/plugins/term/ticket", token)
    sock = Socket(app, "/plugins/term/ws")  # no credential on the handshake at all
    await sock.accepted()
    sock.send_text(json.dumps({"ticket": ticket}))
    sock.send_text("ls")
    assert (await sock.next())["text"] == "ls"

    devices.revoke_device(device["id"])
    assert (await sock.until_closed(timeout=TICK * 10))["code"] == 1008
    await sock.finished()


async def test_an_in_band_ticket_minted_with_the_shared_bearer_stays_open(auth, devices, clock, ttl):
    import json

    app = _in_band_ticket_app(auth)
    device, _token = _phone(devices)
    ticket = await _mint(app, "/api/plugins/term/ticket", _SECRET)
    sock = Socket(app, "/plugins/term/ws")
    await sock.accepted()
    sock.send_text(json.dumps({"ticket": ticket}))
    devices.revoke_device(device["id"])
    await asyncio.sleep(TICK * 6)
    sock.send_text("ls")
    assert (await sock.next())["text"] == "ls"
    await sock.hang_up()


def _agent_browser():
    from graph.plugins.testkit import load_plugin

    pkg = load_plugin(Path(__file__).resolve().parents[1] / "plugins" / "agent_browser", "agent_browser")
    return importlib.import_module(f"{pkg.__name__}.browser_panel")


class _FakeCDP:
    """Stands in for ``browser_stream.CDPStream``: a page that never sends a frame."""

    def __init__(self, *_a, **_k) -> None:
        self.reader_task = asyncio.ensure_future(asyncio.Event().wait())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.reader_task.cancel()

    async def start_screencast(self) -> None:
        return None

    async def dispatch(self, _msg) -> None:
        return None


async def test_agent_browsers_ticket_socket_closes_when_its_minting_phone_is_revoked(
    auth, devices, clock, ttl, monkeypatch
):
    bp = _agent_browser()
    monkeypatch.setattr(bp.browser_stream, "resolve_page_target", lambda binary, timeout: ("ws://page", ""))
    monkeypatch.setattr(bp.browser_stream, "CDPStream", _FakeCDP)
    app = FastAPI()
    app.include_router(bp.build_panel_data_router({}), prefix="/api/plugins/agent_browser")
    _install(app, auth)

    device, token = _phone(devices)
    ticket = await _mint(app, "/api/plugins/agent_browser/stream-ticket", token)
    sock = Socket(app, "/api/plugins/agent_browser/stream", f"ticket={ticket}")
    await sock.accepted()
    await asyncio.sleep(TICK * 3)
    assert sock.outbox.empty()  # open and quiet

    devices.revoke_device(device["id"])
    assert (await sock.until_closed(timeout=TICK * 10))["code"] == 1008
    await sock.finished()


def test_agent_browser_tickets_carry_the_minting_device():
    bs = importlib.import_module(_agent_browser().__name__.rsplit(".", 1)[0] + ".browser_stream")
    t = bs.mint_ticket("dev-123")
    assert bs.redeem_ticket(t) == (True, "dev-123")
    assert bs.redeem_ticket(t) == (False, None)  # still single-use
    assert bs.consume_ticket(bs.mint_ticket()) is True  # the full-scope mint is unchanged
