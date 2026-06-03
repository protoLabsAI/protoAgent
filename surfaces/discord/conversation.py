"""Conversation continuity for the Discord gateway (ADR 0015).

Per-``(channel, user)`` conversation state: when a user @-mentions the agent in
a channel, follow-up messages within the timeout window continue the same
conversation without another mention. The ``conversation_id`` is passed to the
agent as the session/thread key so the LangGraph thread stays consistent across
turns. DMs use a wider window (they're 1:1 sessions).

Ported from ``-deprecated-gina`` (which mirrors Workstacean's ConversationManager
— same key shape, same timeout semantics, same get_or_create / has / end API).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger("protoagent.discord.conversation")


@dataclass
class ConversationEntry:
    conversation_id: str
    channel_id: str
    user_id: str
    started_at: float
    last_activity: float
    timeout_s: float
    turn_number: int


class ConversationManager:
    """Tracks active multi-turn conversations by ``(channel_id, user_id)``.

    A periodic sweep (every ``sweep_interval_s``) drops expired conversations.
    """

    def __init__(self, sweep_interval_s: float = 30.0):
        self._conversations: dict[str, ConversationEntry] = {}
        self._sweep_interval_s = sweep_interval_s
        self._sweep_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the periodic sweep. Idempotent."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

    @staticmethod
    def _key(channel_id: str, user_id: str) -> str:
        return f"{channel_id}:{user_id}"

    def get_or_create(
        self, channel_id: str, user_id: str, *, timeout_s: float = 300.0
    ) -> tuple[str, bool, int]:
        """Return ``(conversation_id, is_new, turn_number)``. An active
        (unexpired) conversation bumps the turn number; otherwise a fresh one
        starts."""
        key = self._key(channel_id, user_id)
        now = time.monotonic()
        existing = self._conversations.get(key)
        if existing and (now - existing.last_activity) < existing.timeout_s:
            existing.last_activity = now
            existing.turn_number += 1
            return existing.conversation_id, False, existing.turn_number

        conversation_id = str(uuid.uuid4())
        self._conversations[key] = ConversationEntry(
            conversation_id=conversation_id,
            channel_id=channel_id,
            user_id=user_id,
            started_at=now,
            last_activity=now,
            timeout_s=timeout_s,
            turn_number=1,
        )
        return conversation_id, True, 1

    def has(self, channel_id: str, user_id: str) -> bool:
        entry = self._conversations.get(self._key(channel_id, user_id))
        if entry is None:
            return False
        return (time.monotonic() - entry.last_activity) < entry.timeout_s

    def end(self, channel_id: str, user_id: str) -> bool:
        return self._conversations.pop(self._key(channel_id, user_id), None) is not None

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_s)
                self._sweep_once()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("[conversation] sweep loop crashed")

    def _sweep_once(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._conversations.items()
            if (now - entry.last_activity) >= entry.timeout_s
        ]
        for key in expired:
            entry = self._conversations.pop(key, None)
            if entry is not None:
                log.info(
                    "[conversation] timed out %s (%d turn(s), user %s in %s)",
                    entry.conversation_id, entry.turn_number, entry.user_id, entry.channel_id,
                )
