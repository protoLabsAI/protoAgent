"""An in-process publish/subscribe event bus (ADR 0003, extended for ADR 0039).

The bus is the foundational "broadcast and forget" primitive. Any component on the
event loop can ``publish`` a topic; it fans out two ways:

- **SSE consoles** — each connected console subscribes via ``GET /api/events`` and
  receives the stream (then filters client-side). This is the original ADR 0003 path.
- **In-process handlers** — server-side plugins register ``subscribe_handler(topic,
  handler)`` to react in-process, filtered by topic (ADR 0039). The bus is the *only*
  inter-plugin channel; plugins never import one another.

Topics are dot-namespaced (``<plugin>.<event>``, e.g. ``artifact.created``). Handler
subscriptions match by pattern with two wildcards: ``*`` matches one segment,
``#`` matches the rest (tail). ``"#"`` alone matches everything.

Each SSE subscriber gets its own bounded queue; on overflow the bus drops that
subscriber's *oldest* event, so one slow console never backpressures a producer or
the others. A small ring buffer retains the most recent events so a reconnecting
console can catch up via ``?since=<seq>`` (ephemeral — there is no durable log; the
Activity thread persists agent-facing events).

``publish`` is synchronous and must be called from the event-loop thread (every
producer — the A2A terminal hook, the scheduler, the inbox, plugins — runs there).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger("protoagent.events")


def topic_matches(pattern: str, topic: str) -> bool:
    """Does ``topic`` match a subscription ``pattern``? ``*`` = one segment, ``#`` = tail.

    ``"#"`` matches everything; ``"artifact.*"`` matches ``artifact.created`` but not
    ``artifact.a.b``; ``"artifact.#"`` matches both ``artifact`` and ``artifact.a.b``.
    """
    if pattern == "#" or pattern == topic:
        return True
    pp = pattern.split(".")
    tp = topic.split(".")
    for i, seg in enumerate(pp):
        if seg == "#":
            return True  # tail wildcard — rest matches
        if i >= len(tp):
            return False
        if seg == "*":
            continue
        if seg != tp[i]:
            return False
    return len(pp) == len(tp)


class EventBus:
    def __init__(self, *, max_queue: int = 256, ring: int = 128) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._handlers: list[tuple[str, Callable[[dict[str, Any]], Any]]] = []
        self._max_queue = max_queue
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring)
        self._seq = 0

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Fan a topic out to SSE subscribers + matching in-process handlers (ADR 0039).

        Fire-and-forget: a handler that raises is logged and isolated — it can never
        break the publisher or another subscriber.
        """
        self._seq += 1
        payload = {"event": event, "data": data or {}, "seq": self._seq}
        self._ring.append(payload)

        # SSE consoles — drop-oldest on overflow.
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - racing producers
                pass

        # In-process handlers — topic-filtered, isolated.
        for pattern, handler in list(self._handlers):
            if not topic_matches(pattern, event):
                continue
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    # Handler is async — schedule it; never block the publisher.
                    asyncio.get_running_loop().create_task(result)
            except Exception:  # noqa: BLE001 — one bad subscriber can't break the bus
                log.exception("[events] handler for %r failed on %r", pattern, event)

    def subscribe_handler(
        self, topic: str, handler: Callable[[dict[str, Any]], Any]
    ) -> Callable[[], None]:
        """Register an in-process handler for ``topic`` (ADR 0039). Returns an
        unsubscribe callable. ``handler`` receives the full payload
        ``{"event", "data", "seq"}`` and may be sync or async."""
        entry = (topic, handler)
        self._handlers.append(entry)

        def _unsubscribe() -> None:
            try:
                self._handlers.remove(entry)
            except ValueError:
                pass

        return _unsubscribe

    async def subscribe(self, since: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield events until the consumer stops (e.g. the SSE connection closes).

        If ``since`` is given, first replays any retained ring-buffer events newer than
        that seq (reconnect catch-up), then streams live."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        try:
            if since is not None:
                for payload in list(self._ring):
                    if payload.get("seq", 0) > since:
                        yield payload
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def handler_count(self) -> int:
        return len(self._handlers)
