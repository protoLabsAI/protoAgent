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
