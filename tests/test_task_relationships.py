"""Durable request lineage and idempotent promotion in the existing tasks store."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from tasks.store import TaskStore


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)
    return str(tmp_path / "issues.db")


@contextmanager
def opened(db_path):
    store = TaskStore(db_path)
    try:
        yield store
    finally:
        store._conn.close()


def test_old_schema_upgrades_without_backfilling_relationships(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE issues (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
                priority INTEGER NOT NULL DEFAULT 2, issue_type TEXT NOT NULL DEFAULT 'task',
                assignee TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT
            )
        """)
        conn.execute("INSERT INTO issues (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     ("task-7", "legacy", "before", "before"))
    with opened(db_path):
        pass
    with opened(db_path) as store:
        row = store.get("task-7")
        assert row["title"] == "legacy"
        assert row["created_at"] == row["updated_at"] == "before"
        assert row["rel_version"] == 0
        assert row["request_ref"] == row["requester"] == row["promotion_key"] == ""
        assert store.create("new")["id"] == "task-8"


@pytest.mark.parametrize("kind", ["inbox", "a2a"])
def test_relationships_persist_and_cannot_be_updated(db_path, kind):
    request = {"kind": kind, "owner": "agent", "context_id": "ctx", "message_id": "msg",
               "task_id": "remote-task", "item_id": "item"}
    requester = {"caller": "Josh", "caller_kind": "operator", "on_behalf_of": "team"}
    with opened(db_path) as store:
        row = store.create("work", request={**request, "unknown": "drop"},
                           requester={**requester, "authority": "drop"}, promotion_key="opaque")
    with opened(db_path) as store:
        persisted = store.get(row["id"])
        assert persisted == row
        assert persisted["rel_version"] == 1
        assert persisted["request_ref"] == json.dumps(request, separators=(",", ":"))
        assert persisted["requester"] == json.dumps(requester, separators=(",", ":"))
        store.update(row["id"], title="changed", request_ref="bad", requester="bad",
                     promotion_key="bad", rel_version=9)
    with opened(db_path) as store:
        updated = store.get(row["id"])
        assert updated["title"] == "changed"
        for key in ("request_ref", "requester", "promotion_key", "rel_version"):
            assert updated[key] == row[key]
        snapshot = store.delete_with_snapshot(row["id"])
        assert snapshot == updated
    with opened(db_path) as store:
        assert store.restore(snapshot) == updated
    with opened(db_path) as store:
        assert store.get(row["id"]) == updated


@pytest.mark.parametrize("metadata", [None, {}, {"unknown": "drop"},
                                     {"owner": "", "caller": None, "context_id": "   "}])
def test_unknown_metadata_stays_empty_and_unkeyed_creates_are_distinct(db_path, metadata):
    with opened(db_path) as store:
        rows = [store.create("work", request=metadata, requester=metadata) for _ in range(2)]
    with opened(db_path) as store:
        assert store.list() == rows
        assert rows[0]["id"] != rows[1]["id"]
        for row in store.list():
            assert row["rel_version"] == 1
            assert row["request_ref"] == row["requester"] == row["promotion_key"] == ""


def test_promotion_retries_return_original_without_creating_or_publishing(db_path, monkeypatch):
    events = []
    monkeypatch.setattr("tasks.store._publish", lambda topic, data: events.append((topic, data)))
    with opened(db_path) as store:
        row = store.create("original", request={"kind": "inbox", "item_id": "item"},
                           requester={"caller": "Josh"}, promotion_key=" key ")
        assert store.create("retry", promotion_key=" key ") == {**row, "promoted_existing": True}
    with opened(db_path) as store:
        assert store.create("restart retry", request={"item_id": "different"},
                            promotion_key=" key ") == {**row, "promoted_existing": True}
        assert store.list() == [row]
        assert store.get(row["id"]) == row
        assert events == [("task.changed", {"id": row["id"], "action": "created"})]
        # Retries do not allocate another identity.
        assert store.create("next")["id"] == "task-2"


def test_different_promotion_keys_preserve_shared_request_lineage(db_path):
    request = {"kind": "a2a", "owner": "agent", "context_id": "ctx", "task_id": "remote"}
    with opened(db_path) as store:
        first = store.create("work", request=request, promotion_key="one")
    with opened(db_path) as store:
        second = store.create("work", request=request, promotion_key="two")
    with opened(db_path) as store:
        assert store.list() == [first, second]
        assert first["id"] != second["id"]
        assert first["request_ref"] == second["request_ref"]
        assert json.loads(first["request_ref"]) == request
