"""Legacy relationship adoption preserves provenance and retries atomically."""

import sqlite3
from contextlib import closing

import pytest

from tasks.store import TaskStore


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)
    path = str(tmp_path / "issues.db")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            CREATE TABLE issues (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
                priority INTEGER NOT NULL DEFAULT 2, issue_type TEXT NOT NULL DEFAULT 'task',
                assignee TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT,
                session_id TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.executemany(
            "INSERT INTO issues (id, title, created_at, updated_at, session_id, links) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("task-1", "legacy", "created", "unchanged-1", "session:é/one",
                 '[ { "ref": "bd-7", "kind": "card", "state": "done" } ]'),
                ("task-2", "other", "created", "unchanged-2", "job:two", "not-json"),
                ("task-3", "unknown", "created", "unchanged-3", "", ""),
            ],
        )
    return path


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM issues ORDER BY id")]


def test_old_schema_backfill_preserves_rows_and_reopens_silently(legacy_db, monkeypatch):
    before = snapshot(legacy_db)
    events = []
    monkeypatch.setattr("tasks.store._publish", lambda *args: events.append(args))
    for _ in range(3):
        store = TaskStore(legacy_db)
        store._conn.close()
        after = snapshot(legacy_db)
        for old, new in zip(before, after, strict=True):
            assert {key: new[key] for key in old} == old
            assert new["rel_version"] == 1
            assert new["request_ref"] == new["requester"] == new["promotion_key"] == ""
        before = after
    assert events == []


def test_existing_versions_are_untouched_and_failed_backfill_retries(legacy_db, monkeypatch):
    # Model a P2a database whose relationship columns already exist.
    with closing(sqlite3.connect(legacy_db)) as conn, conn:
        conn.execute("ALTER TABLE issues ADD COLUMN rel_version INTEGER NOT NULL DEFAULT 0")
        for column in ("request_ref", "requester", "promotion_key"):
            conn.execute(f"ALTER TABLE issues ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        for version in (1, 2):
            conn.execute(
                "INSERT INTO issues (id, title, created_at, updated_at, rel_version, "
                "request_ref, requester, promotion_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"task-{version + 3}", "versioned", "created", "untouched", version,
                 '{"item_id":"durable"}', '{"caller":"operator"}', f"key-{version}"),
            )
    before = snapshot(legacy_db)
    connect = sqlite3.connect
    connections = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql.startswith("UPDATE issues SET rel_version"):
                assert self.in_transaction
                assert self.execute("SELECT COUNT(*) FROM issues WHERE rel_version = 0").fetchone()[0] == 0
                raise RuntimeError("interrupted backfill")
            return result

    def failing_connect(*args, **kwargs):
        conn = connect(*args, **kwargs, factory=FailingConnection)
        connections.append(conn)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr("tasks.store.sqlite3.connect", failing_connect)
        try:
            with pytest.raises(RuntimeError, match="interrupted backfill"):
                TaskStore(legacy_db)
            assert not connections[0].in_transaction
        finally:
            for conn in connections:
                conn.close()
    assert snapshot(legacy_db) == before

    store = TaskStore(legacy_db)
    store._conn.close()
    after = snapshot(legacy_db)
    for old, new in zip(before, after, strict=True):
        assert {key: new[key] for key in old} == {**old, "rel_version": max(1, old["rel_version"])}
