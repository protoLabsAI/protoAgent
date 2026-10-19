"""Hub ticket trust boundary: authenticated HTTP state is the sole device authority.

A member sees only its fleet/stored credential, mints an ordinary ticket, and never
reads the hub registry. The hub substitutes a bounded single-use capability tied to
(slug, target URL, exact WS route, original ticket, device). Neither a browser header
nor possession of another member's handle can select a device. No shared signing key
or plugin protocol change is required. The member still validates its original ticket.
"""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import websockets
from fastapi import FastAPI, Request, WebSocket

from graph.fleet import proxy
from tests import test_device_socket_guard as support
from tests.test_device_socket_guard import (
    DAY, TICK, Socket, _FLEET, _SECRET, _install, _phone,
)

auth = support.auth
clock = support.clock
devices = support.devices
guard = support.guard
ttl = support.ttl

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture(autouse=True)
def bindings(monkeypatch):
    monkeypatch.setattr(proxy, "_ticket_bindings", {})
    monkeypatch.setattr(proxy, "_slug_cache", {})
    monkeypatch.setattr(proxy, "_remote_slugs", set())


def hub(auth):
    app = FastAPI()

    @app.post("/agents/{slug}/{path:path}")
    async def mint(request: Request, slug: str, path: str):
        return await proxy.forward_to(slug, request, path)

    @app.websocket("/agents/{slug}/{path:path}")
    async def socket(ws: WebSocket, slug: str, path: str):
        await proxy.forward_ws(slug, ws, path)

    return _install(app, auth)


@pytest.mark.parametrize("kind", ["local", "remote"])
@pytest.mark.parametrize("protocol", ["terminal", "browser"])
@pytest.mark.parametrize("ending", ["revoke", "expiry", "operator"])
async def test_mint_redeem_and_close_both_legs(auth, devices, clock, ttl, monkeypatch, caplog, kind, protocol, ending):
    device, phone = _phone(devices)
    member_closed = asyncio.Event()
    member_tickets = set()
    observed = []
    route = "plugins/terminal/ws" if protocol == "terminal" else "api/plugins/agent_browser/stream"
    mint_route = "api/plugins/terminal/ticket" if protocol == "terminal" else "api/plugins/agent_browser/stream-ticket"

    async def member(conn):
        observed.append((conn.request.path, dict(conn.request.headers)))
        try:
            if protocol == "terminal":
                hello = json.loads(await conn.recv())
                ticket = hello["ticket"]
                assert hello["cols"] == 80
            else:
                ticket = parse_qs(urlsplit(conn.request.path).query)["ticket"][0]
            assert ticket in member_tickets
            member_tickets.remove(ticket)
            await conn.send("authenticated")
            async for msg in conn:
                await conn.send(msg)
        finally:
            member_closed.set()

    async with websockets.serve(member, "127.0.0.1", 0) as server:
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        stored = "remote-secret" if kind == "remote" else None
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: (kind, base, stored))
        monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _FLEET)

        def mint_member(request):
            assert request.headers["authorization"] == f"Bearer {stored or _FLEET}"
            assert phone not in str(request.url) + repr(dict(request.headers))
            assert str(request.url).endswith(mint_route)
            member_tickets.add("original-member-ticket")
            return httpx.Response(200, stream=httpx.ByteStream(
                json.dumps({"ticket": "original-member-ticket", "ttl": 30}).encode()
            ), headers={"content-type": "application/json"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(mint_member)) as upstream:
            monkeypatch.setattr(proxy, "_get_client", lambda: upstream)
            app = hub(auth)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
                response = await client.post(
                    f"/agents/peer/{mint_route}",
                    headers={"authorization": f"Bearer {_SECRET if ending == 'operator' else phone}",
                             "x-device-id": "forged-device"},
                )
            assert response.status_code == 200
            ticket = response.json()["ticket"]
            assert response.json()["ttl"] == 30
            if ending != "operator":
                assert ticket.startswith(proxy._TICKET_PREFIX)
                assert "original-member-ticket" not in response.text
                assert response.headers["cache-control"] == "no-store"
            sock = Socket(app, f"/agents/peer/{route}", f"ticket={ticket}" if protocol == "browser" else "")
            await sock.accepted()
            if protocol == "terminal":
                sock.send_text(json.dumps({"type": "auth", "ticket": ticket, "cols": 80}))
            assert (await sock.next())["text"] == "authenticated"
            assert sock.scope["state"].get("device_id") == (None if ending == "operator" else device["id"])
            assert phone not in repr(observed)
            assert proxy._TICKET_PREFIX not in repr(observed)
            assert _FLEET not in repr(observed) and "remote-secret" not in repr(observed)
            start = asyncio.get_running_loop().time()
            if ending == "expiry":
                clock.advance(31 * DAY)
            else:
                devices.revoke_device(device["id"])
            if ending == "operator":
                await asyncio.sleep(TICK * 3)
                sock.send_text("still open")
                assert (await sock.next())["text"] == "still open"
                assert not member_closed.is_set()
                await sock.hang_up()
            else:
                closed = await sock.next(TICK * 10)
                assert closed["type"] == "websocket.close" and closed["code"] == 1008
                await asyncio.wait_for(member_closed.wait(), TICK * 10)
                assert asyncio.get_running_loop().time() - start < TICK * 3
                await sock.finished()
            assert ticket not in caplog.text and phone not in caplog.text


@pytest.mark.parametrize("attack", ["forged", "tampered", "expired", "replay", "member", "target", "path", "revoked"])
async def test_handle_refused_before_dial(auth, devices, clock, ttl, monkeypatch, attack):
    device, _ = _phone(devices)
    base = "http://127.0.0.1:1"
    path = "api/plugins/agent_browser/stream"
    ticket = proxy._remember_ticket("peer", base, path, "member-ticket", device["id"])
    slug = "peer"
    if attack == "forged":
        ticket = proxy._TICKET_PREFIX + "made-up"
    elif attack == "tampered":
        ticket += "x"
    elif attack == "expired":
        record = proxy._ticket_bindings[ticket]
        proxy._ticket_bindings[ticket] = (0, *record[1:])
    elif attack == "replay":
        assert proxy._redeem_proxy_ticket(ticket, slug, base, path, {}) == "member-ticket"
    elif attack == "member":
        slug = "other"
    elif attack == "target":
        base = "http://127.0.0.1:2"
    elif attack == "path":
        path = "plugins/terminal/ws"
    elif attack == "revoked":
        devices.revoke_device(device["id"])
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", base, None))

    async def no_dial(*args, **kwargs):
        pytest.fail("invalid handle must never dial the member")

    monkeypatch.setattr(websockets, "connect", no_dial)
    sock = Socket(hub(auth), f"/agents/{slug}/{path}", f"ticket={ticket}", headers={"x-device-id": device["id"]})
    assert (await sock.until_closed(1))["code"] == 1008
    await sock.finished()


def test_capacity_and_unknown_handles_fail_closed(monkeypatch):
    monkeypatch.setattr(proxy, "_TICKET_LIMIT", 2)
    first = proxy._remember_ticket("peer", "base", "route", "original", "device")
    proxy._remember_ticket("peer", "base", "route", "original", "device")
    proxy._remember_ticket("peer", "base", "route", "original", "device")
    assert len(proxy._ticket_bindings) == 2
    with pytest.raises(ValueError):
        proxy._redeem_proxy_ticket(first, "peer", "base", "route", {})


@pytest.mark.parametrize("attack", ["forged", "expired", "replay", "member", "path", "conflicting_device"])
async def test_in_band_handle_is_not_relayed_on_refusal(auth, devices, clock, ttl, monkeypatch, attack):
    device, _ = _phone(devices)
    base, path = "http://member", "plugins/terminal/ws"
    ticket = proxy._remember_ticket("peer", base, path, "member-ticket", device["id"])
    scope = {"state": {}}
    slug = "peer"
    if attack == "forged":
        ticket += "x"
    elif attack == "expired":
        record = proxy._ticket_bindings[ticket]
        proxy._ticket_bindings[ticket] = (0, *record[1:])
    elif attack == "replay":
        proxy._redeem_proxy_ticket(ticket, slug, base, path, {})
    elif attack == "member":
        slug = "other"
    elif attack == "path":
        path = "other/route"
    elif attack == "conflicting_device":
        scope["state"]["device_id"] = "another-authenticated-device"

    class Client:
        def __init__(self):
            self.scope = scope
            self.code = None

        async def receive(self):
            return {"type": "websocket.receive", "text": json.dumps({"type": "auth", "ticket": ticket})}

        async def close(self, code, reason):
            self.code = code

    class Member:
        async def send(self, data):
            pytest.fail("invalid in-band handle must not reach member")

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    client = Client()
    await asyncio.wait_for(proxy._pump_ws(client, Member(), ticket_target=(slug, base, path)), 1)
    assert client.code == 1008


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"ticket": 5}', b'{"ticket":""}', b"x" * 16385])
async def test_malformed_mint_never_exposes_unbound_response(auth, devices, clock, ttl, monkeypatch, payload):
    _, phone = _phone(devices)
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", "http://member", None))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _FLEET)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload))) as member:
        monkeypatch.setattr(proxy, "_get_client", lambda: member)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(hub(auth)), base_url="http://testserver") as client:
            result = await client.post("/agents/peer/api/plugins/terminal/ticket", headers={"authorization": f"Bearer {phone}"})
        assert result.status_code == 502
        assert result.json() == {"detail": "invalid member ticket response"}
        assert not proxy._ticket_bindings


@pytest.mark.parametrize("spelling", ["api/plugins/terminal/%74icket", "api/plugins/terminal/./ticket"])
async def test_alternate_mint_spelling_cannot_bypass_binding(auth, devices, clock, ttl, monkeypatch, spelling):
    from starlette.requests import Request as StarletteRequest

    device, _ = _phone(devices)
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://member", {}))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: _FLEET)
    path = "/agents/peer/" + spelling
    # Raw ASGI preserves literal dots (httpx's *client* would normalize before the hub).
    request = StarletteRequest({
        "type": "http", "method": "POST", "path": path.replace("%74", "t"),
        "raw_path": path.encode(), "query_string": b"", "headers": [],
        "state": {"trust_tier": "operator", "device_id": device["id"], "credential_scope": "device"},
    })
    request._body = b""
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ticket": "original"})
    )) as member:
        monkeypatch.setattr(proxy, "_get_client", lambda: member)
        response = await proxy.forward_to("peer", request, spelling.replace("%74", "t"))
    assert response.status_code == 200
    assert json.loads(response.body)["ticket"].startswith(proxy._TICKET_PREFIX)


@pytest.mark.parametrize("frame", ["raw text", '{"ticket":"ordinary-member-ticket"}', '[' * 1100 + '"hub-ticket.x"' + ']' * 1100])
async def test_non_handle_frames_remain_opaque(frame):
    class Client:
        def __init__(self):
            self.sent = False

        async def receive(self):
            if self.sent:
                return {"type": "websocket.disconnect"}
            self.sent = True
            return {"type": "websocket.receive", "text": frame}

    class Member:
        def __init__(self):
            self.frames = []

        async def send(self, data):
            self.frames.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    member = Member()
    await proxy._pump_ws(Client(), member, ticket_target=("peer", "http://member", "any/route"))
    assert member.frames == [frame]
