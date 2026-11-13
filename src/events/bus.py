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
the others. A small ring buffer retains the most recent events for live catch-up;
with a :class:`events.change_log.ChangeLog` attached (ADR 0130 S1, the server does
this at boot) every retained publish also lands in a durable per-instance log, so a
``?since=<seq>`` / ``Last-Event-ID`` resume replays from the log — surviving both
ring eviction and a server restart — and a cursor older than the log's retention is
answered with a single ``resync_required`` event instead of a silent gap.

``publish`` is synchronous and normally runs on the event-loop thread (every core
producer — the A2A terminal hook, the scheduler, the inbox — runs there). Plugin
middleware is the exception: LangGraph may run *sync* hooks (``before_model``,
``wrap_tool_call``) in worker threads, so ``publish`` detects an off-loop call and
reroutes itself onto the bound loop via ``call_soon_threadsafe`` — emitting from any
middleware hook is safe. The loop is captured from normal on-loop use (first publish,
SSE subscribe, or handler registration); an off-loop publish before any loop is bound
is dropped with a warning (matches the no-bus-wired semantics elsewhere).
"""

from __future__ import annotations

import asyncio
import time
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from events.change_log import entity_ref_of

if TYPE_CHECKING:
    from events.change_log import ChangeLog

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._change_log: ChangeLog | None = None

    def attach_change_log(self, change_log: ChangeLog) -> None:
        """Make catch-up durable (ADR 0130 S1). From here on every retained publish
        appends a ``(seq, kind, entity_id, payload, ts)`` row, and ``subscribe(since=…)``
        replays from the log instead of the in-memory ring. The bus seq continues
        from the log's resume point — its durable high-water (rows, the sweep floor,
        and the throttled mark unretained publishes leave) plus one persist window of
        slack — so after a restart new events never re-mint seqs a client's
        ``Last-Event-ID`` cursor already points past, even a cursor taken from a
        live-only progress frame the log has no row for."""
        self._change_log = change_log
        self._seq = max(self._seq, change_log.resume_seq())

    def _bind_loop(self) -> None:
        """Remember the running loop (if any) so off-loop publishes can find it."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def publish(self, event: str, data: dict[str, Any] | None = None, *, retain: bool = True) -> None:
        """Fan a topic out to SSE subscribers + matching in-process handlers (ADR 0039).

        Fire-and-forget: a handler that raises is logged and isolated — it can never
        break the publisher or another subscriber.

        ``retain=False`` delivers the event live but keeps it OUT of the replay ring —
        for high-rate, live-only topics (per-frame turn progress) whose value expires
        the moment the turn ends. Two reasons, both load-bearing:

        * the ring holds 128 events total, so one long turn's progress would evict the
          durable events a reconnecting client actually needs to catch up on
          (``chat.resumed``, ``background.completed``);
        * replaying a stale progress frame is worse than dropping it — a tab that
          reconnects after the turn finished would render tool cards for work that is
          already over.

        Retained (the default) is right for anything a late subscriber must not miss.

        Thread-safe: asyncio queues are not, so an off-loop call (a plugin middleware
        emitting from a sync hook LangGraph ran in a worker thread) is rerouted onto
        the bound event loop instead of corrupting subscriber queues. Delivery stays
        in publish order per thread; seq is assigned on the loop.
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # off-loop (e.g. a sync middleware hook in a worker thread)
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._publish_on_loop, event, data, retain)
                return
            # No live loop anywhere (sync tests, CLI): deliver inline. Safe — without
            # a loop there are no SSE queues to corrupt, and sync handlers run fine.
        self._publish_on_loop(event, data, retain)

    def _publish_on_loop(self, event: str, data: dict[str, Any] | None, retain: bool = True) -> None:
        self._seq += 1
        payload = {"event": event, "data": data or {}, "seq": self._seq, "ts": time.time()}
        # seq still advances for an unretained event: it is a global publish counter the
        # SSE stream reports, not a ring index, so a client's `since` cursor stays honest
        # about what it has already seen rather than silently rewinding.
        if retain:
            self._ring.append(payload)
        if self._change_log is not None:
            try:
                if retain:
                    # The durable half of the ring (ADR 0130 S1): appended here, in the
                    # same step that assigned seq, so log order IS seq order. The row
                    # carries the entity identity (D2: a resuming client refetches) AND
                    # the payload verbatim, so a replay past the ring stays
                    # frame-identical for today's payload-reading clients (the deck's
                    # feed branches on turn.usage/turn.finished fields).
                    key, eid = entity_ref_of(data)
                    self._change_log.append(
                        self._seq, event, eid, payload["ts"], entity_key=key, data=payload["data"]
                    )
                else:
                    # A progress frame leaves no row, but its seq reaches clients (the
                    # payload and the SSE id:), so the counter's high-water must survive
                    # a restart or a resume cursor could be re-minted (throttled write).
                    self._change_log.note_seq(self._seq)
            except Exception:  # noqa: BLE001 — a full disk must not break live publish
                log.exception("[events] change-log write failed for %r", event)

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

    def subscribe_handler(self, topic: str, handler: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Register an in-process handler for ``topic`` (ADR 0039). Returns an
        unsubscribe callable. ``handler`` receives the full payload
        ``{"event", "data", "seq"}`` and may be sync or async."""
        self._bind_loop()
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

        If ``since`` is given, first replays retained events newer than that seq
        (reconnect catch-up), then streams live. With a change log attached (ADR
        0130 S1) the replay comes from the durable log: every row comes back with
        its original payload — the log stores it verbatim, so a frame replayed
        across a restart or past ring eviction is indistinguishable from the live
        one — falling back to an identity-only frame (``entity_id`` plus the
        payload key the original publish named the entity under, resolved by an
        entity fetch) only for a row whose payload could not be stored. A cursor
        older than retention — or ahead of everything this instance has seen (a
        wiped/foreign log) — gets one ``resync_required`` event telling the client
        to refetch its snapshots instead of trusting a silent gap. A bare bus (no
        log — unit tests, CLI) replays from the ring exactly as before — and a
        cursor it can't place (a previous process's counter; the bare seq restarts
        at 0) never gates the live stream."""
        self._bind_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        try:
            # Highest seq the replay below actually DELIVERED — deliberately not the
            # raw cursor: a stale cursor (another run's counter) must never suppress
            # live events. Queue registration (above) and the replay read run in one
            # synchronous segment, so no publish can land in both; the filter is
            # belt-and-braces for a replayed frame also sitting in the queue.
            replayed_to = 0
            if since is not None:
                batch = (
                    self._log_replay(self._change_log, since)
                    if self._change_log is not None
                    else [p for p in self._ring if p.get("seq", 0) > since]
                )
                for payload in batch:
                    replayed_to = max(replayed_to, payload.get("seq", 0))
                    yield payload
            while True:
                payload = await q.get()
                if payload.get("seq", 0) <= replayed_to:
                    continue
                yield payload
        finally:
            self._subscribers.discard(q)

    def _log_replay(self, clog: ChangeLog, since: int) -> list[dict[str, Any]]:
        """The catch-up batch for a resuming cursor, from the durable log (ADR 0130).

        Runs synchronously on the loop (no await between the queue registration and
        this read), so no event can fall between the batch and the live queue."""
        if since > self._seq or not clog.covers(since):
            # Older than retention (or from another log's epoch): a full replay is
            # impossible, so say so once. The frame carries the server's current seq
            # as its id — after the client resyncs its snapshots, its next reconnect
            # resumes from here instead of re-triggering the resync forever.
            return [
                {
                    "event": "resync_required",
                    "data": {"since": since, "floor": clog.floor_seq(), "last_seq": self._seq},
                    "seq": self._seq,
                    "ts": time.time(),
                }
            ]
        ring_by_seq = {p.get("seq"): p for p in self._ring}
        batch: list[dict[str, Any]] = []
        logged: set[int] = set()
        for row in clog.read_since(since):
            seq = row["seq"]
            logged.add(seq)
            live = ring_by_seq.get(seq)
            if live is not None:
                # The ring still holds the original frame object → replay it verbatim.
                batch.append(live)
                continue
            data = row["data"]
            if not isinstance(data, dict):
                # The payload couldn't be stored (unencodable or over the size cap) →
                # identity-only (D2): the id under the generic key a sync client reads
                # AND the key the original publish named it under, so today's consumers
                # (the deck's feed keys turn.* on session_id/context_id) keep keying
                # correctly instead of minting ""-keyed state.
                data = {"entity_id": row["entity_id"]}
                if row["entity_key"] and row["entity_key"] != "entity_id":
                    data[row["entity_key"]] = row["entity_id"]
            batch.append({"event": row["kind"], "data": data, "seq": seq, "ts": row["ts"]})
        # A retained event whose log append failed (full disk; publish logs the error
        # and carries on) has no row but may still sit in the ring — merge it back in
        # so the reconnect replays it instead of silently skipping it.
        extras = [p for p in self._ring if p.get("seq", 0) > since and p.get("seq") not in logged]
        if extras:
            batch.extend(extras)
            batch.sort(key=lambda p: p.get("seq", 0))
        return batch

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def handler_count(self) -> int:
        return len(self._handlers)
