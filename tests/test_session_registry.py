"""Server-held chat session LIST state (ADR 0130 S6, D1/D2).

The server owns each chat session's tab-list state — created_at, title, open/closed —
and publishes a change-log event on every transition (create/rename/close/reopen/delete)
so every device converges. ``GET /api/chat/sessions`` folds the list over the task-store
index: open sessions carry their title (a turnless one included), closed ones drop out and
are named in ``closed``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

import server.session_registry as sr
from events import EventBus

AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    sr._reset_for_tests()
    yield
    sr._reset_for_tests()


@pytest.fixture
def bus_events(monkeypatch):
    """A private bus wired into the registry; yields the list of published list events."""
    bus = EventBus()
    events: list[dict] = []
    for topic in (sr.CREATED, sr.RENAMED, sr.CLOSED, sr.REOPENED, sr.DELETED):
        bus.subscribe_handler(topic, lambda p: events.append(p))
    monkeypatch.setattr(sr, "_bus", lambda: bus)
    return events


def _engine(tmp_path, name):
    """A bare SQLite engine wired as the A2A task engine the registry reads/writes."""
    import runtime.state as rs

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}.db")
    rs.STATE.a2a_task_engine = engine
    return engine


def _kinds(events):
    return [(e["data"]["kind"], e["data"].get("title")) for e in events]


# ── registry functions ─────────────────────────────────────────────────────────


def test_register_creates_then_reopens_then_renames(tmp_path, monkeypatch, bus_events):
    _engine(tmp_path, "register")

    async def _run():
        await sr.register_session("chat-a", title="Sprint kickoff")
        await sr.register_session("chat-a", title="Sprint kickoff")  # unchanged: silent
        listing = await sr.read_session_list()
        assert listing["chat-a"] == {"title": "Sprint kickoff", "created_at": listing["chat-a"]["created_at"], "closed": False}
        await sr.close_session("chat-a")
        await sr.register_session("chat-a")  # a new send reopens a closed chat
        after = await sr.read_session_list()
        assert after["chat-a"]["closed"] is False

    asyncio.run(_run())
    assert _kinds(bus_events) == [
        ("created", "Sprint kickoff"),
        ("closed", "Sprint kickoff"),
        ("reopened", "Sprint kickoff"),
    ]


def test_rename_registers_an_unknown_session_then_only_on_change(tmp_path, bus_events):
    _engine(tmp_path, "rename")

    async def _run():
        await sr.rename_session("chat-b", "Deploy status")  # unknown → created
        await sr.rename_session("chat-b", "Deploy status")  # unchanged → silent
        await sr.rename_session("chat-b", "Ship the release")  # changed → renamed
        listing = await sr.read_session_list()
        assert listing["chat-b"]["title"] == "Ship the release"

    asyncio.run(_run())
    assert _kinds(bus_events) == [("created", "Deploy status"), ("renamed", "Ship the release")]


def test_close_and_reopen_keep_the_row_and_publish_once(tmp_path, bus_events):
    _engine(tmp_path, "closereopen")

    async def _run():
        await sr.register_session("chat-c", title="Notes")
        await sr.close_session("chat-c")
        await sr.close_session("chat-c")  # already closed → silent
        mid = await sr.read_session_list()
        assert mid["chat-c"]["closed"] is True  # the row is kept, not deleted
        await sr.reopen_session("chat-c")
        await sr.reopen_session("chat-c")  # already open → silent

    asyncio.run(_run())
    assert _kinds(bus_events) == [("created", "Notes"), ("closed", "Notes"), ("reopened", "Notes")]


def test_delete_drops_the_row_and_always_publishes(tmp_path, bus_events):
    _engine(tmp_path, "delete")

    async def _run():
        await sr.register_session("chat-d", title="Old chat")
        await sr.delete_session("chat-d")
        assert "chat-d" not in await sr.read_session_list()
        await sr.delete_session("chat-never")  # unregistered: still publishes so tabs drop

    asyncio.run(_run())
    assert [e["data"]["session_id"] for e in bus_events if e["data"]["kind"] == "deleted"] == ["chat-d", "chat-never"]


def test_migrate_creates_only_unknown_rows(tmp_path, bus_events):
    _engine(tmp_path, "migrate")

    async def _run():
        await sr.register_session("chat-known", title="Server copy wins")
        n = await sr.migrate_sessions(
            [
                {"session_id": "chat-known", "title": "Stale local title"},  # exists → skipped
                {"session_id": "chat-new", "title": "Local only chat"},
                {"session_id": "job:not-a-chat", "title": "ignored"},  # not a chat session
                {"session_id": "", "title": "blank"},
            ]
        )
        assert n == 1
        listing = await sr.read_session_list()
        assert listing["chat-known"]["title"] == "Server copy wins"  # not overwritten
        assert listing["chat-new"]["title"] == "Local only chat"

    asyncio.run(_run())
    assert [e["data"]["session_id"] for e in bus_events if e["data"]["kind"] == "created"] == ["chat-known", "chat-new"]


def test_job_contexts_are_never_registered(tmp_path, bus_events):
    _engine(tmp_path, "jobs")

    async def _run():
        await sr.register_session("background:bg-1", title="x")
        await sr.register_session("job:abc")
        await sr.close_session("")
        assert await sr.read_session_list() == {}

    asyncio.run(_run())
    assert bus_events == []


def test_no_engine_is_a_safe_noop(monkeypatch, bus_events):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "a2a_task_engine", None, raising=False)

    async def _run():
        await sr.register_session("chat-x", title="t")
        assert await sr.read_session_list() == {}

    asyncio.run(_run())
    assert bus_events == []


# ── GET /api/chat/sessions merge + write routes ─────────────────────────────────


def _client(monkeypatch):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None, **_kw):
        return [{"role": "assistant", "content": "ok", "session_id": session_id}]

    monkeypatch.setattr(cr, "chat", _fake_chat)

    async def _fake_retire(_thread_id, *, harvest=False, cascade=True):
        return None

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def _seed_task_rows(engine, rows):
    from a2a.server.tasks.database_task_store import Base, TaskModel

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


def test_index_folds_in_titles_turnless_and_closed(tmp_path, monkeypatch):
    engine = _engine(tmp_path, "index-merge")
    _seed_task_rows(
        engine,
        [
            {"id": "t1", "context_id": "chat-withturns", "state": "TASK_STATE_COMPLETED"},
            {"id": "t2", "context_id": "chat-closed", "state": "TASK_STATE_COMPLETED"},
        ],
    )
    client = _client(monkeypatch)

    # A client registers a title for the turn-bearing session, a turnless session, and closes one.
    assert client.post("/api/chat/sessions", json={"session_id": "chat-withturns", "title": "Has turns"}).json()["ok"]
    assert client.post("/api/chat/sessions", json={"session_id": "chat-fresh", "title": "No turns yet"}).json()["ok"]
    assert client.patch("/api/chat/sessions/chat-closed", json={"closed": True}).json()["ok"]

    body = client.get("/api/chat/sessions").json()
    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["chat-withturns"]["title"] == "Has turns"
    # r1: a just-created session with no turns yet still appears, with its title.
    assert by_id["chat-fresh"] == {
        "session_id": "chat-fresh",
        "last_updated": by_id["chat-fresh"]["last_updated"],
        "turn_count": 0,
        "last_task_id": None,
        "title": "No turns yet",
        "status": "idle",
        "live_task_id": None,
        "last_change_seq": None,
        "updated_at": by_id["chat-fresh"]["updated_at"],
    }
    assert "chat-closed" not in by_id  # closed tab hidden from the open list
    assert body["closed"] == ["chat-closed"]


def test_patch_rename_and_reopen(tmp_path, monkeypatch):
    _engine(tmp_path, "patch")
    client = _client(monkeypatch)
    client.post("/api/chat/sessions", json={"session_id": "chat-p", "title": "First"})
    assert client.patch("/api/chat/sessions/chat-p", json={"title": "Renamed"}).json()["ok"]
    client.patch("/api/chat/sessions/chat-p", json={"closed": True})
    assert "chat-p" not in {s["session_id"] for s in client.get("/api/chat/sessions").json()["sessions"]}
    client.patch("/api/chat/sessions/chat-p", json={"closed": False})
    reopened = {s["session_id"]: s for s in client.get("/api/chat/sessions").json()["sessions"]}
    assert reopened["chat-p"]["title"] == "Renamed"


def test_migrate_route_registers_open_tabs(tmp_path, monkeypatch):
    _engine(tmp_path, "migrate-route")
    client = _client(monkeypatch)
    res = client.post(
        "/api/chat/sessions/migrate",
        json={"sessions": [{"session_id": "chat-m1", "title": "One"}, {"session_id": "chat-m2", "title": "Two"}]},
    )
    assert res.json() == {"ok": True, "migrated": 2}
    titles = {s["session_id"]: s["title"] for s in client.get("/api/chat/sessions").json()["sessions"]}
    assert titles == {"chat-m1": "One", "chat-m2": "Two"}


def test_delete_route_removes_the_list_row(tmp_path, monkeypatch):
    engine = _engine(tmp_path, "delete-route")
    _seed_task_rows(engine, [{"id": "t1", "context_id": "chat-del", "state": "TASK_STATE_COMPLETED"}])
    client = _client(monkeypatch)
    client.post("/api/chat/sessions", json={"session_id": "chat-del", "title": "Doomed"})
    assert client.delete("/api/chat/sessions/chat-del").json()["deleted"] is True
    listing = client.get("/api/chat/sessions").json()
    assert "chat-del" not in {s["session_id"] for s in listing["sessions"]}
    assert listing["closed"] == []  # deleted, not merely closed
