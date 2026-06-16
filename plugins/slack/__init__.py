"""Slack communication plugin (ADR 0029) — a Socket Mode ``ChatAdapter``.

Shows the standard handling a **websocket** transport (vs Telegram's HTTP
long-poll): Socket Mode opens a WSS via ``apps.connections.open``, receives event
envelopes (which must be **acked** by echoing the ``envelope_id``), and replies via
``chat.postMessage``. Needs a bot token (``xoxb-``) for the Web API and an
app-level token (``xapp-``, ``connections:write``) for the socket. The adapter is
the only platform-specific code; the wirer handles the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging

from graph.plugins.chat_surface import InboundMessage, register_chat_surface

log = logging.getLogger("protoagent.plugins.slack")

_API = "https://slack.com/api/{method}"


class SlackAdapter:
    id = "slack"
    chunk_limit = 39000  # Slack's ~40k message cap, with headroom

    def _bot(self, cfg: dict) -> str:
        return (cfg.get("bot_token") or "").strip()

    def _app(self, cfg: dict) -> str:
        return (cfg.get("app_token") or "").strip()

    def configured(self, cfg: dict) -> bool:
        return bool(self._bot(cfg) and self._app(cfg))

    async def validate(self, cfg: dict) -> tuple[bool, str | None, str | None]:
        import httpx

        bot = self._bot(cfg)
        if not (bot and self._app(cfg)):
            return (False, None, "Need both a bot token (xoxb-) and an app-level token (xapp-).")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(_API.format(method="auth.test"), headers={"Authorization": f"Bearer {bot}"})
            data = resp.json()
            if data.get("ok"):
                return (True, data.get("user") or data.get("team"), None)
            return (False, None, data.get("error") or "auth.test failed")
        except Exception as exc:  # noqa: BLE001
            return (False, None, str(exc))

    async def run(self, handle, *, cfg: dict, host) -> None:
        import httpx
        import websockets

        bot, app = self._bot(cfg), self._app(cfg)
        async with httpx.AsyncClient(timeout=30) as client:

            async def _open_socket() -> str:
                resp = await client.post(
                    _API.format(method="apps.connections.open"), headers={"Authorization": f"Bearer {app}"}
                )
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"apps.connections.open: {data.get('error')}")
                return data["url"]

            async def _post(channel: str, text: str) -> None:
                await client.post(
                    _API.format(method="chat.postMessage"),
                    headers={"Authorization": f"Bearer {bot}"},
                    json={"channel": channel, "text": text},
                )

            log.info("[slack] gateway started (socket mode)")
            while True:
                try:
                    url = await _open_socket()
                    async with websockets.connect(url) as ws:
                        async for raw in ws:
                            env = json.loads(raw)
                            etype = env.get("type")
                            if etype == "disconnect":
                                break  # Slack asked us to reconnect
                            if env.get("envelope_id"):
                                await ws.send(json.dumps({"envelope_id": env["envelope_id"]}))  # ack
                            if etype != "events_api":
                                continue
                            ev = (env.get("payload") or {}).get("event") or {}
                            # Skip non-messages, edits, and our own / other bots' posts (loop guard).
                            if ev.get("type") != "message" or ev.get("subtype") or ev.get("bot_id"):
                                continue
                            text, channel, uid = ev.get("text"), ev.get("channel"), ev.get("user")
                            if not (text and channel):
                                continue

                            async def reply(out: str, _ch=channel) -> None:
                                await _post(_ch, out)

                            await handle(
                                InboundMessage(
                                    text=text,
                                    user_id=str(uid or ""),
                                    channel_id=str(channel),
                                    reply=reply,
                                )
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — transient socket/API error; reconnect
                    log.exception("[slack] socket error; reconnecting in 5s")
                    await asyncio.sleep(5)


def register(registry) -> None:
    register_chat_surface(registry, SlackAdapter())
