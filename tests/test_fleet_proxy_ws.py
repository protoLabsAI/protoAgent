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
        monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (f"http://127.0.0.1:{port}", {}))
        with TestClient(_ws_app()).websocket_connect("/agents/peer/live") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "ping"  # text round-trips through the hub
            ws.send_bytes(b"\x00\x01\x02")
            assert ws.receive_bytes() == b"\x00\x01\x02"  # binary round-trips too
    finally:
        stop()


def test_ws_proxy_rejects_when_agent_not_running(monkeypatch):
    # No live target → the proxy closes the handshake (the WS analog of the HTTP 409).
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: None)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_ws_app()).websocket_connect("/agents/ghost/live"):
            pass


def test_ws_proxy_refuses_remote_member(monkeypatch):
    """Security: the hub's default-deny auth is HTTP-only (BaseHTTPMiddleware skips WS
    scopes), so a WS proxied to a REMOTE member would lend that member's stored bearer to
    an unauthenticated caller. forward_ws refuses it (1008) BEFORE opening any upstream —
    even though a target would resolve. Local peers/host are unaffected (no stored creds)."""
    from graph.fleet import supervisor

    # Slug resolves to a registered remote (with a stored token) and NOT to a live local peer.
    monkeypatch.setattr(supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(
        supervisor, "remote_for_slug", lambda slug: {"id": slug, "url": "http://100.64.0.9:7870", "token": "sek"}
    )
    # If the guard failed to fire, _target_for_slug would hand back the remote target and the
    # relay would try to dial it — make that loud rather than a silent connect attempt.
    monkeypatch.setattr(
        proxy, "_target_for_slug", lambda slug: pytest.fail("must refuse a remote before resolving a target")
    )
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(_ws_app()).websocket_connect("/agents/ava/live"):
            pass
    assert exc.value.code == 1008  # policy violation, not a transient "not running"


def test_ws_proxy_still_serves_live_local_peer(monkeypatch):
    """A running LOCAL peer (a live pid in fleet state) still proxies — the remote refusal
    must not catch same-slug local members."""
    port, stop = _echo_ws_server()
    try:
        from graph.fleet import supervisor

        monkeypatch.setattr(supervisor, "_load_state", lambda: {"peer": {"pid": 4242, "port": port}})
        monkeypatch.setattr(supervisor, "_alive", lambda pid: True)
        monkeypatch.setattr(supervisor, "remote_for_slug", lambda slug: None)
        monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (f"http://127.0.0.1:{port}", {}))
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
