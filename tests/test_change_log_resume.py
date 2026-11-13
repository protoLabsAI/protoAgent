"""Durable change log, SSE seq ids, Last-Event-ID resume, resync_required (ADR 0130 S1)."""

from __future__ import annotations

import asyncio
import time

from events import ChangeLog, EventBus


def _bus_with_log(tmp_path, **log_kw):
    clog = ChangeLog(tmp_path / "change-log.db", **log_kw)
    bus = EventBus()
    bus.attach_change_log(clog)
    return bus, clog


# --- the log append (D2) ---


def test_retained_publish_appends_to_the_log_in_seq_order(tmp_path):
    """Every retained publish lands as one (seq, kind, entity_id, ts) row, in seq order."""
    bus, clog = _bus_with_log(tmp_path)
    bus.publish("chat.resumed", {"session_id": "s-1"})
    bus.publish("task.updated", {"id": "task-9"})
    bus.publish("background.completed", {"job_id": "bg-2"})
    rows = clog.read_since(0)
    assert [(r["seq"], r["kind"], r["entity_id"]) for r in rows] == [
        (1, "chat.resumed", "s-1"),
        (2, "task.updated", "task-9"),
        (3, "background.completed", "bg-2"),
    ]
    assert all(isinstance(r["ts"], float) for r in rows)


def test_unretained_progress_frames_stay_live_only(tmp_path):
    """retain=False (per-frame progress) still fans out live but never reaches the log —
    and its seq still advances, so logged rows keep their global numbering."""

    async def run():
        bus, clog = _bus_with_log(tmp_path)
        sub = bus.subscribe()
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        bus.publish("chat.progress", {"i": 1}, retain=False)
        live = await asyncio.wait_for(t, timeout=1)
        bus.publish("chat.completed", {"session_id": "s-1"})
        await sub.aclose()
        return live, clog.read_since(0)

    live, rows = asyncio.run(run())
    assert live["event"] == "chat.progress"  # delivered live
    assert [(r["seq"], r["kind"]) for r in rows] == [(2, "chat.completed")]  # seq 1 was the frame


# --- Last-Event-ID resume, including across a restart (D2) ---


def test_resume_after_restart_replays_every_retained_change_in_order(tmp_path):
    """A new process on the same log continues the seq and serves a client's old cursor:
    rows past the new ring come back as identity-only frames, in-ring rows keep their
    payloads, and the live stream follows with no duplicates."""
    bus1, clog1 = _bus_with_log(tmp_path)
    for i in range(1, 6):
        bus1.publish("task.updated", {"id": f"task-{i}"})
    clog1.close()  # server stops

    async def run():
        bus2 = EventBus()
        bus2.attach_change_log(ChangeLog(tmp_path / "change-log.db"))
        bus2.publish("task.updated", {"id": "task-6"})  # continues at seq 6, not 1
        bus2.publish("chat.completed", {"session_id": "s-9", "n": 7})
        sub = bus2.subscribe(since=2)
        replayed = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(5)]
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        bus2.publish("chat.resumed", {"session_id": "s-9"})  # live, after the replay
        live = await asyncio.wait_for(t, timeout=1)
        await sub.aclose()
        return replayed, live

    replayed, live = asyncio.run(run())
    assert [p["seq"] for p in replayed] == [3, 4, 5, 6, 7]
    # Seqs 3–5 predate the restart: the new ring never saw them → identity-only (D2).
    assert replayed[0] == {
        "event": "task.updated",
        "data": {"entity_id": "task-3"},
        "seq": 3,
        "ts": replayed[0]["ts"],
    }
    # Seqs 6–7 are in the live ring → replayed verbatim, payload intact.
    assert replayed[4]["data"] == {"session_id": "s-9", "n": 7}
    assert live["seq"] == 8 and live["event"] == "chat.resumed"


def test_bare_bus_without_log_keeps_ring_replay(tmp_path):
    """No log attached (unit tests, CLI): ?since= replay still comes from the ring."""

    async def run():
        bus = EventBus()
        bus.publish("a.1", {"i": 1})
        bus.publish("a.2", {"i": 2})
        sub = bus.subscribe(since=1)
        replayed = await asyncio.wait_for(sub.__anext__(), timeout=1)
        await sub.aclose()
        return replayed

    assert asyncio.run(run())["seq"] == 2


# --- resync_required (D2 gap handling) ---


def test_cursor_older_than_retention_gets_resync_required(tmp_path):
    """A cursor the sweep has passed can't be fully replayed: the client gets one
    resync_required event (then the live stream), never a silent gap."""

    async def run():
        bus, clog = _bus_with_log(tmp_path, max_rows=2, sweep_every=10_000)
        for i in range(1, 7):
            bus.publish("task.updated", {"id": f"task-{i}"})
        clog.sweep()  # keeps seqs 5–6, floor rises to 4
        sub = bus.subscribe(since=1)
        first = await asyncio.wait_for(sub.__anext__(), timeout=1)
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        bus.publish("chat.completed", {"session_id": "s-1"})
        live = await asyncio.wait_for(t, timeout=1)
        await sub.aclose()
        return first, live

    first, live = asyncio.run(run())
    assert first["event"] == "resync_required"
    assert first["data"] == {"since": 1, "floor": 4, "last_seq": 6}
    # The frame's own id is the current seq, so the client's next reconnect resumes
    # from here instead of re-triggering the resync forever.
    assert first["seq"] == 6
    assert live["seq"] == 7  # resync does NOT replay; live events follow as normal


def test_cursor_ahead_of_the_log_gets_resync_required(tmp_path):
    """A cursor past everything this instance has seen (wiped or foreign log) is a
    different epoch — resync, don't silently replay nothing."""

    async def run():
        bus, _ = _bus_with_log(tmp_path)
        bus.publish("task.updated", {"id": "task-1"})
        sub = bus.subscribe(since=999)
        first = await asyncio.wait_for(sub.__anext__(), timeout=1)
        await sub.aclose()
        return first

    first = asyncio.run(run())
    assert first["event"] == "resync_required" and first["data"]["since"] == 999


# --- the retention sweep (7d / 100k) ---


def test_sweep_drops_rows_past_the_age_window(tmp_path):
    now = time.time()
    clog = ChangeLog(tmp_path / "log.db")
    for seq in (1, 2, 3):
        clog.append(seq, "old.kind", "e", ts=now - 8 * 86400)
    for seq in (4, 5, 6):
        clog.append(seq, "new.kind", "e", ts=now)
    assert clog.sweep(now=now) == 3
    assert [r["seq"] for r in clog.read_since(0)] == [4, 5, 6]
    assert clog.floor_seq() == 3
    assert clog.covers(3) and not clog.covers(2)


def test_sweep_caps_row_count_keeping_the_newest(tmp_path):
    clog = ChangeLog(tmp_path / "log.db", max_rows=2, sweep_every=10_000)
    for seq in range(1, 6):
        clog.append(seq, "k", "e")
    assert clog.sweep() == 3
    assert [r["seq"] for r in clog.read_since(0)] == [4, 5]
    assert clog.floor_seq() == 3


def test_append_sweeps_opportunistically(tmp_path):
    """Retention needs no dedicated timer: every Nth append sweeps."""
    clog = ChangeLog(tmp_path / "log.db", max_rows=2, sweep_every=3)
    for seq in (1, 2, 3):
        clog.append(seq, "k", "e")
    assert [r["seq"] for r in clog.read_since(0)] == [2, 3]
    assert clog.floor_seq() == 1


def test_floor_and_high_water_survive_a_restart(tmp_path):
    """The sweep floor and seq high-water persist: a reopened instance still detects
    pre-retention cursors, and the bus never re-mints swept seqs."""
    clog = ChangeLog(tmp_path / "log.db")
    for seq in (1, 2, 3):
        clog.append(seq, "k", "e")
    clog.sweep(now=time.time() + 30 * 86400)  # everything ages out
    clog.close()

    reopened = ChangeLog(tmp_path / "log.db")
    assert reopened.floor_seq() == 3
    assert reopened.last_seq() == 3  # from the floor — the table itself is empty
    bus = EventBus()
    bus.attach_change_log(reopened)
    seen: list[dict] = []
    bus.subscribe_handler("#", seen.append)
    bus.publish("k.new", {"id": "e"})
    assert seen[0]["seq"] == 4  # continues past the swept high-water


# --- the SSE surface (/api/events) ---


def test_sse_resume_frames_carry_log_seq_ids_in_order(tmp_path):
    """The resumed stream frames each replayed change with id: <seq> (ADR 0130 S1)."""
    from operator_api.routes import _sse_event_stream

    async def run():
        bus, _ = _bus_with_log(tmp_path)
        bus.publish("task.updated", {"id": "task-1"})
        bus.publish("task.updated", {"id": "task-2"})
        bus.publish("task.updated", {"id": "task-3"})
        gen = _sse_event_stream(bus.subscribe, since=1, keepalive_s=5)
        assert await gen.__anext__() == ": connected\n\n"
        frames = [await asyncio.wait_for(gen.__anext__(), timeout=1) for _ in range(2)]
        await gen.aclose()
        return frames

    frames = asyncio.run(run())
    assert frames[0].startswith("id: 2\n") and frames[1].startswith("id: 3\n")


def test_last_event_id_header_resumes_the_event_route(tmp_path):
    """GET /api/events maps the SSE Last-Event-ID header to the subscription cursor."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.routes import register_operator_routes

    captured: dict = {}

    async def fake_subscribe(since=None):
        captured["since"] = since
        return
        yield  # pragma: no cover — makes this an async generator

    async def _unused(body):  # pragma: no cover — required registrar args
        return ""

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        events_subscribe=fake_subscribe,
    )
    r = TestClient(app).get("/api/events", headers={"Last-Event-ID": "42"})
    assert r.status_code == 200 and r.text.startswith(": connected")
    assert captured["since"] == 42
