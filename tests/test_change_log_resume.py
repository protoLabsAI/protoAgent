"""Durable change log, SSE seq ids, Last-Event-ID resume, resync_required (ADR 0130 S1)."""

from __future__ import annotations

import asyncio
import time

from events import ChangeLog, EventBus
from events.change_log import LIVE_SEQ_PERSIST_EVERY as SLACK  # the restart reseed slack
from events.change_log import MAX_PAYLOAD_BYTES


def _bus_with_log(tmp_path, **log_kw):
    clog = ChangeLog(tmp_path / "change-log.db", **log_kw)
    bus = EventBus()
    bus.attach_change_log(clog)
    return bus, clog


# --- the log append (D2) ---


def test_retained_publish_appends_to_the_log_in_seq_order(tmp_path):
    """Every retained publish lands as one (seq, kind, entity_id, ts) row, in seq
    order — plus the payload key the identity arrived under and the payload itself,
    verbatim, so a replay is frame-identical to the original publish."""
    bus, clog = _bus_with_log(tmp_path)
    bus.publish("chat.resumed", {"session_id": "s-1"})
    bus.publish("task.updated", {"id": "task-9"})
    bus.publish("background.completed", {"job_id": "bg-2"})
    bus.publish("turn.input_required", {"task_id": "t-4", "context_id": "ctx-7", "prompt": "pick one"})
    rows = clog.read_since(0)
    assert [(r["seq"], r["kind"], r["entity_id"], r["entity_key"]) for r in rows] == [
        (1, "chat.resumed", "s-1", "session_id"),
        (2, "task.updated", "task-9", "id"),
        (3, "background.completed", "bg-2", "job_id"),
        (4, "turn.input_required", "ctx-7", "context_id"),
    ]
    assert rows[0]["data"] == {"session_id": "s-1"}
    assert rows[3]["data"] == {"task_id": "t-4", "context_id": "ctx-7", "prompt": "pick one"}
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
    """A new process on the same log continues the seq (past a restart-slack window, so
    even unretained frames' seqs are never re-minted) and serves a client's old cursor:
    rows the new ring never saw come back with their stored payloads, frame-identical
    to the original publish, and the live stream follows with no duplicates."""
    bus1, clog1 = _bus_with_log(tmp_path)
    for i in range(1, 6):
        bus1.publish("task.updated", {"id": f"task-{i}"})
    clog1.close()  # server stops

    s = 5 + SLACK  # the new run reseeds past the old high-water plus the slack window

    async def run():
        bus2 = EventBus()
        bus2.attach_change_log(ChangeLog(tmp_path / "change-log.db"))
        bus2.publish("task.updated", {"id": "task-6"})  # continues past seq 5, never back at 1
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
    assert [p["seq"] for p in replayed] == [3, 4, 5, s + 1, s + 2]
    # Seqs 3–5 predate the restart: the new ring never saw them, so they come back
    # from the log — with the payload the original publish carried, not an
    # identity-only husk (the deck/console branch on payload fields on replay).
    assert replayed[0] == {
        "event": "task.updated",
        "data": {"id": "task-3"},
        "seq": 3,
        "ts": replayed[0]["ts"],
    }
    # The two post-restart rows are in the live ring → replayed verbatim, payload intact.
    assert replayed[4]["data"] == {"session_id": "s-9", "n": 7}
    assert live["seq"] == s + 3 and live["event"] == "chat.resumed"


def test_replayed_turn_events_keep_the_fields_the_deck_branches_on(tmp_path):
    """The deck's feed (and the console) read payload fields off replayed frames:
    `turn.usage` sets last_cost_usd/last_model and unparks on a non-input-required
    `state`; `turn.finished` renders ✗ only when `ok` is False. A replay past the
    ring must carry those fields verbatim — an identity-only husk zeroed the costs,
    showed a failed turn as ✓, and unparked a session still waiting on the operator."""
    usage = {
        "task_id": "t-1",
        "context_id": "ctx-1",
        "cost_usd": 0.0421,
        "model": "m-9",
        "input_tokens": 10,
        "output_tokens": 3,
        "state": "input-required",
    }
    finished = {"session_id": "s-1", "task_id": "t-2", "ok": False, "origin": "scheduler"}
    bus1, clog1 = _bus_with_log(tmp_path)
    bus1.publish("turn.usage", usage)
    bus1.publish("turn.finished", finished)
    clog1.close()  # restart: the new ring never holds these rows

    async def run():
        bus2 = EventBus()
        bus2.attach_change_log(ChangeLog(tmp_path / "change-log.db"))
        sub = bus2.subscribe(since=0)
        replayed = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(2)]
        await sub.aclose()
        return replayed

    replayed = asyncio.run(run())
    assert [p["event"] for p in replayed] == ["turn.usage", "turn.finished"]
    assert replayed[0]["data"] == usage
    assert replayed[1]["data"] == finished


def test_unstorable_payload_falls_back_to_identity_only(tmp_path):
    """A payload json can't encode (live SSE skips those frames too) or one over the
    64 KiB cap still logs its row — identity-only, keyed the way the publish named
    its entity — so the change itself is never lost to a resume."""
    bus1, clog1 = _bus_with_log(tmp_path)
    bus1.publish("task.updated", {"id": "task-1", "blob": object()})  # unencodable
    bus1.publish("chat.resumed", {"session_id": "s-1", "text": "x" * MAX_PAYLOAD_BYTES})  # oversized
    assert [(r["kind"], r["entity_id"], r["data"]) for r in clog1.read_since(0)] == [
        ("task.updated", "task-1", None),
        ("chat.resumed", "s-1", None),
    ]
    clog1.close()  # restart, so the replay can't take the ring's verbatim copy

    async def run():
        bus2 = EventBus()
        bus2.attach_change_log(ChangeLog(tmp_path / "change-log.db"))
        sub = bus2.subscribe(since=0)
        replayed = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(2)]
        await sub.aclose()
        return replayed

    replayed = asyncio.run(run())
    assert replayed[0]["data"] == {"entity_id": "task-1", "id": "task-1"}
    assert replayed[1]["data"] == {"entity_id": "s-1", "session_id": "s-1"}


def test_append_failure_falls_back_to_the_ring(tmp_path):
    """A change-log write failure (full disk) is logged, never raised — and the event
    still reached the ring, so a reconnect replays it from there (in seq order)
    instead of silently skipping the gap row."""

    def boom(*a, **k):
        raise OSError("disk full")

    async def run():
        bus, clog = _bus_with_log(tmp_path)
        bus.publish("task.updated", {"id": "task-1"})
        orig = clog.append
        clog.append = boom
        bus.publish("task.updated", {"id": "task-2"})  # ring only — the log write failed
        clog.append = orig
        bus.publish("task.updated", {"id": "task-3"})
        sub = bus.subscribe(since=0)
        replayed = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(3)]
        await sub.aclose()
        return replayed

    replayed = asyncio.run(run())
    assert [(p["seq"], p["data"]["id"]) for p in replayed] == [(1, "task-1"), (2, "task-2"), (3, "task-3")]


def test_progress_frame_cursor_survives_a_restart(tmp_path):
    """SSE stamps id: on unretained frames too, so a client's Last-Event-ID can be a
    progress-frame seq the log has no row for. The restarted bus must reseed PAST those
    (the resume slack covers the unpersisted tail) — otherwise it re-mints seqs under
    the cursor and a later resume silently skips real changes."""
    bus1, clog1 = _bus_with_log(tmp_path)
    bus1.publish("task.updated", {"id": "task-1"})  # seq 1 — the only logged row
    for i in range(3):  # seqs 2–4: stamped on live frames, never logged
        bus1.publish("chat.progress", {"i": i}, retain=False)
    clog1.close()  # restart, with a client cursor parked at 4

    async def run():
        bus2 = EventBus()
        bus2.attach_change_log(ChangeLog(tmp_path / "change-log.db"))
        bus2.publish("task.updated", {"id": "task-2"})
        sub = bus2.subscribe(since=4)
        first = await asyncio.wait_for(sub.__anext__(), timeout=1)
        await sub.aclose()
        return first

    first = asyncio.run(run())
    # The new change replays (its seq is above the cursor) — not skipped, no resync.
    assert first["event"] == "task.updated" and first["data"] == {"id": "task-2"}
    assert first["seq"] > 4


def test_live_high_water_is_persisted_past_the_throttle(tmp_path):
    """A pure-progress stretch longer than the persist window still lands its high-water
    in the meta row, so the restart slack never has more than one window to cover."""
    clog = ChangeLog(tmp_path / "log.db", live_persist_every=4)
    bus = EventBus()
    bus.attach_change_log(clog)
    bus.publish("task.updated", {"id": "t-1"})  # seq 1
    for i in range(9):  # seqs 2–10; the throttled meta writes land at 5 and 9
        bus.publish("chat.progress", {"i": i}, retain=False)
    clog.close()

    reopened = ChangeLog(tmp_path / "log.db", live_persist_every=4)
    assert reopened.last_seq() == 9  # the persisted live mark, not the lone row's 1
    bus2 = EventBus()
    bus2.attach_change_log(reopened)
    seen: list[dict] = []
    bus2.subscribe_handler("#", seen.append)
    bus2.publish("task.updated", {"id": "t-2"})
    assert seen[0]["seq"] > 10  # past every seq the old run stamped on a frame


def test_fresh_log_first_progress_frame_marks_the_log_used(tmp_path):
    """A run that only ever emitted progress frames persists its FIRST live seq eagerly,
    so the restart slack applies — while a log that never recorded anything issued no
    cursors and reseeds from 0 (test buses keep their 1-based seqs)."""
    clog = ChangeLog(tmp_path / "log.db", live_persist_every=100)
    bus = EventBus()
    bus.attach_change_log(clog)
    for i in range(3):  # seqs 1–3: no retained publish at all
        bus.publish("chat.progress", {"i": i}, retain=False)
    clog.close()

    reopened = ChangeLog(tmp_path / "log.db", live_persist_every=100)
    assert reopened.last_seq() == 1  # the eager first-frame mark
    assert reopened.resume_seq() >= 3  # the slack covers the unpersisted tail
    assert ChangeLog(tmp_path / "virgin.db").resume_seq() == 0  # never used → no slack


def test_bare_bus_stale_cursor_still_streams_live_events():
    """No log attached (unit tests, CLI; the server's fallback when the log fails at
    boot) and a cursor from a previous run — the bare counter restarts at 0, so the
    cursor is ahead of everything: nothing replays, but every live event still flows,
    exactly the pre-ADR-0130 ring-only contract. (Gating the live loop on the raw
    cursor starved such subscribers until the counter caught up.)"""

    async def run():
        bus = EventBus()
        bus.publish("a.1", {"i": 1})  # seqs 1–2: the ring holds nothing past 500
        bus.publish("a.2", {"i": 2})
        sub = bus.subscribe(since=500)
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        bus.publish("b.live", {"i": 3})
        live = await asyncio.wait_for(t, timeout=1)
        await sub.aclose()
        return live

    live = asyncio.run(run())
    assert live["event"] == "b.live" and live["seq"] == 3


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
    assert seen[0]["seq"] == 4 + SLACK  # past the swept high-water, plus the restart slack
