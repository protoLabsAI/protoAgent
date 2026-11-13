"""Server-held session state + ``session.changed`` (ADR 0130 S2, D1).

The server derives ``{live_task_id, status, last_change_seq, updated_at}`` from the
task store and the turn lock, exposes it on the sessions API, and publishes ONE
``session.changed`` per transition (turn start/end, pause, steer consumed, delete),
which the S1 change log records so ``last_change_seq`` survives a restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.session_state as ss
from events import EventBus

AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    ss._reset_for_tests()
    yield
    ss._reset_for_tests()


@pytest.fixture
def state_bus(monkeypatch):
    """A private bus wired into session_state; yields (bus, the session.changed frames)."""
    bus = EventBus()
    events: list[dict] = []
    bus.subscribe_handler(ss.TOPIC, lambda p: events.append(p))
    monkeypatch.setattr(ss, "_bus", lambda: bus)
    return bus, events


def _engine(tmp_path, name, rows):
    """A real SQLite A2A task store holding ``rows`` ({id, context_id, state})."""
    from a2a.server.tasks.database_task_store import Base, TaskModel
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}.db")

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for row in rows:
                await conn.execute(
                    TaskModel.__table__.insert(),
                    {
                        "id": row["id"],
                        "context_id": row["context_id"],
                        "kind": "task",
                        "status": {"state": row["state"]},
                        "artifacts": [],
                        "history": [],
                        "last_updated": row.get("at", AT),
                    },
                )

    asyncio.run(_seed())
    return engine


def _transitions(events):
    return [(e["data"]["status"], e["data"]["reason"]) for e in events]


def test_derive_status_matches_turn_lock_and_store():
    # Running wins (the turn lock is ground truth), holder preferred, else the newest
    # non-terminal task; parked newest ⇒ paused; a working row with NO live turn is a
    # restart-interrupted turn and reads idle, never a phantom running.
    assert ss.derive_status(active=True, holder="t9", newest_state="working", newest_task_id="t1") == ("running", "t9")
    assert ss.derive_status(active=True, holder=None, newest_state="working", newest_task_id="t1") == ("running", "t1")
    assert ss.derive_status(active=True, holder=None, newest_state="completed", newest_task_id="t1") == ("running", "")
    assert ss.derive_status(
        active=False, holder=None, newest_state="TASK_STATE_INPUT_REQUIRED", newest_task_id="t2"
    ) == ("paused", "t2")
    assert ss.derive_status(active=False, holder=None, newest_state="auth-required", newest_task_id="t2") == (
        "paused",
        "t2",
    )
    assert ss.derive_status(active=False, holder=None, newest_state="TASK_STATE_WORKING", newest_task_id="t3") == (
        "idle",
        "",
    )
    assert ss.derive_status(active=False, holder=None, newest_state="completed", newest_task_id="t1") == ("idle", "")
    assert ss.derive_status(active=False, holder=None, newest_state=None, newest_task_id=None) == ("idle", "")


def test_turn_bracket_publishes_once_per_transition(state_bus):
    """The idle-beacon bracket's edges: two overlapped drivers on one session are ONE
    running stretch — one `session.changed` in, one out (r2: once per transition)."""
    from server import turn_control

    _bus, events = state_bus
    turn_control._turn_started("chat-s2")
    turn_control._turn_started("chat-s2")  # queued second driver: no new transition
    turn_control._turn_ended("chat-s2")
    turn_control._turn_ended("chat-s2")
    assert _transitions(events) == [("running", "turn_started"), ("idle", "turn_ended")]
    assert events[0]["data"]["session_id"] == "chat-s2"
    assert events[1]["seq"] > events[0]["seq"]


def test_job_contexts_never_publish(state_bus):
    """Job-owned execution contexts (background:/job:) are not chat sessions."""
    from server import turn_control

    _bus, events = state_bus
    turn_control._turn_started("background:bg-1")
    turn_control._turn_ended("background:bg-1")
    ss.note_paused("job:abc", "t1")
    ss.note_turn_started("")
    assert events == []


def test_park_publishes_paused_once_then_end_settles_silent(state_bus, tmp_path, monkeypatch):
    """A HITL/decision park: the executor's ``input_required`` frame publishes paused
    ONCE; the bracket's turn-end settle confirms it against the task store instead of
    calling the session idle (r1 paused lifecycle, r2 once per transition)."""
    import runtime.state as rs
    import server.a2a as a2a
    from server import turn_control

    bus, events = state_bus
    engine = _engine(
        tmp_path, "park", [{"id": "t-park", "context_id": "chat-park", "state": "TASK_STATE_INPUT_REQUIRED"}]
    )
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    monkeypatch.setattr(a2a, "_event_bus", bus)

    async def drive():
        turn_control._turn_started("chat-park")
        a2a._a2a_progress("chat-park", "t-park", {"phase": "input_required", "prompt": "approve?"})
        turn_control._turn_ended("chat-park")  # schedules the settle on this loop
        for settle in list(ss._SETTLES):
            await settle

    asyncio.run(drive())
    assert _transitions(events) == [("running", "turn_started"), ("paused", "paused")]
    assert events[1]["data"]["live_task_id"] == "t-park"


def test_terminal_end_settles_idle_from_store(state_bus, tmp_path, monkeypatch):
    """A server-started turn (same bracket) whose row settled terminal derives idle."""
    import runtime.state as rs
    from server import turn_control

    _bus, events = state_bus
    engine = _engine(tmp_path, "done", [{"id": "t-done", "context_id": "chat-done", "state": "TASK_STATE_COMPLETED"}])
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)

    async def drive():
        turn_control._turn_started("chat-done")
        turn_control._turn_ended("chat-done")
        for settle in list(ss._SETTLES):
            await settle

    asyncio.run(drive())
    assert _transitions(events) == [("running", "turn_started"), ("idle", "turn_ended")]


def test_steer_consumed_always_publishes(state_bus, monkeypatch):
    """A consumed steer changes no status but every device must resettle its queued
    bubbles — the frame publishes for ANY origin, each boundary once (force)."""
    import server.a2a as a2a

    bus, events = state_bus
    monkeypatch.setattr(a2a, "_event_bus", bus)
    ss.note_turn_started("chat-steer")
    frame = {"phase": "steer_consumed", "items": [{"id": "m1", "text": "hi"}], "origin": ""}
    a2a._a2a_progress("chat-steer", "t-live", frame)
    a2a._a2a_progress("chat-steer", "t-live", frame)
    assert _transitions(events)[1:] == [("running", "steer_consumed"), ("running", "steer_consumed")]
    assert events[1]["data"]["live_task_id"] == "t-live"


def test_session_changed_lands_in_change_log_and_reseeds_after_restart(state_bus, tmp_path):
    """r2: every transition is one durable change-log row; after a 'restart' (fresh
    in-memory maps) ``last_change_seq`` reseeds from the log."""
    from events import ChangeLog

    bus, _events = state_bus
    clog = ChangeLog(tmp_path / "change-log.db")
    bus.attach_change_log(clog)
    ss.note_turn_started("chat-log")
    ss.note_paused("chat-log", "t1")
    rows = [r for r in clog.read_since(0) if r["kind"] == ss.TOPIC]
    assert [(r["entity_id"], r["entity_key"]) for r in rows] == [("chat-log", "session_id")] * 2
    assert rows[-1]["data"]["status"] == "paused"

    ss._reset_for_tests()  # new process: in-memory seq map is gone, the log is not
    fields = ss.live_state_fields("chat-log", newest_state="input-required", newest_task_id="t1")
    assert fields["last_change_seq"] == rows[-1]["seq"]
    assert fields["status"] == "paused" and fields["live_task_id"] == "t1"
    assert fields["updated_at"] is not None


def _client(monkeypatch):
    import operator_api.chat_routes as cr

    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_sessions_api_exposes_server_held_state(monkeypatch, tmp_path, state_bus):
    """GET /api/chat/sessions and /{id} carry {status, live_task_id, last_change_seq,
    updated_at} for every lifecycle: parked ⇒ paused, settled ⇒ idle, a restart-stale
    working row ⇒ idle, and a turn in flight (the turn lock) ⇒ running (r1, r3)."""
    import runtime.state as rs
    from runtime import turn_activity

    engine = _engine(
        tmp_path,
        "api",
        [
            {"id": "t1", "context_id": "chat-idle", "state": "TASK_STATE_COMPLETED"},
            {"id": "t2", "context_id": "chat-paused", "state": "TASK_STATE_INPUT_REQUIRED"},
            {"id": "t3", "context_id": "chat-stale", "state": "TASK_STATE_WORKING"},
        ],
    )
    client = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)

    by_id = {r["session_id"]: r for r in client.get("/api/chat/sessions").json()["sessions"]}
    assert by_id["chat-idle"]["status"] == "idle" and by_id["chat-idle"]["live_task_id"] is None
    assert by_id["chat-paused"]["status"] == "paused" and by_id["chat-paused"]["live_task_id"] == "t2"
    assert by_id["chat-stale"]["status"] == "idle"  # a restart cut that turn down; never phantom-running
    assert all(r["updated_at"] == "2026-11-01T12:00:00+00:00" for r in by_id.values())
    assert all(r["last_change_seq"] is None for r in by_id.values())  # nothing published yet

    detail = client.get("/api/chat/sessions/chat-paused").json()
    assert (detail["status"], detail["live_task_id"]) == ("paused", "t2")
    assert detail["last_state"] == "TASK_STATE_INPUT_REQUIRED"  # existing field, untouched

    turn_activity.begin("chat-idle")
    try:
        running = client.get("/api/chat/sessions/chat-idle").json()
    finally:
        turn_activity.end("chat-idle")
    assert running["status"] == "running" and running["active"] is True


def test_delete_route_publishes_deleted(monkeypatch, tmp_path, state_bus):
    """DELETE /api/chat/sessions/{id} publishes one session.changed(deleted)."""
    import operator_api.chat_routes as cr
    import runtime.state as rs

    _bus, events = state_bus
    engine = _engine(tmp_path, "del", [{"id": "t1", "context_id": "chat-del", "state": "TASK_STATE_COMPLETED"}])

    async def _fake_retire(_thread_id, *, harvest=False, cascade=True):
        return None

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    client = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)

    assert client.delete("/api/chat/sessions/chat-del").json()["deleted"] is True
    assert _transitions(events) == [("deleted", "deleted")]
    assert events[0]["data"]["session_id"] == "chat-del"
