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
        monkeypatch.setattr(proxy, "_target_for_slug",
                            lambda slug: (f"http://127.0.0.1:{port}", {}))
        with TestClient(_ws_app()).websocket_connect("/agents/peer/live") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "ping"             # text round-trips through the hub
            ws.send_bytes(b"\x00\x01\x02")
            assert ws.receive_bytes() == b"\x00\x01\x02"   # binary round-trips too
    finally:
        stop()


def test_ws_proxy_rejects_when_agent_not_running(monkeypatch):
    # No live target → the proxy closes the handshake (the WS analog of the HTTP 409).
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: None)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_ws_app()).websocket_connect("/agents/ghost/live"):
            pass
