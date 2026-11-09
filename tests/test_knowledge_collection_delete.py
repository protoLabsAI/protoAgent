"""Collection bulk deletion shares the source lifecycle, including private-only writes."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore
from operator_api.knowledge_routes import _INVALIDATED_GRACE_SECONDS, register_knowledge_routes


def _client(monkeypatch, store):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "knowledge_store", store)
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def _add(store, label, collection="books", domain="docs"):
    cid = store.add_chunk(f"orchard {label}", domain=domain, collection=collection, source=label)
    assert cid is not None
    return cid


def _post(client, action, collection="books"):
    response = client.post(f"/api/knowledge/{action}-by-collection", json={"collection": collection})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("fts", [True, False])
def test_delete_and_restore_collection(monkeypatch, tmp_path, fts):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    ids = {_add(store, "first"), _add(store, "second")}
    other = _add(store, "other", "papers")
    superseded = _add(store, "superseded")
    assert store.invalidate_chunk(superseded)
    monkeypatch.setattr(store, "_fts_available", fts)
    client = _client(monkeypatch, store)
    assert _post(client, "delete") == {"enabled": True, "deleted": 2}
    assert {r["id"] for r in store.search("orchard", k=20)} == {other}
    assert [r["collection"] for r in store.list_collections()] == ["papers"]
    assert _post(client, "delete")["deleted"] == 0
    assert _post(client, "restore") == {"enabled": True, "restored": 2}
    assert {r["id"] for r in store.search("orchard", k=20)} == ids | {other}
    assert {r["collection"]: r["chunks"] for r in store.list_collections()} == {"books": 2, "papers": 1}
    assert _post(client, "restore")["restored"] == 0
    assert store.get_chunk(superseded)["invalidated_at"] is not None


def test_general_includes_legacy_null_and_empty_but_not_history(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    ids = {_add(store, "explicit", "general"), _add(store, "null", None)}
    empty = _add(store, "empty", None)
    ids.add(empty)
    history = _add(store, "history", None, "conversation")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE chunks SET collection = '' WHERE id = ?", (empty,))
    assert store.invalidate_by_collection(" general ") == 3
    assert store.get_chunk(history)["invalidated_at"] is None
    # Even a bulk-deleted history row must not be restored by collection.
    assert store.invalidate_by_source("history") == 1
    assert store.restore_by_collection("general") == 3
    assert store.get_chunk(history)["invalidated_at"] is not None
    assert {r["id"] for r in store.search("orchard", k=20)} == ids
    for name in ("", "   ", "unknown"):
        assert store.invalidate_by_collection(name) == 0
        assert store.restore_by_collection(name) == 0


def test_delete_sweeps_past_grace_rows(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    old = _add(store, "old")
    recent = _add(store, "recent", "recent")
    assert store.invalidate_by_collection("books") == 1
    assert store.invalidate_by_collection("recent") == 1
    aged = (datetime.now(timezone.utc) - timedelta(seconds=_INVALIDATED_GRACE_SECONDS + 60)).isoformat()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE chunks SET invalidated_at = ? WHERE id = ?", (aged, old))
    client = _client(monkeypatch, store)
    assert _post(client, "delete", "unknown")["deleted"] == 0
    assert store.get_chunk(old) is None
    assert _post(client, "restore")["restored"] == 0
    assert _post(client, "restore", "recent")["restored"] == 1
    assert store.get_chunk(recent)["invalidated_at"] is None


def test_layered_collection_lifecycle_only_touches_private(monkeypatch, tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    _add(private, "private")
    shared = _add(commons, "shared")
    client = _client(monkeypatch, LayeredKnowledgeStore(private, commons))
    assert _post(client, "delete")["deleted"] == 1
    assert commons.get_chunk(shared)["invalidated_at"] is None
    assert _post(client, "restore")["restored"] == 1
    assert commons.invalidate_by_collection("books") == 1
    assert _post(client, "restore")["restored"] == 0
    assert commons.get_chunk(shared)["invalidated_at"] is not None


@pytest.mark.parametrize("action,count", [("delete", "deleted"), ("restore", "restored")])
def test_route_validation_and_optional_backend(monkeypatch, action, count):
    client = _client(monkeypatch, object())
    for body in (None, {}, {"collection": ""}, {"collection": "   "}, {"collection": None}):
        assert client.post(f"/api/knowledge/{action}-by-collection", json=body).status_code == 400
    body = _post(client, action)
    assert body["enabled"] is True
    assert body[count] == 0
    assert isinstance(body["error"], str) and body["error"]
    client = _client(monkeypatch, None)
    assert _post(client, action) == {"enabled": False, count: 0}


@pytest.mark.parametrize("action,count", [("delete", "deleted"), ("restore", "restored")])
def test_backend_failure_returns_error(monkeypatch, action, count):
    class Broken:
        def invalidate_by_collection(self, collection):
            raise RuntimeError("backend unavailable")

        restore_by_collection = invalidate_by_collection

    body = _post(_client(monkeypatch, Broken()), action)
    assert body == {"enabled": True, count: 0, "error": "backend unavailable"}


def test_failed_sweep_does_not_block_delete(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    _add(store, "first")

    def fail(grace):
        assert grace == 24 * 3600
        raise RuntimeError("sweep unavailable")

    monkeypatch.setattr(store, "purge_invalidated", fail)
    assert _post(_client(monkeypatch, store), "delete")["deleted"] == 1
