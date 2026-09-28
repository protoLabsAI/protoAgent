"""Gateway in-flight limiter observability (ADR 0115 C6 / D8, #3760).

Proves the three read surfaces over one *fake saturated lane* — its single slot held, five
waiters queued across all three priorities, the injected clock advanced past the 60 s
saturation dwell — line up:

- ``GET /api/telemetry/llm-lanes`` returns the D8 payload (or ``{"enabled": false}`` off),
  reading memory only,
- ``sdk.llm_lanes()`` returns the same in-process snapshot with no HTTP round-trip,
- the four Prometheus signals update from the limiter's listener hook, and are a silent
  no-op when prometheus-client is absent.

Everything runs on one event loop with an injected clock, so ``saturated`` and the queued
counts are deterministic; ``queue_timeout`` is set huge so no waiter times out for real
during the test's wall-clock.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from fastapi import FastAPI

from graph import llm_limiter
from observability import metrics
from operator_api.telemetry_routes import register_telemetry_routes

LANE = "https://gw/v1|protolabs/smart"


class FakeClock:
    """A controllable monotonic clock (mirrors tests/test_llm_limiter.py)."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _reset():
    llm_limiter._reset_for_tests()
    yield
    llm_limiter._reset_for_tests()


def _app() -> FastAPI:
    app = FastAPI()
    register_telemetry_routes(app)
    return app


async def _wait_until_queued(n: int, tries: int = 200) -> None:
    """Yield the loop until ``n`` waiters have reached the lane's queue."""
    for _ in range(tries):
        lane = llm_limiter._LANES.get(LANE)
        if lane is not None and len(lane._queue) >= n:
            return
        await asyncio.sleep(0)
    depth = len(llm_limiter._LANES[LANE]._queue) if LANE in llm_limiter._LANES else 0
    raise AssertionError(f"expected {n} queued waiters, saw {depth}")


@contextlib.asynccontextmanager
async def _saturated_lane(clock: FakeClock):
    """One lane at its limit (1) with five waiters queued as interactive/default/bulk.

    All acquisitions happen at ``clock == 0`` so ``_queue_nonempty_since`` is 0; the caller
    advances the clock past the 60 s dwell before snapshotting. Waiters block in ``acquire``
    (never granted while the holder holds the only slot); cleanup cancels every task, which
    drains the queue with no residue."""
    llm_limiter.configure(limit=1, queue_timeout=10_000.0, interactive_reserve=1, clock=clock)
    tasks: list[asyncio.Task] = []

    def _spawn(priority: str, ready: asyncio.Event) -> None:
        async def _run():
            async with llm_limiter.acquire(LANE, priority):
                ready.set()
                await asyncio.Event().wait()  # hold the slot until cancelled

        tasks.append(asyncio.create_task(_run()))

    holder_ready = asyncio.Event()
    _spawn("default", holder_ready)
    await holder_ready.wait()  # the holder now owns the single slot

    for priority in ("interactive", "default", "bulk", "bulk", "bulk"):
        _spawn(priority, asyncio.Event())
    await _wait_until_queued(5)

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_EXPECTED_LANE = {
    "lane": LANE,
    "limit": 1,
    "reserve": 0,  # interactive_reserve=1 clamps to limit-1 == 0
    "inflight": 1,
    "queued": 5,
    "queued_by_priority": {"interactive": 1, "default": 1, "bulk": 3},
    "oldest_wait_s": 120.0,
    "wait_p50_s_5m": 0.0,  # only the holder's grant is in the window, and it waited 0s
    "wait_p90_s_5m": 0.0,
    "queue_timeouts_5m": 0,
    "saturated": True,
}


# ── r2 + r3: route payload and sdk.llm_lanes() agree on the D8 shape ────────────────
async def test_route_and_sdk_report_the_saturated_lane():
    from graph import sdk

    clock = FakeClock()
    async with _saturated_lane(clock):
        clock.advance(120.0)  # past the 60 s saturation dwell

        sdk_snap = sdk.llm_lanes()  # in-process read, no HTTP

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/api/telemetry/llm-lanes")

        assert resp.status_code == 200
        route_snap = resp.json()

        for snap in (sdk_snap, route_snap):
            assert snap["enabled"] is True
            assert isinstance(snap["generated_at"], str)
            assert snap["lanes"] == [_EXPECTED_LANE]
        # Identical D8 shape on both surfaces (bar the per-call generated_at stamp).
        assert route_snap["lanes"] == sdk_snap["lanes"]


# ── r2 (off case): the route collapses to {"enabled": false} when the limiter is off ──
async def test_route_reports_disabled_when_limiter_off():
    from graph import sdk

    llm_limiter.configure(limit=0)  # the default — limiter disabled

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/telemetry/llm-lanes")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}

    # sdk.llm_lanes() returns the raw snapshot (enabled False, no lanes) — same source.
    snap = sdk.llm_lanes()
    assert snap["enabled"] is False
    assert snap["lanes"] == []
    assert isinstance(snap["generated_at"], str)


async def test_route_makes_no_network_call(monkeypatch):
    """The route reads the in-process snapshot only. Guard at the socket layer: opening any
    real socket fails the test, while the in-memory ASGITransport (which never touches a
    socket) serves the request — so a route that reached out to the DB or a peer would trip
    the guard rather than pass silently."""
    import socket

    def _no_socket(*_a, **_k):
        raise AssertionError("the llm-lanes route must not open a socket / make a network call")

    monkeypatch.setattr(socket, "socket", _no_socket)

    clock = FakeClock()
    async with _saturated_lane(clock):
        clock.advance(120.0)
        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/api/telemetry/llm-lanes")
        assert resp.status_code == 200
        assert resp.json()["lanes"][0]["saturated"] is True


# ── r1: the four D8 metrics update from the limiter, and no-op without prometheus ───
def _try_init() -> bool:
    try:
        metrics.init()
        return metrics.is_enabled()
    except Exception:  # noqa: BLE001 — already-registered or prometheus missing → skip
        return False


async def test_lane_metrics_update_from_the_limiter_snapshot():
    if not (metrics.is_enabled() or _try_init()):
        pytest.skip("prometheus-client not installed")
    from prometheus_client import REGISTRY

    p = metrics._prefix()
    clock = FakeClock()
    async with _saturated_lane(clock):
        clock.advance(120.0)

        waits_before = (
            REGISTRY.get_sample_value(
                f"{p}_llm_queue_wait_seconds_count", {"lane": LANE, "priority": "bulk"}
            )
            or 0.0
        )
        timeouts_before = REGISTRY.get_sample_value(f"{p}_llm_queue_timeouts_total", {"lane": LANE}) or 0.0

        # The event a real inflight_queue_timeout dispatches: it records the wait + the
        # timeout, and refreshes the gauges from the snapshot (which the event alone can't
        # carry per-priority).
        metrics.record_lane_event(llm_limiter.LaneEvent("timeout", LANE, "bulk", 90.0, 1, 5))

        # Gauges reflect the current snapshot exactly (set, not incremented).
        assert REGISTRY.get_sample_value(f"{p}_llm_inflight", {"lane": LANE}) == 1.0
        assert REGISTRY.get_sample_value(f"{p}_llm_queue_depth", {"lane": LANE, "priority": "bulk"}) == 3.0
        assert REGISTRY.get_sample_value(f"{p}_llm_queue_depth", {"lane": LANE, "priority": "default"}) == 1.0
        assert (
            REGISTRY.get_sample_value(f"{p}_llm_queue_depth", {"lane": LANE, "priority": "interactive"}) == 1.0
        )
        # Histogram + counter advanced by exactly one observation/increment.
        assert (
            REGISTRY.get_sample_value(f"{p}_llm_queue_wait_seconds_count", {"lane": LANE, "priority": "bulk"})
            == waits_before + 1.0
        )
        assert REGISTRY.get_sample_value(f"{p}_llm_queue_timeouts_total", {"lane": LANE}) == timeouts_before + 1.0


async def test_record_lane_event_works_as_a_live_limiter_listener():
    """What ``init`` wires: register ``record_lane_event`` as a limiter listener and drive a
    real acquire/release; the inflight gauge tracks the slot without any explicit call."""
    if not (metrics.is_enabled() or _try_init()):
        pytest.skip("prometheus-client not installed")
    from prometheus_client import REGISTRY

    p = metrics._prefix()
    clock = FakeClock()
    llm_limiter.add_listener(metrics.record_lane_event)
    try:
        llm_limiter.configure(limit=2, interactive_reserve=0, clock=clock)
        async with llm_limiter.acquire(LANE, "interactive"):
            assert REGISTRY.get_sample_value(f"{p}_llm_inflight", {"lane": LANE}) == 1.0
        # Released — the gauge falls back to 0.
        assert REGISTRY.get_sample_value(f"{p}_llm_inflight", {"lane": LANE}) == 0.0
    finally:
        llm_limiter.remove_listener(metrics.record_lane_event)


async def test_queue_depth_gauge_tracks_enqueue_and_dequeue_live():
    """The #3760 review regression: with ``record_lane_event`` wired as a live listener, the
    queue-depth gauge must CLIMB as waiters join the queue behind full slots (the enqueue
    event) and DRAIN to 0 when queued waiters are cancelled before a grant (the dequeue
    event) — not stay pinned to its last acquire/release value while the backlog grows.
    Drives real acquires through the listener and never calls ``record_lane_event`` by hand."""
    if not (metrics.is_enabled() or _try_init()):
        pytest.skip("prometheus-client not installed")
    from prometheus_client import REGISTRY

    p = metrics._prefix()
    clock = FakeClock()
    llm_limiter.add_listener(metrics.record_lane_event)  # register BEFORE any acquire fires
    tasks: list[asyncio.Task] = []

    def depth(priority: str):
        return REGISTRY.get_sample_value(f"{p}_llm_queue_depth", {"lane": LANE, "priority": priority})

    def _spawn(priority: str, ready: asyncio.Event) -> None:
        async def _run():
            async with llm_limiter.acquire(LANE, priority):
                ready.set()
                await asyncio.Event().wait()  # hold until cancelled

        tasks.append(asyncio.create_task(_run()))

    try:
        llm_limiter.configure(limit=1, queue_timeout=10_000.0, interactive_reserve=0, clock=clock)

        holder_ready = asyncio.Event()
        _spawn("default", holder_ready)
        await holder_ready.wait()  # the holder's grant refreshes every depth gauge from the snapshot
        assert depth("bulk") == 0.0  # nothing queued yet — a clean baseline regardless of prior tests

        # Two bulk waiters JOIN the queue behind the full slot. The gauge must climb off the
        # enqueue events alone: no acquire/release/timeout has fired since the holder's grant.
        for _ in range(2):
            _spawn("bulk", asyncio.Event())
        await _wait_until_queued(2)
        assert depth("bulk") == 2.0

        # Cancel the two queued (never-granted) waiters: the dequeue events must drain the
        # gauge back to 0 rather than leaving the ghost backlog counted.
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        assert depth("bulk") == 0.0
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        llm_limiter.remove_listener(metrics.record_lane_event)


def test_lane_metrics_are_a_noop_without_prometheus(monkeypatch):
    """Reproduce the prometheus-absent state (``_enabled`` False, handles None — exactly
    what ``init`` leaves when the import fails): the call must not raise and must not even
    read the limiter snapshot."""
    monkeypatch.setattr(metrics, "_enabled", False)
    monkeypatch.setattr(metrics, "_llm_inflight", None)

    def _boom():
        raise AssertionError("snapshot must not be read when metrics are disabled")

    monkeypatch.setattr(llm_limiter, "snapshot", _boom)

    # No raise, no snapshot read.
    metrics.record_lane_event(llm_limiter.LaneEvent("timeout", LANE, "bulk", 5.0, 1, 3))
    metrics.record_lane_event(llm_limiter.LaneEvent("acquired", LANE, "interactive", 0.0, 1, 0))
