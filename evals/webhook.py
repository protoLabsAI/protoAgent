"""Lightweight webhook receiver for A2A push-notification eval cases.

A2A push notifications are a single POST with a JSON body to a consumer
callback URL. To assert on them in an eval (or test) without standing up a
real web server, ``webhook_listener`` runs a raw ``asyncio`` HTTP server on an
ephemeral port and captures every POST body + headers into a ``WebhookCapture``
the caller asserts against — no FastAPI/aiohttp dependency.

    async with webhook_listener() as (url, capture):
        # register `url` as the task's pushNotificationConfig, run the task …
        assert capture.received  # the agent delivered a notification

Backported from the protoLabs fleet (gina ``evals/webhook.py``).
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class WebhookCapture:
    received: list[dict] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def webhook_listener(host: str = "127.0.0.1"):
    """Yield ``(url, capture)``; the server runs until the context exits."""
    port = _free_port()
    capture = WebhookCapture()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readline()  # request line (ignored)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"", b"\r\n"):
                    break
                name, _, val = line.decode("latin-1").partition(":")
                if name:
                    headers[name.strip().lower()] = val.strip()

            length = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(length) if length else b""
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                payload = {"_raw": body.decode("utf-8", errors="replace")}

            capture.received.append(payload)
            capture.headers.append(headers)

            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port)
    url = f"http://{host}:{port}/webhook"
    try:
        yield url, capture
    finally:
        server.close()
        await server.wait_closed()
