"""Operator writes preserve exact quotes and the existing chat-scoped ledger contract."""

import asyncio
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.directives import MAX_QUOTE_CHARS, DirectiveStore
from operator_api.chat_routes import register_chat_routes
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = DirectiveStore(tmp_path)
    monkeypatch.setattr(STATE, "directive_store", store)
    events = []
    monkeypatch.setattr("graph.directives._publish", lambda topic, data: events.append((topic, data)))
    note = AsyncMock(return_value=True)
    monkeypatch.setattr(importlib.import_module("server.chat"), "record_directive_withdrawal", note)
    app = FastAPI()
    register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        yield client, store, events, note


def create(client, **fields):
    return client.post("/api/directives", json={
        "session_id": "chat-a", "quote": " \tKeep café safe.\r\n  Stop on WARN.\n ",
        "scope": "release", "conditions": "until Friday", **fields,
    })


def test_create_patch_and_scoped_get(api):
    client, store, events, _ = api
    response = create(client)
    assert response.status_code == 201
    original = response.json()["directive"]
    assert original["author"] == "operator"
    assert original["quote"] == " \tKeep café safe.\r\n  Stop on WARN.\n "
    assert store.get("chat-a", original["id"]).to_dict() == original
    url = f'/api/directives/{original["id"]}?session_id=chat-a'
    updated = client.patch(url, json={"scope": "deploy"}).json()["directive"]
    assert updated == {**original, "scope": "deploy"}
    updated = client.patch(url, json={"conditions": ""}).json()["directive"]
    assert updated == {**original, "scope": "deploy", "conditions": ""}
    assert client.get("/api/chat/sessions/chat-a/directives").json()["directives"] == [updated]
    assert events == [("directive.changed", {"session_id": "chat-a", "id": original["id"]})] * 3


@pytest.mark.parametrize("field,value", [("quote", None), ("quote", "same"), ("author", "agent"), ("session_id", "chat-b"), ("scope", None)])
def test_rejected_patch_has_no_mutation(api, field, value):
    client, store, events, _ = api
    original = create(client, quote="same").json()["directive"]
    response = client.patch(f'/api/directives/{original["id"]}?session_id=chat-a', json={field: value})
    assert response.status_code == 422
    assert store.get("chat-a", original["id"]).to_dict() == original
    assert len(events) == 1


@pytest.mark.parametrize("quote", ["", " \n\t", " " + "x" * MAX_QUOTE_CHARS])
def test_invalid_quote_not_trimmed_to_fit(api, quote):
    client, store, events, _ = api
    assert create(client, quote=quote).status_code == 400
    assert store.list("chat-a") == []
    assert events == []


def test_withdraw_uses_existing_note_and_close(api):
    client, store, events, note = api
    original = create(client).json()["directive"]
    url = f'/api/directives/{original["id"]}/withdraw?session_id=chat-a'
    response = client.post(url, params={"session_id": "chat-a", "reason": "plan changed"})
    assert response.status_code == 200
    closed = store.get("chat-a", original["id"])
    assert closed.status == "revoked" and closed.closed_by == "operator"
    assert closed.quote == original["quote"] and closed.close_reason == "plan changed"
    note.assert_awaited_once_with("chat-a", closed, "plan changed")
    assert len(events) == 2
    assert client.post(url).status_code == 409
    assert client.patch(f'/api/directives/{closed.id}?session_id=chat-a', json={"scope": "x"}).status_code == 409
    note.assert_awaited_once()


@pytest.mark.parametrize("foreign", [False, True])
def test_unknown_and_foreign_ids_fail_without_side_effects(api, foreign):
    client, store, events, note = api
    original = create(client).json()["directive"]
    did = original["id"] if foreign else "dir-unknown"
    session = "chat-b" if foreign else "chat-a"
    assert client.patch(f"/api/directives/{did}?session_id={session}", json={"scope": "x"}).status_code == 404
    assert client.post(f"/api/directives/{did}/withdraw?session_id={session}").status_code == 404
    assert store.get("chat-a", original["id"]).to_dict() == original
    assert len(events) == 1
    note.assert_not_awaited()


def test_legacy_delete_remains_compatible(api):
    client, _, _, note = api
    original = create(client).json()["directive"]
    assert client.delete(f'/api/chat/sessions/chat-a/directives/{original["id"]}').status_code == 200
    note.assert_awaited_once()


def test_concurrent_store_patches_keep_both_fields(tmp_path):
    first, second = DirectiveStore(tmp_path), DirectiveStore(tmp_path)
    original = first.record("chat-a", "quote", author="operator")
    barrier = threading.Barrier(2)

    def patch(store, **fields):
        barrier.wait(timeout=5)
        store.update("chat-a", original.id, **fields)

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(patch, first, scope="new scope")
        b = pool.submit(patch, second, conditions="new conditions")
        a.result(timeout=5)
        b.result(timeout=5)
    assert first.get("chat-a", original.id).to_dict() == {
        **original.to_dict(), "scope": "new scope", "conditions": "new conditions",
    }


def test_routes_offload_store_operations(api, monkeypatch):
    client, store, _, _ = api
    for name in ("record", "update", "close"):
        original = getattr(store, name)

        def checked(*args, _original=original, **kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, checked)
    did = create(client).json()["directive"]["id"]
    assert client.patch(f"/api/directives/{did}?session_id=chat-a", json={"scope": "x"}).status_code == 200
    assert client.post(f"/api/directives/{did}/withdraw?session_id=chat-a").status_code == 200


def test_mutations_require_operator_auth(api, monkeypatch):
    from a2a_impl import auth

    _, store, _, note = api
    monkeypatch.setattr(auth, "_BEARER", ["test-operator"])
    monkeypatch.setattr(auth, "_FEDERATION", ["test-federation"])
    monkeypatch.setattr(auth, "_FLEET", [None])
    app = FastAPI()
    app.add_middleware(auth.A2AAuthMiddleware)
    register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        for headers in ({}, {"Authorization": "Bearer test-federation"}):
            assert client.post("/api/directives", json={"session_id": "chat-a", "quote": "x"}, headers=headers).status_code in (401, 403)
            assert client.patch("/api/directives/dir-x?session_id=chat-a", json={"scope": "x"}, headers=headers).status_code in (401, 403)
            assert client.post("/api/directives/dir-x/withdraw?session_id=chat-a", headers=headers).status_code in (401, 403)
        assert store.list("chat-a") == []
        assert client.post("/api/directives", json={"session_id": "chat-a", "quote": "x"}, headers={"Authorization": "Bearer test-operator"}).status_code == 201
    note.assert_not_awaited()


def test_session_identity_is_not_trimmed(api):
    client, store, _, _ = api
    original = create(client, session_id=" chat-a ").json()["directive"]
    assert original["session_id"] == " chat-a "
    assert store.list("chat-a") == []
    assert client.patch(f'/api/directives/{original["id"]}', params={"session_id": " chat-a "}, json={"scope": "x"}).status_code == 200
