"""An in-process publish/subscribe event bus (ADR 0003).

The bus is the foundational "server speaks unprompted" primitive: any component
running on the event loop can ``publish`` an event, and every connected console
(subscribed via the ``GET /api/events`` SSE route) receives it. It is read-only
server→client — subscribers never push back through it.

Each subscriber gets its own bounded queue. On overflow the bus drops the
*oldest* event for that subscriber and enqueues the newest, so one slow console
can never apply backpressure to a producer (or to the other subscribers).

``publish`` is synchronous and must be called from the event-loop thread (every
producer — the A2A terminal hook, the scheduler, the inbox — already runs there).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class EventBus:
    def __init__(self, *, max_queue: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Fan an event out to every current subscriber (drop-oldest on overflow)."""
        payload = {"event": event, "data": data or {}}
        for q in list(self._subscribers):
            if q.full():
                # Drop the oldest queued event to make room for the newest.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - racing producers
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events until the consumer stops (e.g. the SSE connection closes)."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subscribers)
