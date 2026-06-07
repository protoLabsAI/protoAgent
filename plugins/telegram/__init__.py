"""Telegram communication plugin (ADR 0029) — the reference ``ChatAdapter``.

Self-contained: the Telegram Bot API is plain HTTP (httpx long-poll), so unlike
Discord there's no ``surfaces/`` module — the adapter *is* the transport. This is
the template for new comms plugins: implement ``ChatAdapter`` (connect, receive →
``handle``, send) + a manifest, and ``register()`` is one line. Slack/WhatsApp/etc.
follow the same shape.
"""

from __future__ import annotations

import asyncio
import logging

from graph.plugins.chat_surface import InboundMessage, register_chat_surface

log = logging.getLogger("protoagent.plugins.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramAdapter:
    """Telegram Bot API over httpx long-poll. The whole platform-specific surface."""

    id = "telegram"
    chunk_limit = 4096  # Telegram's per-message character cap

    def _token(self, cfg: dict) -> str:
        return (cfg.get("bot_token") or "").strip()

    def configured(self, cfg: dict) -> bool:
        return bool(self._token(cfg))

    async def validate(self, cfg: dict) -> tuple[bool, str | None, str | None]:
        import httpx

        token = self._token(cfg)
        if not token:
            return (False, None, "No bot token set.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(_API.format(token=token, method="getMe"))
            data = resp.json()
            if data.get("ok"):
                return (True, (data.get("result") or {}).get("username"), None)
            return (False, None, data.get("description") or "Invalid bot token")
        except Exception as exc:  # noqa: BLE001
            return (False, None, str(exc))

    async def run(self, handle, *, cfg: dict, host) -> None:
        import httpx

        token = self._token(cfg)
        offset = 0
        # timeout > the 30s long-poll so the connection isn't cut mid-poll.
        async with httpx.AsyncClient(timeout=40) as client:

            async def _send(chat_id, text: str) -> None:
                await client.post(_API.format(token=token, method="sendMessage"),
                                  json={"chat_id": chat_id, "text": text})

            log.info("[telegram] gateway started (long-poll)")
            while True:
                try:
                    resp = await client.get(
                        _API.format(token=token, method="getUpdates"),
                        params={"offset": offset, "timeout": 30},
                    )
                    updates = (resp.json() or {}).get("result") or []
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — transient network/API error; back off + retry
                    log.exception("[telegram] getUpdates failed; backing off 5s")
                    await asyncio.sleep(5)
                    continue
                for upd in updates:
                    offset = upd["update_id"] + 1
                    message = upd.get("message") or {}
                    text = message.get("text")
                    if not text:
                        continue
                    chat_id = (message.get("chat") or {}).get("id")
                    user_id = str((message.get("from") or {}).get("id") or "")

                    async def reply(out: str, _cid=chat_id) -> None:
                        await _send(_cid, out)

                    await handle(InboundMessage(
                        text=text, user_id=user_id, channel_id=str(chat_id), reply=reply,
                    ))


def register(registry) -> None:
    register_chat_surface(registry, TelegramAdapter())
