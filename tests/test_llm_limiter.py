"""The pure in-flight limiter (ADR 0115 C2, #3760).

Everything runs on a single event loop with an injected clock, so aging, the reserve, the
snapshot and the queue timeout are deterministic. The only test that leans on real time is
the queue-timeout one, which uses a tiny ``queue_timeout`` and lets ``asyncio.wait_for``
fire for real (the production deadline).
"""

from __future__ import annotations

import asyncio

import pytest

from graph import llm_limiter
from graph.llm_limiter import (
    BULK,
    DEFAULT,
    INTERACTIVE,
    GatewayQueueTimeout,
    LaneEvent,
    _Lane,
    _percentile,
)


class FakeClock:
    """A controllable monotonic clock."""

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


def _lane(clock, *, limit=1, queue_timeout=1000.0, reserve=1) -> _Lane:
    return _Lane(
        "gw|model",
        limit=limit,
        queue_timeout=queue_timeout,
        interactive_reserve=reserve,
        clock=clock,
    )


async def _spawn(lane, priority, acquired: asyncio.Event, release: asyncio.Event, *, arrival=None):
    """A worker that holds a slot: sets ``acquired`` once granted, exits on ``release``."""

    async def _run():
        async with lane.acquire(priority, arrival=arrival):
            acquired.set()
            await release.wait()

    return asyncio.create_task(_run())


async def _yield(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ── r1: async context manager + limit=0 pass-through ─────────────────────────────
async def test_acquire_is_async_context_manager_and_limit_zero_passthrough():
    # Module default is disabled: pass-through, no lane created, snapshot reports off.
    entered = False
    async with llm_limiter.acquire("gw|m", DEFAULT):
        entered = True
    assert entered
    snap = llm_limiter.snapshot()
    assert snap["enabled"] is False
    assert snap["lanes"] == []

    # Even "concurrent" acquires never wait when disabled.
    async with llm_limiter.acquire("gw|m"), llm_limiter.acquire("gw|m"):
        pass
    assert llm_limiter._LANES == {}


async def test_positional_priority_and_slot_release():
    clock = FakeClock()
    lane = _lane(clock, limit=1)
    acq1, rel1 = asyncio.Event(), asyncio.Event()
    t1 = await _spawn(lane, DEFAULT, acq1, rel1)
    await acq1.wait()
    assert lane._inflight == 1

    # Second caller must wait for the one slot.
    acq2, rel2 = asyncio.Event(), asyncio.Event()
    t2 = await _spawn(lane, DEFAULT, acq2, rel2)
    await _yield()
    assert not acq2.is_set()
    assert lane._queue

    rel1.set()
    await acq2.wait()
    assert lane._inflight == 1
    rel2.set()
    await asyncio.gather(t1, t2)
    assert lane._inflight == 0


# ── r2: class order, FIFO, and aging (injected clock) ────────────────────────────
async def test_class_order_then_fifo():
    clock = FakeClock()
    lane = _lane(clock, limit=1, reserve=0)
    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()

    # Enqueue bulk first, then default: class must beat arrival order.
    b_acq, b_rel = asyncio.Event(), asyncio.Event()
    tb = await _spawn(lane, BULK, b_acq, b_rel)
    await _yield()
    d1_acq, d1_rel = asyncio.Event(), asyncio.Event()
    td1 = await _spawn(lane, DEFAULT, d1_acq, d1_rel)
    await _yield()
    d2_acq, d2_rel = asyncio.Event(), asyncio.Event()
    td2 = await _spawn(lane, DEFAULT, d2_acq, d2_rel)
    await _yield()

    hold_rel.set()
    await d1_acq.wait()  # default d1 (earlier of the two defaults) served before bulk
    assert not b_acq.is_set()
    assert not d2_acq.is_set()

    d1_rel.set()
    await d2_acq.wait()  # FIFO within default: d2 before bulk
    assert not b_acq.is_set()

    d2_rel.set()
    await b_acq.wait()  # bulk last

    b_rel.set()
    await asyncio.gather(holder, tb, td1, td2)


async def test_aging_promotes_one_step_per_60s():
    # A bulk waiter that has aged one step ties a fresh default and wins on FIFO. The flip
    # happens exactly at 60s, so releasing at t=59 serves the default and at t=60 the bulk.
    for release_at, expect_bulk in ((59.0, False), (60.0, True)):
        llm_limiter._reset_for_tests()
        clock = FakeClock(0.0)
        lane = _lane(clock, limit=1, reserve=0)

        hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
        holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
        await hold_acq.wait()

        b_acq, b_rel = asyncio.Event(), asyncio.Event()
        tb = await _spawn(lane, BULK, b_acq, b_rel)  # bulk arrives at t=0
        await _yield()
        clock.advance(1.0)  # default arrives one second later
        d_acq, d_rel = asyncio.Event(), asyncio.Event()
        td = await _spawn(lane, DEFAULT, d_acq, d_rel)
        await _yield()

        clock.t = release_at
        hold_rel.set()
        await _yield()

        if expect_bulk:
            assert b_acq.is_set() and not d_acq.is_set()
        else:
            assert d_acq.is_set() and not b_acq.is_set()

        # drain
        b_rel.set()
        d_rel.set()
        await asyncio.gather(holder, tb, td)


async def test_arrival_preserves_aging_from_logical_start():
    # A reconnect re-acquires with the original arrival, so a bulk waiter carried from the
    # start of the call can outrank a fresh default in the selection.
    clock = FakeClock(130.0)
    lane = _lane(clock, limit=1)
    loop = asyncio.get_running_loop()
    aged_bulk = llm_limiter._Waiter(BULK, wait_start=130.0, arrival=0.0, seq=1, future=loop.create_future())
    fresh_default = llm_limiter._Waiter(
        DEFAULT, wait_start=130.0, arrival=130.0, seq=2, future=loop.create_future()
    )
    lane._queue = [aged_bulk, fresh_default]
    assert lane._effective_rank(aged_bulk, 130.0) == _rank_interactive() == 2
    assert lane._select(130.0) is aged_bulk
    for w in (aged_bulk, fresh_default):
        w.future.cancel()


def _rank_interactive() -> int:
    from graph.llm_limiter import _RANK

    return _RANK[INTERACTIVE]


# ── r3: reserve goes only to interactive; clamped to limit-1 ──────────────────────
async def test_reserve_slot_only_for_interactive():
    clock = FakeClock()
    lane = _lane(clock, limit=2, reserve=1)  # 1 reserved for interactive
    # Hold one slot with a default caller: 1 slot free but it is the reserved one.
    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()
    assert lane._inflight == 1 and lane._non_interactive == 1

    b_acq, b_rel = asyncio.Event(), asyncio.Event()
    tb = await _spawn(lane, BULK, b_acq, b_rel)
    await _yield()
    i_acq, i_rel = asyncio.Event(), asyncio.Event()
    ti = await _spawn(lane, INTERACTIVE, i_acq, i_rel)
    await _yield()

    # The reserved free slot goes to interactive; bulk keeps waiting.
    await i_acq.wait()
    assert not b_acq.is_set()
    assert lane._inflight == 2

    # Freeing the default slot lets the (non-interactive) bulk caller in.
    hold_rel.set()
    await b_acq.wait()

    i_rel.set()
    b_rel.set()
    await asyncio.gather(holder, tb, ti)


async def test_reserve_clamped_to_limit_minus_one():
    clock = FakeClock()
    # reserve far above capacity clamps to limit-1.
    lane = _lane(clock, limit=2, reserve=99)
    assert lane.snapshot(0.0)["reserve"] == 1

    # With limit 1, the reserve clamps to 0, so non-interactive work still gets the slot.
    lane1 = _lane(clock, limit=1, reserve=5)
    assert lane1.snapshot(0.0)["reserve"] == 0
    acq, rel = asyncio.Event(), asyncio.Event()
    t = await _spawn(lane1, BULK, acq, rel)
    await acq.wait()  # bulk got the only slot despite reserve=5
    rel.set()
    await t


# ── r4: queue timeout ─────────────────────────────────────────────────────────────
async def test_queue_timeout_raises_named_gateway_queue_timeout():
    lane = _Lane(
        "https://gw/v1|protolabs/smart",
        limit=1,
        queue_timeout=0.05,
        interactive_reserve=0,
        clock=__import__("time").monotonic,
    )
    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()

    with pytest.raises(GatewayQueueTimeout) as exc:
        async with lane.acquire(DEFAULT):
            pass

    assert isinstance(exc.value, TimeoutError)
    msg = str(exc.value)
    assert "https://gw/v1|protolabs/smart" in msg
    assert "position" in msg
    assert "model.max_inflight" in msg
    assert "model.inflight_queue_timeout" in msg
    assert exc.value.position >= 1
    assert exc.value.waited_s > 0

    # No residue: only the holder's slot remains, nothing queued.
    assert lane._queue == []
    assert lane._inflight == 1
    assert lane.snapshot()["queue_timeouts_5m"] == 1

    hold_rel.set()
    await holder
    assert lane._inflight == 0


# ── r5: cancelled / erroring waiter leaves no residue ─────────────────────────────
async def test_cancelled_waiter_leaves_no_residue():
    clock = FakeClock()
    lane = _lane(clock, limit=1)
    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()

    async def _waiter():
        async with lane.acquire(DEFAULT):
            pass

    task = asyncio.create_task(_waiter())
    await _yield()
    assert lane._queue  # it is queued behind the holder

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Nothing leaked: queue empty, the holder still holds its slot.
    assert lane._queue == []
    assert lane._inflight == 1

    # A fresh caller is served the moment the holder releases — the slot was not lost.
    hold_rel.set()
    await holder
    async with lane.acquire(DEFAULT):
        assert lane._inflight == 1
    assert lane._inflight == 0


async def test_exception_in_body_releases_slot():
    clock = FakeClock()
    lane = _lane(clock, limit=1)
    with pytest.raises(ValueError):
        async with lane.acquire(DEFAULT):
            assert lane._inflight == 1
            raise ValueError("boom")
    assert lane._inflight == 0
    assert lane._queue == []


# ── r6: snapshot shape, percentiles, saturated, contextvar ────────────────────────
async def test_snapshot_shape_and_saturated():
    clock = FakeClock(0.0)
    lane = _lane(clock, limit=2, reserve=1)
    holds = []
    for _ in range(2):  # fill both slots with interactive so nothing is reserved-blocked
        acq, rel = asyncio.Event(), asyncio.Event()
        holds.append((await _spawn(lane, INTERACTIVE, acq, rel), acq, rel))
        await acq.wait()

    # Queue a mix of classes.
    waiters = []
    for cls in (DEFAULT, BULK, BULK):
        acq, rel = asyncio.Event(), asyncio.Event()
        waiters.append((await _spawn(lane, cls, acq, rel), acq, rel))
        await _yield()

    snap = lane.snapshot(10.0)
    assert set(snap) == {
        "lane",
        "limit",
        "reserve",
        "inflight",
        "queued",
        "queued_by_priority",
        "oldest_wait_s",
        "wait_p50_s_5m",
        "wait_p90_s_5m",
        "queue_timeouts_5m",
        "saturated",
    }
    assert snap["limit"] == 2
    assert snap["reserve"] == 1
    assert snap["inflight"] == 2
    assert snap["queued"] == 3
    assert snap["queued_by_priority"] == {"interactive": 0, "default": 1, "bulk": 2}
    assert snap["oldest_wait_s"] == 10.0
    assert snap["saturated"] is False  # only 10s elapsed

    # After 60s of a continuously non-empty queue, the lane reports saturated.
    assert lane.snapshot(60.0)["saturated"] is True

    for task, _, rel in holds:
        rel.set()
    for task, _, rel in waiters:
        rel.set()
    await asyncio.gather(*[t for t, _, _ in holds], *[t for t, _, _ in waiters])


async def test_snapshot_records_wait_percentiles():
    clock = FakeClock(0.0)
    lane = _lane(clock, limit=1)
    # Grant a waiter after a known queue wait so it lands in the 5-minute window.
    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()
    w_acq, w_rel = asyncio.Event(), asyncio.Event()
    tw = await _spawn(lane, DEFAULT, w_acq, w_rel)
    await _yield()
    clock.advance(30.0)
    hold_rel.set()
    await w_acq.wait()  # granted after waiting 30s
    # Two grants land in the window: the holder waited 0s (immediate), the waiter 30s.
    snap = lane.snapshot(30.0)
    assert snap["wait_p50_s_5m"] == 15.0
    assert snap["wait_p90_s_5m"] == 27.0
    w_rel.set()
    await asyncio.gather(holder, tw)


def test_percentile_helper():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([5.0], 0.9) == 5.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert _percentile([0.0, 10.0, 20.0, 30.0, 40.0], 0.9) == 36.0


def test_priority_contextvar():
    assert llm_limiter.get_priority() == DEFAULT
    token = llm_limiter.set_priority(INTERACTIVE)
    assert llm_limiter.get_priority() == INTERACTIVE
    llm_limiter.reset_priority(token)
    assert llm_limiter.get_priority() == DEFAULT

    with llm_limiter.priority_scope(BULK):
        assert llm_limiter.get_priority() == BULK
    assert llm_limiter.get_priority() == DEFAULT

    with pytest.raises(ValueError):
        llm_limiter.set_priority("nope")


async def test_acquire_uses_contextvar_priority():
    clock = FakeClock()
    llm_limiter.configure(limit=1, interactive_reserve=0, clock=clock)
    with llm_limiter.priority_scope(BULK):
        async with llm_limiter.acquire("gw|m"):
            lane = llm_limiter._LANES["gw|m"]
            assert lane._queue == []  # granted, and it was tagged bulk (non-interactive)
            assert lane._non_interactive == 1
    assert llm_limiter._LANES["gw|m"]._inflight == 0


async def test_reconfigure_to_zero_admits_queued_waiters():
    """Switching the limiter off at runtime (configure(limit=0) — the documented return to
    the unlimited behaviour) must release everyone already queued. Otherwise new callers
    skip the limiter (module-level acquire short-circuits at _LIMIT == 0) while the queued
    ones sit out the whole queue_timeout and fail GatewayQueueTimeout."""
    clock = FakeClock()
    llm_limiter.configure(limit=1, interactive_reserve=0, queue_timeout=1000.0, clock=clock)
    lane = llm_limiter._get_or_create_lane("gw|m")

    hold_acq, hold_rel = asyncio.Event(), asyncio.Event()
    holder = await _spawn(lane, DEFAULT, hold_acq, hold_rel)
    await hold_acq.wait()

    # Two callers queue behind the single slot.
    w1_acq, w1_rel = asyncio.Event(), asyncio.Event()
    w2_acq, w2_rel = asyncio.Event(), asyncio.Event()
    t1 = await _spawn(lane, DEFAULT, w1_acq, w1_rel)
    t2 = await _spawn(lane, BULK, w2_acq, w2_rel)
    await _yield()
    assert lane._queue and not w1_acq.is_set() and not w2_acq.is_set()

    # Turn the limiter off. The queued waiters must be admitted at once, not stranded.
    llm_limiter.configure(limit=0)
    await _yield()
    assert w1_acq.is_set()
    assert w2_acq.is_set()
    assert lane._queue == []

    hold_rel.set()
    w1_rel.set()
    w2_rel.set()
    await asyncio.gather(holder, t1, t2)
    assert lane._inflight == 0


async def test_snapshot_enabled_reflects_config():
    clock = FakeClock()
    llm_limiter.configure(limit=0, clock=clock)
    assert llm_limiter.snapshot()["enabled"] is False
    llm_limiter.configure(limit=4)
    snap = llm_limiter.snapshot()
    assert snap["enabled"] is True
    assert isinstance(snap["generated_at"], str)


# ── metrics hook ──────────────────────────────────────────────────────────────────
async def test_listener_receives_enqueue_acquire_and_release():
    clock = FakeClock()
    llm_limiter.configure(limit=1, clock=clock)
    events: list[LaneEvent] = []
    llm_limiter.add_listener(events.append)
    try:
        async with llm_limiter.acquire("gw|m", INTERACTIVE):
            pass
    finally:
        llm_limiter.remove_listener(events.append)
    kinds = [e.kind for e in events]
    # The waiter is enqueued, immediately granted (a free slot), then released. The enqueue
    # event is what keeps the queue-depth gauge live while slots are busy (#3760 review).
    assert kinds == ["enqueued", "acquired", "released"]
    assert all(e.lane == "gw|m" and e.priority == INTERACTIVE for e in events)


async def test_listener_sees_dequeue_when_a_queued_waiter_is_cancelled():
    """A waiter that queues behind a full lane and is cancelled before its grant must emit a
    ``dequeued`` event, so a listener's depth gauge drains rather than counting the ghost
    forever (the #3760 review finding)."""
    clock = FakeClock()
    llm_limiter.configure(limit=1, queue_timeout=10_000.0, interactive_reserve=0, clock=clock)

    holder_acquired = asyncio.Event()
    holder_release = asyncio.Event()

    async def _holder():
        async with llm_limiter.acquire("gw|m", INTERACTIVE):
            holder_acquired.set()
            await holder_release.wait()

    holder = asyncio.create_task(_holder())
    await holder_acquired.wait()  # the holder owns the only slot

    events: list[LaneEvent] = []
    llm_limiter.add_listener(events.append)  # watch only the queued waiter's lifecycle

    async def _waiter():
        async with llm_limiter.acquire("gw|m", BULK):
            pass

    queued = asyncio.create_task(_waiter())
    try:
        while not any(e.kind == "enqueued" for e in events):
            await asyncio.sleep(0)  # let the waiter reach the queue behind the full slot
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
    finally:
        llm_limiter.remove_listener(events.append)
        holder_release.set()
        await holder

    kinds = [e.kind for e in events]
    assert kinds == ["enqueued", "dequeued"]  # never granted, then drained on cancel
    assert all(e.lane == "gw|m" and e.priority == BULK for e in events)


def test_bad_listener_never_breaks_dispatch():
    def boom(_event):
        raise RuntimeError("listener blew up")

    good: list[LaneEvent] = []
    llm_limiter.add_listener(boom)
    llm_limiter.add_listener(good.append)
    try:
        llm_limiter._dispatch(LaneEvent("acquired", "gw|m", DEFAULT, 0.0, 1, 0))
    finally:
        llm_limiter.remove_listener(boom)
        llm_limiter.remove_listener(good.append)
    assert len(good) == 1


# ── r7: no imports from graph.llm or the config ───────────────────────────────────
def test_no_forbidden_imports():
    import ast
    import pathlib

    src = pathlib.Path(llm_limiter.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    for mod in modules:
        assert mod != "graph.llm", "must not import graph.llm"
        assert mod != "graph.config" and not mod.startswith("graph.config."), "must not import the config"
