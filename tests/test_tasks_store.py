"""In-process tasks store (Sprint B): SQLite issue tracker — create/list/update/
close/delete, replacing the file-based `br` CLI."""

from __future__ import annotations

import pytest

from tasks.store import TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


def test_create_assigns_sequential_id_and_defaults(store):
    a = store.create("first task")
    b = store.create("second", priority=1, issue_type="bug", description="boom")
    assert a["id"] == "task-1" and b["id"] == "task-2"
    assert a["status"] == "open" and a["priority"] == 2 and a["issue_type"] == "task"
    assert b["priority"] == 1 and b["issue_type"] == "bug" and b["description"] == "boom"
    assert a["created_at"] and a["updated_at"]


def test_create_requires_title(store):
    with pytest.raises(ValueError):
        store.create("   ")


def test_list_and_include_closed(store):
    store.create("open one")
    closed = store.create("done one")
    store.close(closed["id"], reason="shipped")
    assert len(store.list()) == 2
    open_only = store.list(include_closed=False)
    assert [i["id"] for i in open_only] == ["task-1"]


def test_update_fields_and_type_alias(store):
    i = store.create("t")
    out = store.update(i["id"], status="in_progress", priority=0, type="feature", title="renamed")
    assert out["status"] == "in_progress" and out["priority"] == 0
    assert out["issue_type"] == "feature" and out["title"] == "renamed"
    assert out["updated_at"] >= i["updated_at"]


def test_update_normalizes_bad_status_and_type(store):
    i = store.create("t")
    out = store.update(i["id"], status="bogus", issue_type="nope")
    assert out["status"] == "open" and out["issue_type"] == "task"


def test_close_sets_terminal_fields(store):
    i = store.create("t")
    out = store.close(i["id"], reason="obsolete")
    assert out["status"] == "closed" and out["closed_at"] and out["close_reason"] == "obsolete"


def test_update_and_close_unknown_raise(store):
    with pytest.raises(KeyError):
        store.update("task-999", status="open")
    with pytest.raises(KeyError):
        store.close("task-999")


def test_delete(store):
    i = store.create("t")
    assert store.delete(i["id"]) is True
    assert store.get(i["id"]) is None
    assert store.delete("task-999") is False


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "issues.db")
    TaskStore(db_path=path).create("survive me")
    assert [i["title"] for i in TaskStore(db_path=path).list()] == ["survive me"]


# ── agent tools over the store ────────────────────────────────────────────────


def test_task_tools_wired_and_functional(store):
    from tools.lg_tools import get_all_tools

    by_name = {getattr(t, "name", ""): t for t in get_all_tools(tasks_store=store)}
    for name in ("task_create", "task_list", "task_update", "task_close"):
        assert name in by_name
    # Not added when no store is passed.
    assert "task_create" not in {getattr(t, "name", "") for t in get_all_tools()}

    out = by_name["task_create"].invoke({"title": "ship tasks", "priority": 1})
    assert "task-1" in out
    by_name["task_update"].invoke({"issue_id": "task-1", "status": "in_progress"})
    listing = by_name["task_list"].invoke({})
    assert "in_progress" in listing and "ship tasks" in listing
    closed = by_name["task_close"].invoke({"issue_id": "task-1", "reason": "done"})
    assert "Closed task-1" in closed
    assert store.get("task-1")["status"] == "closed"


def test_operator_adapter_maps_to_store(store):
    """The console's tasks routes go through _TaskStoreAdapter → the in-process
    store (project_path ignored), so the agent + console share one board."""
    from operator_api.routes import _TaskStoreAdapter

    a = _TaskStoreAdapter(store)
    assert a.status("/anything") == {"initialized": True}
    issue = a.create("/anything", {"title": "from console", "type": "bug", "priority": 1})
    assert issue["id"] == "task-1" and issue["issue_type"] == "bug"
    assert a.list("/anything")[0]["title"] == "from console"
    a.update("/x", "task-1", {"status": "in_progress", "project_path": "/x"})  # project_path ignored
    assert store.get("task-1")["status"] == "in_progress"
    a.close("/x", "task-1", "shipped")
    assert store.get("task-1")["status"] == "closed"
    assert a.delete("/x", "task-1") == {"deleted": True}


def test_mutations_publish_task_changed(store, monkeypatch):
    """create/update/close/delete push `task.changed` so the console Tasks panel
    invalidates live instead of polling every 5s (#1310)."""
    from graph.plugins import host

    events: list[tuple] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    issue = store.create("do a thing")
    store.update(issue["id"], status="in_progress")
    store.close(issue["id"])
    assert store.delete(issue["id"]) is True
    assert [(t, d["action"]) for t, d in events] == [
        ("task.changed", "created"),
        ("task.changed", "updated"),
        ("task.changed", "closed"),
        ("task.changed", "deleted"),
    ]


def test_delete_missing_does_not_publish(store, monkeypatch):
    from graph.plugins import host

    events: list[tuple] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    assert store.delete("bd-404") is False
    assert events == []  # nothing deleted → no event


# --- ADR 0079: task→goal/session attribution -------------------------------


def test_create_stamps_session_id_and_defaults_empty(store):
    a = store.create("no session")
    b = store.create("goal task", session_id="sess-7")
    assert a["session_id"] == ""
    assert b["session_id"] == "sess-7"


def test_list_filters_by_session(store):
    store.create("global one")
    store.create("goal A", session_id="A")
    store.create("goal A two", session_id="A")
    store.create("goal B", session_id="B")
    a_tasks = store.list(session_id="A")
    assert {t["title"] for t in a_tasks} == {"goal A", "goal A two"}
    assert len(store.list(session_id="B")) == 1
    assert len(store.list()) == 4  # unfiltered still returns all


def test_session_id_column_migrates_onto_a_legacy_board(tmp_path):
    """A board created WITHOUT session_id (pre-ADR-0079) gains the column on next open,
    non-destructively — the guarded ALTER keeps live prod boards loading clean."""
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE issues (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', "
        "status TEXT DEFAULT 'open', priority INTEGER DEFAULT 2, issue_type TEXT DEFAULT 'task', "
        "assignee TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "closed_at TEXT, close_reason TEXT)"
    )
    conn.execute("INSERT INTO issues (id, title, created_at, updated_at) VALUES ('task-1','old one','t','t')")
    conn.commit()
    conn.close()

    store = TaskStore(db_path=db)  # opens + migrates
    rows = store.list()
    assert rows and rows[0]["id"] == "task-1"
    assert rows[0]["session_id"] == ""  # existing row backfilled to the default
    # and new session-attributed creates work on the migrated board
    assert store.create("new", session_id="s9")["session_id"] == "s9"


@pytest.mark.platform_sensitive
def test_ids_survive_delete_all_and_restart(store):
    original = store.create("A")
    assert store.delete(original["id"])
    store._conn.close()
    reopened = TaskStore(db_path=str(store.path))
    try:
        other = reopened.create("B")
        assert other["id"] == "task-2"
        assert reopened.restore(original) == original
        assert {row["id"] for row in reopened.list()} == {original["id"], other["id"]}
        with pytest.raises(ValueError, match="already exists"):
            reopened.restore(original)
    finally:
        reopened._conn.close()


@pytest.mark.platform_sensitive
def test_sequence_migrates_legacy_ids_and_reserves_restored_ids(store):
    original = store.create("original")
    # Model an existing board with no persistent sequence, including legacy IDs.
    with store._conn:
        store._conn.execute("DROP TABLE task_sequence")
        store._conn.execute("UPDATE issues SET id = 'bd-42'")
    store._conn.close()
    reopened = TaskStore(db_path=str(store.path))
    try:
        assert reopened.get("bd-42")["title"] == "original"
        assert reopened.delete("bd-42")
        assert reopened.create("next")["id"] == "task-43"
        reopened.restore({**original, "id": "task-100"})
        assert reopened.delete("task-100")
        assert reopened.create("after restore")["id"] == "task-101"
    finally:
        reopened._conn.close()


def test_failed_write_rolls_back_and_store_remains_usable(store):
    import sqlite3

    original = store.create("original")
    with pytest.raises(sqlite3.IntegrityError):
        store.restore({"id": "task-500"})  # Missing required title/timestamps.
    assert not store._conn.in_transaction
    with pytest.raises(ValueError):
        store.create("invalid priority", priority="not a number")
    assert not store._conn.in_transaction
    assert store.get(original["id"]) == original
    assert store.create("after failure")["id"] == "task-2"


@pytest.mark.platform_sensitive
def test_multiple_connections_serialize_task_operations(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr("tasks.store._publish", lambda *args: None)
    other = TaskStore(db_path=str(store.path))
    start = Barrier(2)

    def exercise(connection):
        start.wait(timeout=10)
        ids = []
        for index in range(40):
            row = connection.create(f"task {index}")
            ids.append(row["id"])
            updated = connection.update(row["id"], title="updated")
            assert updated["title"] == "updated"
            assert connection.delete(row["id"])
            assert connection.restore(updated) == updated
            assert connection.close(row["id"])["status"] == "closed"
            assert connection.delete(row["id"])
        return ids

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(exercise, (store, other)))
        ids = [issue_id for result in results for issue_id in result]
        assert len(set(ids)) == 80
        assert store.list() == []
        assert other.create("after stress")["id"] == "task-81"
    finally:
        other._conn.close()


@pytest.mark.asyncio
async def test_eight_http_clients_create_update_delete_undo(store, monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from operator_api.routes import register_operator_routes
    from operator_api.undo import UndoLedger

    # A private ledger keeps tokens isolated from other tests.
    monkeypatch.setattr("operator_api.undo.LEDGER", UndoLedger())
    monkeypatch.setattr("tasks.store._publish", lambda *args: None)
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        tasks_store=store,
    )

    async def exercise(client_index):
        ids = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def create(title):
                response = await client.post("/api/tasks/issues", json={"title": title})
                assert response.status_code == 200, response.text
                row = response.json()["issue"]
                ids.append(row["id"])
                return row

            for index in range(15):
                original = await create(f"{client_index}-{index}")
                url = f"/api/tasks/issues/{original['id']}"
                updated = await client.patch(url, json={"status": "in_progress"})
                assert updated.status_code == 200, updated.text
                snapshot = updated.json()["issue"]
                deleted = await client.delete(url)
                assert deleted.status_code == 200, deleted.text
                token = deleted.json()["undo_token"]
                other = await create("unrelated")
                assert other["id"] != original["id"]
                undo = await client.post(f"/api/undo/{token}")
                assert undo.status_code == 200, undo.text
                assert store.get(original["id"]) == snapshot
                assert store.get(other["id"]) == other
                assert (await client.post(f"/api/undo/{token}")).status_code == 404
                # Competing PATCH/delete requests must succeed or report missing,
                # never return a server error or mutate an unrelated task.
                patched, removed = await asyncio.gather(
                    client.patch(url, json={"title": "racing update"}), client.delete(url)
                )
                assert patched.status_code in (200, 404), patched.text
                assert removed.status_code == 200, removed.text
                assert (await client.get("/api/tasks/issues")).status_code == 200
        return ids

    results = await asyncio.gather(*(exercise(index) for index in range(8)))
    ids = [issue_id for result in results for issue_id in result]
    assert len(ids) == len(set(ids)) == 240
    assert len(store.list()) == 120


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("operation", ["create", "update", "close", "restore"])
def test_mutation_returns_snapshot_even_if_deleted_after_commit(store, monkeypatch, operation):
    """A concurrent writer may delete immediately after commit, before publication returns."""
    other = TaskStore(db_path=str(store.path))
    row = store.create("original")
    if operation == "restore":
        store.delete(row["id"])

    def delete_after_commit(topic, data):
        # Use SQL to avoid recursively publishing another event. This also proves
        # callbacks run after commit and outside the store's connection lock.
        assert store.get(data["id"]) is not None
        with other._conn:
            other._conn.execute("DELETE FROM issues WHERE id = ?", (data["id"],))

    monkeypatch.setattr("tasks.store._publish", delete_after_commit)
    try:
        if operation == "create":
            result = store.create("created")
        elif operation == "update":
            result = store.update(row["id"], title="updated")
        elif operation == "close":
            result = store.close(row["id"])
        else:
            result = store.restore(row)
        assert result["title"] == {"create": "created", "update": "updated"}.get(operation, "original")
        assert store.get(result["id"]) is None
    finally:
        other._conn.close()


@pytest.mark.parametrize("snapshot_available", [True, False])
def test_delete_undo_captures_the_row_actually_deleted(store, monkeypatch, snapshot_available):
    """An update between a legacy snapshot and delete must not be lost by Undo."""
    from operator_api.routes import _TaskStoreAdapter, _delete_task_with_undo
    from operator_api.undo import UndoLedger

    ledger = UndoLedger()
    monkeypatch.setattr("operator_api.undo.LEDGER", ledger)
    original = store.create("original", session_id="session")
    adapter = _TaskStoreAdapter(store)
    # Model a snapshot taken before another client commits an update. The real
    # store must use its atomic delete path instead of this separate read.
    monkeypatch.setattr(adapter, "snapshot", lambda issue_id: original if snapshot_available else None)
    store.update(original["id"], title="latest")
    result = _delete_task_with_undo(adapter, "", original["id"])
    assert result["deleted"] is True
    assert ledger.restore(result["undo_token"]) == "task"
    assert store.get(original["id"])["title"] == "latest"
    assert store.get(original["id"])["session_id"] == "session"


@pytest.mark.platform_sensitive
@pytest.mark.asyncio
async def test_precreated_tasks_concurrent_reads_delete_and_double_undo(store, monkeypatch):
    """QA bd-vaa.10: eight clients, no concurrent creates, two redeems per token."""
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from operator_api.routes import register_operator_routes
    from operator_api.undo import UndoLedger

    monkeypatch.setattr("operator_api.undo.LEDGER", UndoLedger())
    monkeypatch.setattr("tasks.store._publish", lambda *args: None)
    rows = [store.create(f"task {index}", session_id=f"session-{index}") for index in range(80)]
    app = FastAPI()
    register_operator_routes(
        app, runtime_status=lambda: {}, subagent_list=lambda: [],
        subagent_run=lambda r: None, subagent_batch=lambda r: None, tasks_store=store,
    )

    async def exercise(client_index):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            for iteration in range(3):
                for original in rows[client_index::8]:
                    issue_id = original["id"]
                    url = f"/api/tasks/issues/{issue_id}"
                    updated, listing = await asyncio.gather(
                        client.patch(url, json={"title": f"{issue_id}-{iteration}", "status": "in_progress"}),
                        client.get("/api/tasks/issues"),
                    )
                    assert updated.status_code == 200, updated.text
                    assert listing.status_code == 200, listing.text
                    snapshot = updated.json()["issue"]
                    assert await asyncio.to_thread(store.get, issue_id) == snapshot
                    deleted = await client.delete(url)
                    assert deleted.status_code == 200, deleted.text
                    assert deleted.json()["deleted"] is True
                    token = deleted.json()["undo_token"]
                    assert token
                    results = await asyncio.gather(
                        client.post(f"/api/undo/{token}"), client.post(f"/api/undo/{token}"),
                    )
                    assert sorted(r.status_code for r in results) == [200, 404], [r.text for r in results]
                    assert await asyncio.to_thread(store.get, issue_id) == snapshot

    await asyncio.gather(*(exercise(index) for index in range(8)))
    assert len(store.list()) == 80


@pytest.mark.parametrize("operation", ["get", "list"])
def test_reads_hold_connection_lock_through_fetch(store, monkeypatch, operation):
    """Guard the shared-connection fix independently of SQLite build/timing."""
    row = store.create("original")
    connection = store._conn

    class GuardedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            assert store._lock.locked()
            return self.cursor.fetchone()

        def fetchall(self):
            assert store._lock.locked()
            return self.cursor.fetchall()

    class GuardedConnection:
        def execute(self, *args):
            assert store._lock.locked()
            return GuardedCursor(connection.execute(*args))

    monkeypatch.setattr(store, "_conn", GuardedConnection())
    if operation == "get":
        assert store.get(row["id"]) == row
    else:
        assert store.list() == [row]


@pytest.mark.platform_sensitive
def test_competing_deletes_return_exactly_one_snapshot(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    row = store.create("original", session_id="session")
    other = TaskStore(db_path=str(store.path))
    barrier = Barrier(2)
    events = []

    def publish(topic, data):
        # Publication must happen after commit and outside the connection lock.
        assert store.get(row["id"]) == (None if data["action"] == "deleted" else row)
        events.append((topic, data))

    monkeypatch.setattr("tasks.store._publish", publish)

    def delete(connection):
        barrier.wait(timeout=10)
        return connection.delete_with_snapshot(row["id"])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(delete, (store, other)))
        assert results.count(None) == 1
        assert [result for result in results if result is not None] == [row]
        assert events == [("task.changed", {"id": row["id"], "action": "deleted"})]
        assert store.restore(row) == row
    finally:
        other._conn.close()
