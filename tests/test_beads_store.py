"""In-process beads store (Sprint B): SQLite issue tracker — create/list/update/
close/delete, replacing the file-based `br` CLI."""

from __future__ import annotations

import pytest

from beads.store import BeadsStore


@pytest.fixture
def store(tmp_path):
    return BeadsStore(db_path=str(tmp_path / "issues.db"))


def test_create_assigns_sequential_id_and_defaults(store):
    a = store.create("first task")
    b = store.create("second", priority=1, issue_type="bug", description="boom")
    assert a["id"] == "bd-1" and b["id"] == "bd-2"
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
    assert [i["id"] for i in open_only] == ["bd-1"]


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
        store.update("bd-999", status="open")
    with pytest.raises(KeyError):
        store.close("bd-999")


def test_delete(store):
    i = store.create("t")
    assert store.delete(i["id"]) is True
    assert store.get(i["id"]) is None
    assert store.delete("bd-999") is False


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "issues.db")
    BeadsStore(db_path=path).create("survive me")
    assert [i["title"] for i in BeadsStore(db_path=path).list()] == ["survive me"]
