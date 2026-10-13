"""Per-lane, priority-ordered in-flight limiter for model calls (ADR 0115).

Nothing in the runtime bounds how many chat-model calls are in flight at once, so a
burst of fan-outs all hit the gateway together, all time out together, and all retry
together (#3760, the #209 amplification). This module is the pure mechanism ADR 0115
D1–D6/D8 describes: a bounded, priority-ordered wait queue per ``(base_url, model)``
lane, created lazily per process.

It is deliberately self-contained — it imports **nothing** from ``graph.llm`` or
``graph.config`` (import-layering + testability). Later cards wire it in: config in C3,
``graph/llm.py`` in C4, priority tagging in C5, Prometheus/route in C6. C6 feeds metrics
through :func:`add_listener` rather than this module importing ``observability``.

Everything is driven by an injectable monotonic clock, so aging, the reserve, the queue
timeout and the snapshot are deterministic under test (ADR 0115 "Host-side testing").
The queue-wait deadline itself is a real ``asyncio.wait_for`` so a saturated-and-idle
lane still times its waiters out with no external pump.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime
import logging
import math
import time
from collections import deque, namedtuple
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Priority classes (ADR 0115 D6) ──────────────────────────────────────────────
INTERACTIVE = "interactive"  # chat / console turns an operator is watching
DEFAULT = "default"  # A2A, background and scheduled turns, subagents
BULK = "bulk"  # workflow fan-outs, sweeps, review-panel finders
PRIORITIES: tuple[str, ...] = (INTERACTIVE, DEFAULT, BULK)

# Serving rank: higher wins. Aging (below) raises a waiter's effective rank one step per
# minute, so `bulk` cannot starve behind a steady stream of `default`/`interactive` work.
_RANK: dict[str, int] = {BULK: 0, DEFAULT: 1, INTERACTIVE: 2}
_MAX_RANK = _RANK[INTERACTIVE]

# Internal constants (NOT config keys, per D6). Aging step, the "saturated" dwell, and the
# rolling window the snapshot percentiles/timeout count are computed over.
_AGING_INTERVAL_S = 60.0
_SATURATION_AFTER_S = 60.0
_WINDOW_S = 300.0  # 5 minutes, for wait_p50/p90 and queue_timeouts_5m

# The priority the current logical call runs under. A ContextVar (the `_REQUEST_MEASURE`
# pattern in graph/llm.py) so a subagent `task()` inherits its parent's class. Anything
# unset is `default`.
_PRIORITY: contextvars.ContextVar[str] = contextvars.ContextVar("_llm_priority", default=DEFAULT)


# ── Metrics hook (ADR 0115 D8) ───────────────────────────────────────────────────
# A LaneEvent carries everything C6 needs to update the four D8 signals without this
# module importing observability: the kind, the lane/priority, the wait time (grants and
# timeouts only), and the current gauges. `kind` is one of enqueued / acquired / released /
# dequeued / timeout — every queue-depth AND in-flight transition emits, so the gauges a
# listener maintains stay live (climb as waiters pile up, drain to 0 when the lane clears)
# rather than only refreshing on a grant/release/timeout.
LaneEvent = namedtuple("LaneEvent", ["kind", "lane", "priority", "wait_s", "inflight", "queued"])

_listeners: list[Callable[[LaneEvent], None]] = []


def add_listener(fn: Callable[[LaneEvent], None]) -> None:
    """Register a hook called on every queue/slot transition — enqueue, acquire, release,
    dequeue and timeout. C6 uses this to feed Prometheus, and refreshes the depth gauge on
    the enqueue/dequeue events so a busy lane's backlog is visible and a drained lane reads
    0. Listeners must not raise; a raising listener is logged and dropped from that dispatch,
    never allowed to break a model call."""
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn: Callable[[LaneEvent], None]) -> None:
    with contextlib.suppress(ValueError):
        _listeners.remove(fn)


def _dispatch(event: LaneEvent) -> None:
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:  # noqa: BLE001 — a bad listener must never fail a model call
            log.debug("[llm-limiter] listener raised on %s", event.kind, exc_info=True)


# ── Priority context (used by C5) ─────────────────────────────────────────────────
def _validate(priority: str) -> str:
    if priority not in _RANK:
        raise ValueError(f"unknown llm priority {priority!r}; expected one of {PRIORITIES}")
    return priority


def get_priority() -> str:
    """The class the current logical call runs under (default ``default``)."""
    return _PRIORITY.get()


def set_priority(priority: str) -> contextvars.Token:
    """Set the current class; returns a token for :func:`reset_priority`. Rejects unknown
    classes."""
    return _PRIORITY.set(_validate(priority))


def reset_priority(token: contextvars.Token) -> None:
    _PRIORITY.reset(token)


@contextlib.contextmanager
def priority_scope(priority: str):
    """Run a block under ``priority``, restoring the previous class on exit."""
    token = set_priority(priority)
    try:
        yield
    finally:
        reset_priority(token)


class GatewayQueueTimeout(TimeoutError):
    """A caller waited longer than ``model.inflight_queue_timeout`` for an in-flight slot.

    Subclasses ``TimeoutError`` but is deliberately NOT in ``RETRYABLE_STREAM_ERRORS``
    (ADR 0115 D4): retrying a queue timeout just rejoins the queue and reproduces the
    #209 amplification locally. Acquisition sits outside the per-chunk stall guard, so
    this can never be mislabelled a ``StreamStallTimeout``. The message names the lane,
    the wait, and the queue position, and points at the two knobs that widen the queue."""

    def __init__(self, lane: str, waited_s: float, position: int) -> None:
        self.lane = lane
        self.waited_s = waited_s
        self.position = position
        super().__init__(
            f"gateway lane {lane!r} in-flight queue timed out after {waited_s:.1f}s "
            f"at queue position {position} — raise model.max_inflight or "
            f"model.inflight_queue_timeout, or reduce concurrent load on this lane"
        )


@dataclass
class _Waiter:
    """One caller's place in a lane's queue."""

    priority: str
    wait_start: float  # this acquisition's clock; the queue-timeout + wait metrics use it
    arrival: float  # the logical call's start; aging counts from here (D5 reconnect)
    seq: int  # tie-break within a class: strictly FIFO
    future: asyncio.Future
    holding: bool = False  # True once this waiter has been granted a slot


class _Lane:
    """The limiter for a single ``(base_url, model)`` lane.

    A lane is pure and single-event-loop: no threading locks, no background task. Aging
    is evaluated lazily at each admission decision (which only happens when a slot frees
    or a waiter arrives), so no timer is needed to re-order the queue over time.
    """

    def __init__(
        self,
        lane: str,
        *,
        limit: int,
        queue_timeout: float,
        interactive_reserve: int,
        clock: Callable[[], float],
    ) -> None:
        self._lane = lane
        self._limit = max(0, int(limit))
        self._queue_timeout = float(queue_timeout)
        self._reserve = max(0, int(interactive_reserve))
        self._clock = clock
        self._inflight = 0
        self._non_interactive = 0  # slots held by non-`interactive` callers (reserve math)
        self._queue: list[_Waiter] = []
        self._seq = 0
        self._queue_nonempty_since: float | None = None  # for `saturated`
        self._waits: deque[tuple[float, float, str]] = deque()  # (recorded_at, wait_s, priority)
        self._timeouts: deque[tuple[float, str]] = deque()  # (recorded_at, priority)

    # ── config (D2): applies to acquisitions after the change; held slots never revoked ──
    def reconfigure(self, *, limit: int, queue_timeout: float, interactive_reserve: int, clock=None) -> None:
        self._limit = max(0, int(limit))
        self._queue_timeout = float(queue_timeout)
        self._reserve = max(0, int(interactive_reserve))
        if clock is not None:
            self._clock = clock
        now = self._clock()
        if self._limit == 0:
            # Limiter switched off at runtime (D2, the documented return to today's
            # unlimited behaviour): the lane no longer bounds anything, so nobody must be
            # left queued. New callers already pass straight through (the module-level
            # acquire short-circuits at `_LIMIT == 0`); if the waiters already in line kept
            # waiting they would sit out the full queue_timeout and fail GatewayQueueTimeout
            # while everyone else skips the limiter. Admit them all at once.
            self._drain_passthrough(now)
        else:
            # A raised limit may free capacity for waiters already in the queue.
            self._admit_waiters(now)

    def _drain_passthrough(self, now: float) -> None:
        """Grant every queued waiter immediately. Used when the limit is 0 (the limiter is
        disabled): there is no capacity to respect and nothing should stay queued, so this
        bypasses the ``inflight >= limit`` gate in :meth:`_select` that would otherwise
        strand them forever."""
        for w in list(self._queue):
            self._grant(w, now)

    @property
    def _reserve_clamped(self) -> int:
        """Reserve, clamped to ``limit − 1`` so non-interactive work always has a slot."""
        return min(self._reserve, max(0, self._limit - 1))

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ── ordering & aging (D6) ────────────────────────────────────────────────────
    def _effective_rank(self, w: _Waiter, now: float) -> int:
        aged = int(max(0.0, now - w.arrival) // _AGING_INTERVAL_S)
        return min(_RANK[w.priority] + aged, _MAX_RANK)

    def _order_key(self, w: _Waiter, now: float) -> tuple[int, int]:
        # Maximised: higher effective rank first, then smaller seq (FIFO within a class).
        return (self._effective_rank(w, now), -w.seq)

    def _select(self, now: float) -> _Waiter | None:
        """The single best admittable waiter, or None. A non-interactive waiter is only
        admittable while a non-reserved slot is free; an interactive waiter may take any
        free slot including the reserve. Reserve eligibility uses the *base* class — the
        reserve is a hard guarantee for genuinely interactive callers, independent of
        aging (which only reorders)."""
        if self._inflight >= self._limit:
            return None
        non_reserved_room = (self._limit - self._reserve_clamped) - self._non_interactive
        best: _Waiter | None = None
        best_key: tuple[int, int] | None = None
        for w in self._queue:
            if w.future.done():  # cancelled/timed-out; will be dequeued by its own finally
                continue
            if w.priority != INTERACTIVE and non_reserved_room <= 0:
                continue
            key = self._order_key(w, now)
            if best is None or key > best_key:  # type: ignore[operator]
                best, best_key = w, key
        return best

    def _admit_waiters(self, now: float) -> None:
        while True:
            w = self._select(now)
            if w is None:
                return
            self._grant(w, now)

    def _grant(self, w: _Waiter, now: float) -> None:
        self._queue.remove(w)
        if w.future.done():  # defensive: never count an already-resolved waiter
            self._update_saturation(now)
            return
        w.holding = True
        self._inflight += 1
        if w.priority != INTERACTIVE:
            self._non_interactive += 1
        waited = max(0.0, now - w.wait_start)
        self._record_wait(now, waited, w.priority)
        w.future.set_result(None)
        self._update_saturation(now)
        self._emit("acquired", w.priority, waited)

    def _release(self, w: _Waiter, now: float) -> None:
        w.holding = False
        self._inflight -= 1
        if w.priority != INTERACTIVE:
            self._non_interactive -= 1
        self._emit("released", w.priority, 0.0)
        self._admit_waiters(now)

    # ── queue bookkeeping ─────────────────────────────────────────────────────────
    def _enqueue(self, w: _Waiter, now: float) -> None:
        self._queue.append(w)
        self._update_saturation(now)
        # A waiter joining the queue changes the depth even though no slot moved; emit so
        # the D8 queue-depth gauge climbs with the backlog instead of sitting at its last
        # acquire/release value while every slot is busy.
        self._emit("enqueued", w.priority, 0.0)

    def _dequeue(self, w: _Waiter, now: float) -> None:
        with contextlib.suppress(ValueError):
            self._queue.remove(w)
        self._update_saturation(now)
        # A cancelled or timed-out waiter leaving the queue also moves the depth with no
        # slot transition; emit so the gauge drains (a fully cleared lane reads back to 0)
        # rather than keeping the ghost counted until some unrelated event fires.
        self._emit("dequeued", w.priority, 0.0)

    def _update_saturation(self, now: float) -> None:
        if self._queue:
            if self._queue_nonempty_since is None:
                self._queue_nonempty_since = now
        else:
            self._queue_nonempty_since = None

    def _position_of(self, w: _Waiter, now: float) -> int:
        """1-based position: how many queued waiters would be served before ``w``."""
        key = self._order_key(w, now)
        ahead = sum(1 for u in self._queue if u is not w and self._order_key(u, now) > key)
        return ahead + 1

    # ── metrics windows ───────────────────────────────────────────────────────────
    def _prune(self, now: float) -> None:
        cutoff = now - _WINDOW_S
        while self._waits and self._waits[0][0] < cutoff:
            self._waits.popleft()
        while self._timeouts and self._timeouts[0][0] < cutoff:
            self._timeouts.popleft()

    def _record_wait(self, now: float, waited: float, priority: str) -> None:
        self._waits.append((now, waited, priority))
        self._prune(now)

    def _record_timeout(self, now: float, priority: str) -> None:
        self._timeouts.append((now, priority))
        self._prune(now)

    def _emit(self, kind: str, priority: str, wait_s: float) -> None:
        _dispatch(LaneEvent(kind, self._lane, priority, wait_s, self._inflight, len(self._queue)))

    def saturated(self, now: float) -> bool:
        return (
            bool(self._queue)
            and self._queue_nonempty_since is not None
            and (now - self._queue_nonempty_since) >= _SATURATION_AFTER_S
        )

    def snapshot(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        self._prune(now)
        waits = [w for (_, w, _) in self._waits]
        by_priority = {INTERACTIVE: 0, DEFAULT: 0, BULK: 0}
        for w in self._queue:
            by_priority[w.priority] += 1
        oldest = max((now - w.wait_start for w in self._queue), default=0.0)
        return {
            "lane": self._lane,
            "limit": self._limit,
            "reserve": self._reserve_clamped,
            "inflight": self._inflight,
            "queued": len(self._queue),
            "queued_by_priority": {
                "interactive": by_priority[INTERACTIVE],
                "default": by_priority[DEFAULT],
                "bulk": by_priority[BULK],
            },
            "oldest_wait_s": round(oldest, 1),
            "wait_p50_s_5m": round(_percentile(waits, 0.5), 1),
            "wait_p90_s_5m": round(_percentile(waits, 0.9), 1),
            "queue_timeouts_5m": len(self._timeouts),
            "saturated": self.saturated(now),
        }

    # ── the context manager ───────────────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def acquire(self, priority: str, *, arrival: float | None = None):
        """Hold one slot for the body. ``limit == 0`` is a pass-through with no waiting and
        no bookkeeping beyond one branch. A slot is always released on exit, including on
        exception or cancellation; a cancelled or timed-out waiter leaves no residue."""
        if self._limit == 0:
            yield
            return

        now = self._clock()
        waiter = _Waiter(
            priority=priority,
            wait_start=now,
            arrival=now if arrival is None else arrival,
            seq=self._next_seq(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._enqueue(waiter, now)
        self._admit_waiters(now)  # may grant immediately if a slot is free

        try:
            if not waiter.holding:
                try:
                    await asyncio.wait_for(waiter.future, self._queue_timeout)
                except asyncio.TimeoutError:
                    if not waiter.holding:  # a genuine queue timeout (not a grant race)
                        end = self._clock()
                        waited = max(0.0, end - waiter.wait_start)
                        position = self._position_of(waiter, end)
                        self._dequeue(waiter, end)
                        self._record_timeout(end, waiter.priority)
                        self._emit("timeout", waiter.priority, waited)
                        raise GatewayQueueTimeout(self._lane, waited, position) from None
            yield
        finally:
            end = self._clock()
            if waiter.holding:
                self._release(waiter, end)
            else:
                # cancelled or timed out before a grant: drop it, leaking nothing.
                self._dequeue(waiter, end)
                if not waiter.future.done():
                    waiter.future.cancel()


# ── per-process registry + module-level config (D1/D2) ────────────────────────────
_LIMIT = 0  # model.max_inflight — 0 disables the limiter entirely (today's behaviour)
_QUEUE_TIMEOUT = 300.0  # model.inflight_queue_timeout
_RESERVE = 1  # model.inflight_interactive_reserve
_CLOCK: Callable[[], float] = time.monotonic
_LANES: dict[str, _Lane] = {}


def configure(
    *,
    limit: int | None = None,
    queue_timeout: float | None = None,
    interactive_reserve: int | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Set the process-wide limiter config (C3 calls this on config load/hot-reload).

    Every lane in a process shares one ``max_inflight`` (D1); per-lane overrides are
    future work. A change applies to acquisitions made after it — slots already held are
    never revoked (D2). ``clock`` is here for deterministic tests."""
    global _LIMIT, _QUEUE_TIMEOUT, _RESERVE, _CLOCK
    if limit is not None:
        _LIMIT = max(0, int(limit))
    if queue_timeout is not None:
        _QUEUE_TIMEOUT = float(queue_timeout)
    if interactive_reserve is not None:
        _RESERVE = max(0, int(interactive_reserve))
    if clock is not None:
        _CLOCK = clock
    for lane in _LANES.values():
        lane.reconfigure(
            limit=_LIMIT, queue_timeout=_QUEUE_TIMEOUT, interactive_reserve=_RESERVE, clock=_CLOCK
        )


def _get_or_create_lane(lane: str) -> _Lane:
    limiter = _LANES.get(lane)
    if limiter is None:
        limiter = _Lane(
            lane,
            limit=_LIMIT,
            queue_timeout=_QUEUE_TIMEOUT,
            interactive_reserve=_RESERVE,
            clock=_CLOCK,
        )
        _LANES[lane] = limiter
    return limiter


def acquire(lane: str, priority: str | None = None, *, arrival: float | None = None):
    """Acquire one in-flight slot on ``lane`` for the wrapped block.

    An async context manager. ``priority`` defaults to the current :func:`get_priority`
    class. ``arrival`` lets a reconnect re-acquire while preserving aging from the start
    of the logical call (D5). When ``max_inflight`` is 0 this is a bare pass-through with
    no lane created and no bookkeeping."""
    priority = get_priority() if priority is None else _validate(priority)

    @contextlib.asynccontextmanager
    async def _cm():
        if _LIMIT == 0:  # the one branch a disabled limiter costs
            yield
            return
        async with _get_or_create_lane(lane).acquire(priority, arrival=arrival):
            yield

    return _cm()


def snapshot() -> dict:
    """The ADR 0115 D8 payload for every lane this process has touched. ``enabled`` is
    false when ``max_inflight`` is 0 (the limiter is off)."""
    now = _CLOCK()
    return {
        "enabled": _LIMIT > 0,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "lanes": [lane.snapshot(now) for lane in _LANES.values()],
    }


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (``p`` in [0, 1]); 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = p * (len(ordered) - 1)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _reset_for_tests() -> None:
    """Clear the registry, listeners and config back to defaults. Test-only."""
    global _LIMIT, _QUEUE_TIMEOUT, _RESERVE, _CLOCK
    _LANES.clear()
    _listeners.clear()
    _LIMIT = 0
    _QUEUE_TIMEOUT = 300.0
    _RESERVE = 1
    _CLOCK = time.monotonic
