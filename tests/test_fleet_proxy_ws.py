"""Fleet-proxy WebSocket relay (#883) — `proxy.forward_ws` proxies a WS upgrade through
the hub to the focused member, so a plugin's live socket (agent_browser's viewport/feed)
traverses the hub instead of showing "Disconnected" behind the HTTP-only proxy."""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from graph.fleet import proxy


def _echo_ws_server():
    """A real echo WebSocket server on a free port, in a background thread.
    Returns (port, stop) — proves forward_ws relays frames BOTH ways over real sockets."""
    import websockets

    holder: dict = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    async def _echo(conn):
        async for msg in conn:
            await conn.send(msg)  # echo text or binary

    async def _main():
        server = await websockets.serve(_echo, "127.0.0.1", 0)
        holder["port"] = server.sockets[0].getsockname()[1]
        ready.set()
        await asyncio.Future()  # serve forever

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except (asyncio.CancelledError, RuntimeError):
            pass

    threading.Thread(target=_run, daemon=True).start()
    assert ready.wait(5), "echo ws server didn't start"
    return holder["port"], (lambda: loop.call_soon_threadsafe(loop.stop))


def _ws_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/agents/{slug}/{path:path}")
    async def _p(ws: WebSocket, slug: str, path: str):
        await proxy.forward_ws(slug, ws, path)

    return app


def test_ws_proxy_relays_text_and_binary(monkeypatch):
    port, stop = _echo_ws_server()
    try:
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", f"http://127.0.0.1:{port}", None))
        with TestClient(_ws_app()).websocket_connect("/agents/peer/live") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "ping"  # text round-trips through the hub
            ws.send_bytes(b"\x00\x01\x02")
            assert ws.receive_bytes() == b"\x00\x01\x02"  # binary round-trips too
    finally:
        stop()


def test_ws_proxy_rejects_when_agent_not_running(monkeypatch):
    # No live target → the proxy closes the handshake (the WS analog of the HTTP 409).
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: None)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_ws_app()).websocket_connect("/agents/ghost/live"):
            pass


def test_ws_proxy_still_serves_live_local_peer(monkeypatch):
    """A running LOCAL peer (a live pid in fleet state) shadows a same-slug remote — resolved
    for real through ``_resolve_slug``, so the remote rules can't catch a local member."""
    port, stop = _echo_ws_server()
    try:
        from graph.fleet import supervisor

        monkeypatch.setattr(supervisor, "_load_state", lambda: {"peer": {"pid": 4242, "port": port}})
        monkeypatch.setattr(supervisor, "_alive", lambda pid: True)
        monkeypatch.setattr(
            supervisor, "remote_for_slug", lambda slug: {"id": slug, "url": "http://100.64.0.9:7870", "token": "sek"}
        )
        with TestClient(_ws_app()).websocket_connect("/agents/peer/live") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "ping"
    finally:
        stop()


# ── _member_ws_query: token auth + fleet swap (ADR 0089) ──────────────────────


def _patch_bt(monkeypatch, fn):
    monkeypatch.setattr("a2a_impl.auth.bearer_tier", fn)


def _patch_fleet(monkeypatch, tok="fleet-tok"):
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: tok)


def test_member_ws_query_swaps_operator_token_for_fleet(monkeypatch):
    from urllib.parse import parse_qs

    _patch_bt(monkeypatch, lambda t: "operator" if t == "op" else None)
    _patch_fleet(monkeypatch)
    q, allowed = proxy._member_ws_query("alice", "token=op&cols=80")
    assert allowed
    d = parse_qs(q)
    assert d["token"] == ["fleet-tok"] and d["cols"] == ["80"]  # swapped; other params kept


def test_member_ws_query_refuses_unauthenticated_token(monkeypatch):
    _patch_bt(monkeypatch, lambda t: None)  # nothing authenticates → close, don't lend the socket
    _, allowed = proxy._member_ws_query("alice", "token=nope")
    assert not allowed


def test_member_ws_query_passes_through_ticket_plugin(monkeypatch):
    # No token param (agent_browser mints a member-side ?ticket=) — forward as-is; the member
    # self-authenticates and the hub must neither gate nor inject.
    _patch_bt(monkeypatch, lambda t: None)  # closed hub, empty token → not operator
    q, allowed = proxy._member_ws_query("alice", "ticket=abc123")
    assert allowed and q == "ticket=abc123"


def test_member_ws_query_open_mode_injects_fleet(monkeypatch):
    from urllib.parse import parse_qs

    # Open hub: bearer_tier('') → operator, so even a tokenless handshake gets the fleet token a
    # closed member still needs; harmless for ticket plugins (they ignore ?token=).
    _patch_bt(monkeypatch, lambda t: "operator")
    _patch_fleet(monkeypatch)
    q, allowed = proxy._member_ws_query("alice", "ticket=abc")
    d = parse_qs(q)
    assert allowed and d["token"] == ["fleet-tok"] and d["ticket"] == ["abc"]


def test_member_ws_query_host_passes_through(monkeypatch):
    # The host's plugins expect the operator bearer, not the fleet token — never swap for host.
    _patch_bt(monkeypatch, lambda t: "operator")
    _patch_fleet(monkeypatch)
    q, allowed = proxy._member_ws_query("host", "token=op")
    assert allowed and q == "token=op"


# ── Remote members over WS (ADR 0113 D6) ──────────────────────────────────────
#
# The one rule: the hub never attaches a remote's stored bearer to an upgrade ON ITS OWN. These
# tests dial a real recording server as the "remote" and assert on exactly what it received —
# the query string, the Authorization header (or its absence) and the subprotocols — because a
# mocked wire is how an inert fix ships. Every refusal also proves nothing was dialled.

_STORED = "remote-stored-bearer"
_HUB_OP = "hub-operator-bearer"


def _recording_ws_server(subprotocols=None):
    """A real WS server that, on connect, sends back what its handshake carried as JSON:
    ``{path, authorization, subprotocol, headers}``. Returns (port, stop)."""
    import json

    import websockets

    holder: dict = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    async def _handler(conn):
        req = conn.request
        await conn.send(
            json.dumps(
                {
                    "path": req.path,
                    "authorization": req.headers.get("authorization"),
                    "subprotocol": conn.subprotocol,
                    "offered": req.headers.get("sec-websocket-protocol"),
                    "headers": {k.lower(): v for k, v in req.headers.raw_items()},
                }
            )
        )
        async for msg in conn:
            await conn.send(msg)

    async def _main():
        server = await websockets.serve(_handler, "127.0.0.1", 0, subprotocols=subprotocols)
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
    assert ready.wait(5), "recording ws server didn't start"
    return holder["port"], (lambda: loop.call_soon_threadsafe(loop.stop))


def _closed_hub(monkeypatch):
    """A token-gated hub: only ``_HUB_OP`` authenticates, as operator."""
    _patch_bt(monkeypatch, lambda t: "operator" if t == _HUB_OP else None)
    _patch_fleet(monkeypatch, "fleet-tok-must-not-leave-this-machine")


def _remote(monkeypatch, port: int | None, token: str | None = _STORED):
    url = f"http://127.0.0.1:{port}" if port else "http://100.64.0.9:7870"
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("remote", url, token))


def _no_dial(monkeypatch):
    """A refusal must happen BEFORE any upstream is dialled — make a dial loud."""
    import websockets

    async def _boom(*a, **k):
        pytest.fail("a refused remote WS must not dial the upstream")

    monkeypatch.setattr(websockets, "connect", _boom)


def _seen(ws) -> dict:
    import json

    return json.loads(ws.receive_text())


def _query(seen: dict) -> dict:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(seen["path"]).query, keep_blank_values=True)


def _refused(url: str, **kw) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(_ws_app()).websocket_connect(url, **kw):
            pass
    return exc.value.code


def test_remote_without_stored_token_is_refused(monkeypatch):
    """Nothing to authenticate against ⇒ the hub would be a blind pipe into an open instance.
    Refused even for an operator caller, and nothing is dialled."""
    _closed_hub(monkeypatch)
    _remote(monkeypatch, None, token=None)
    _no_dial(monkeypatch)
    assert _refused(f"/agents/ava/live?token={_HUB_OP}") == 1008
    assert _refused("/agents/ava/live?ticket=t1") == 1008


def test_remote_operator_query_token_is_swapped_for_stored_token(monkeypatch):
    port, stop = _recording_ws_server()
    try:
        _closed_hub(monkeypatch)
        _remote(monkeypatch, port)
        with TestClient(_ws_app()).websocket_connect(f"/agents/ava/pty?token={_HUB_OP}&cols=80") as ws:
            seen = _seen(ws)
        q = _query(seen)
        assert q["token"] == [_STORED]  # swapped into the slot it was presented in
        assert q["cols"] == ["80"]  # other params kept
        assert seen["authorization"] is None  # the hub added NO Authorization header
        wire = seen["path"] + repr(seen["headers"])
        assert _HUB_OP not in wire  # the hub's credential never reaches the remote
        assert "fleet-tok" not in wire  # nor the loopback-only fleet token
    finally:
        stop()


@pytest.mark.parametrize("presented", ["garbage", "", "fed-token"])
def test_remote_non_operator_query_token_is_refused(monkeypatch, presented):
    # garbage, an EMPTY token=, and a real-but-lesser tier (federation) are all refused.
    _patch_bt(monkeypatch, lambda t: "federation" if t == "fed-token" else ("operator" if t == _HUB_OP else None))
    _remote(monkeypatch, None)
    _no_dial(monkeypatch)
    assert _refused(f"/agents/ava/pty?token={presented}") == 1008


def test_remote_ticket_without_token_passes_through_with_no_authorization(monkeypatch):
    """Ticket-based plugins (agent_browser): the ticket was minted over the authenticated HTTP
    proxy and the remote checks it. The hub adds NOTHING — not the stored bearer in a header,
    not a ?token= param."""
    port, stop = _recording_ws_server()
    try:
        _closed_hub(monkeypatch)
        _remote(monkeypatch, port)
        with TestClient(_ws_app()).websocket_connect("/agents/ava/api/plugins/agent_browser/stream?ticket=t1") as ws:
            seen = _seen(ws)
        assert _query(seen) == {"ticket": ["t1"]}
        assert seen["authorization"] is None
        assert _STORED not in seen["path"] + repr(seen["headers"])
    finally:
        stop()


def test_remote_ticket_on_open_hub_still_gets_no_credential(monkeypatch):
    """An OPEN hub makes every presented token operator, but it never presents one on the
    caller's behalf — unlike the local path, which injects the fleet token in open mode."""
    port, stop = _recording_ws_server()
    try:
        _patch_bt(monkeypatch, lambda t: "operator")  # open hub
        _patch_fleet(monkeypatch)
        _remote(monkeypatch, port)
        with TestClient(_ws_app()).websocket_connect("/agents/ava/stream?ticket=t1") as ws:
            seen = _seen(ws)
        assert _query(seen) == {"ticket": ["t1"]}
        assert seen["authorization"] is None
    finally:
        stop()


def test_remote_operator_authorization_header_is_replaced_by_stored_bearer(monkeypatch):
    """Server-to-server caller with the HUB's bearer in a header: authenticated at the hub, then
    replaced by the remote's stored bearer — the hub credential is never forwarded."""
    port, stop = _recording_ws_server()
    try:
        _closed_hub(monkeypatch)
        _remote(monkeypatch, port)
        hdr = {"authorization": f"Bearer {_HUB_OP}"}
        with TestClient(_ws_app()).websocket_connect("/agents/ava/stream?ticket=t1", headers=hdr) as ws:
            seen = _seen(ws)
        assert seen["authorization"] == f"Bearer {_STORED}"
        assert _query(seen) == {"ticket": ["t1"]}  # no ?token= invented for a header caller
        assert _HUB_OP not in seen["path"] + repr(seen["headers"])
    finally:
        stop()


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer ", f"Basic {_HUB_OP}", "garbage"])
def test_remote_bad_authorization_header_is_refused_not_stripped(monkeypatch, header):
    # A presented credential that fails is a policy violation, not a silent fallback to anonymous.
    _closed_hub(monkeypatch)
    _remote(monkeypatch, None)
    _no_dial(monkeypatch)
    assert _refused("/agents/ava/stream?ticket=t1", headers={"authorization": header}) == 1008


def test_remote_good_token_does_not_excuse_bad_header(monkeypatch):
    _closed_hub(monkeypatch)
    _remote(monkeypatch, None)
    _no_dial(monkeypatch)
    code = _refused(f"/agents/ava/pty?token={_HUB_OP}", headers={"authorization": "Bearer garbage"})
    assert code == 1008


def test_remote_subprotocols_pass_through_and_never_carry_the_stored_token(monkeypatch):
    port, stop = _recording_ws_server(subprotocols=["binary", "json"])
    try:
        _closed_hub(monkeypatch)
        _remote(monkeypatch, port)
        with TestClient(_ws_app()).websocket_connect("/agents/ava/stream?ticket=t1", subprotocols=["json"]) as ws:
            seen = _seen(ws)
            assert ws.accepted_subprotocol == "json"  # the remote's pick reaches the client
        assert seen["offered"] == "json"  # exactly the caller's offer, nothing added
        assert _STORED not in repr(seen["headers"])
    finally:
        stop()


def test_local_member_regression_fleet_swap_and_caller_header(monkeypatch):
    """Local members are unchanged: operator ?token= → the fleet token; the caller's own
    Authorization header rides through (the hub attaches no stored credential)."""
    port, stop = _recording_ws_server()
    try:
        _closed_hub(monkeypatch)
        _patch_fleet(monkeypatch, "fleet-tok")
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", f"http://127.0.0.1:{port}", None))
        hdr = {"authorization": "Bearer caller-own"}
        with TestClient(_ws_app()).websocket_connect(f"/agents/alice/pty?token={_HUB_OP}", headers=hdr) as ws:
            seen = _seen(ws)
        assert _query(seen)["token"] == ["fleet-tok"]
        assert seen["authorization"] == "Bearer caller-own"
    finally:
        stop()
    # …and a non-operator token is still refused for a local member.
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", "http://127.0.0.1:1", None))
    _no_dial(monkeypatch)
    assert _refused("/agents/alice/pty?token=garbage") == 1008


def test_host_regression_passes_token_and_header_through(monkeypatch):
    port, stop = _recording_ws_server()
    try:
        _closed_hub(monkeypatch)
        monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("host", f"http://127.0.0.1:{port}", None))
        hdr = {"authorization": "Bearer caller-own"}
        with TestClient(_ws_app()).websocket_connect(f"/agents/host/pty?token={_HUB_OP}", headers=hdr) as ws:
            seen = _seen(ws)
        assert _query(seen)["token"] == [_HUB_OP]  # host plugins expect the operator bearer
        assert seen["authorization"] == "Bearer caller-own"
    finally:
        stop()


def test_resolve_slug_precedence(monkeypatch):
    """host → live local → remote; a remote's token comes from its own record."""
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "_load_state", lambda: {"alice": {"pid": 1, "port": 7001}})
    monkeypatch.setattr(supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(supervisor, "remote_for_slug", lambda slug: {"id": slug, "url": "http://r:1", "token": "sek"})
    assert proxy._resolve_slug("alice") == ("local", "http://127.0.0.1:7001", None)
    assert proxy._resolve_slug("ava") == ("remote", "http://r:1", "sek")
    monkeypatch.setattr(supervisor, "remote_for_slug", lambda slug: {"id": slug, "url": "http://r:1", "token": ""})
    assert proxy._resolve_slug("ava") == ("remote", "http://r:1", None)
