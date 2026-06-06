"""Tests for the delegate CRUD store + REST API (ADR 0025, PR2)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import plugins.delegates.api as api
from plugins.delegates import store


@pytest.fixture
def fake_io(monkeypatch):
    """In-memory config doc + secrets, swapped in for graph.config_io."""
    st = {"doc": {}, "secrets": {}}
    import graph.config_io as cio

    monkeypatch.setattr(cio, "load_yaml_doc", lambda *a, **k: st["doc"])
    monkeypatch.setattr(cio, "save_yaml_doc", lambda doc, *a, **k: st.update(doc=doc))
    monkeypatch.setattr(cio, "load_secrets", lambda: st["secrets"])

    def _save_secrets(upd):
        for sec, vals in (upd or {}).items():
            st["secrets"].setdefault(sec, {}).update(vals)

    monkeypatch.setattr(cio, "save_secrets", _save_secrets)
    return st


# ── store ─────────────────────────────────────────────────────────────────────


def test_upsert_routes_secret_to_overlay_and_strips_config(fake_io):
    store.upsert_delegate({"name": "helm", "type": "a2a", "url": "https://h/a2a",
                           "auth": {"scheme": "bearer", "token": "SEKRET"}})
    stored = fake_io["doc"]["delegates"][0]
    assert stored["auth"] == {"scheme": "bearer"}            # token stripped from config
    assert "SEKRET" not in str(stored)
    assert fake_io["secrets"]["delegate_secrets"]["helm.auth.token"] == "SEKRET"


def test_merged_delegates_overlays_secret(fake_io):
    store.upsert_delegate({"name": "opus", "type": "openai", "url": "https://g/v1",
                           "model": "m", "api_key": "K"})
    assert "K" not in str(fake_io["doc"]["delegates"])        # not in tracked config
    merged = store.merged_delegates()
    assert merged[0]["api_key"] == "K"                        # overlaid back at load


def test_upsert_replaces_by_name_and_delete(fake_io):
    store.upsert_delegate({"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"})
    store.upsert_delegate({"name": "p", "type": "acp", "command": "proto2", "workdir": "/tmp"})
    assert len(fake_io["doc"]["delegates"]) == 1
    assert fake_io["doc"]["delegates"][0]["command"] == "proto2"
    store.delete_delegate("p")
    assert fake_io["doc"]["delegates"] == []


# ── API ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(fake_io, monkeypatch):
    async def _noreload():
        return True, "reloaded"
    monkeypatch.setattr(api, "_reload", _noreload)
    app = FastAPI()
    app.include_router(api.build_router())
    return TestClient(app)


def test_delegate_types_endpoint(client):
    r = client.get("/api/delegate-types")
    assert r.status_code == 200
    assert {t["type"] for t in r.json()["types"]} == {"a2a", "openai", "acp"}


def test_create_list_update_delete_flow(client, fake_io):
    # create
    r = client.post("/api/delegates", json={"name": "opus", "type": "openai",
                                            "url": "https://g/v1", "model": "m", "api_key": "K"})
    assert r.status_code == 200 and r.json()["ok"] is True
    names = [d["name"] for d in r.json()["delegates"]]
    assert names == ["opus"]
    # secret routed, not echoed
    body = r.json()["delegates"][0]
    assert "K" not in str(body) and body["has_secret"] is True
    assert fake_io["secrets"]["delegate_secrets"]["opus.api_key"] == "K"

    # duplicate → 409
    assert client.post("/api/delegates", json={"name": "opus", "type": "openai",
                                               "url": "https://g/v1", "model": "m"}).status_code == 409

    # list
    assert [d["name"] for d in client.get("/api/delegates").json()["delegates"]] == ["opus"]

    # update missing → 404
    assert client.put("/api/delegates/nope", json={"type": "openai", "url": "https://g/v1",
                                                    "model": "m"}).status_code == 404
    # update ok
    r = client.put("/api/delegates/opus", json={"type": "openai", "url": "https://g/v1",
                                                "model": "m2"})
    assert r.status_code == 200
    assert client.get("/api/delegates").json()["delegates"][0]["model"] == "m2"

    # delete
    assert client.request("DELETE", "/api/delegates/opus").status_code == 200
    assert client.get("/api/delegates").json()["delegates"] == []


def test_create_invalid_returns_400(client):
    assert client.post("/api/delegates", json={"name": "x", "type": "nope"}).status_code == 400
    assert client.post("/api/delegates", json={"name": "y", "type": "openai",
                                               "url": "https://g/v1"}).status_code == 400  # no model


def test_test_endpoint_acp_probe(client):
    import sys
    # acp probe is local (binary-on-PATH + workdir exists) — point at the python exe.
    r = client.post("/api/delegates/test", json={"name": "t", "type": "acp",
                                                 "command": sys.executable, "workdir": "/tmp"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_endpoint_unknown_type_400(client):
    assert client.post("/api/delegates/test", json={"type": "nope"}).status_code == 400


def test_test_endpoint_probes_saved_delegate_by_name(client):
    # The per-row Test button sends only {name, type}; the endpoint must probe the
    # STORED config (command/workdir), not fail on the missing fields.
    import sys
    client.post("/api/delegates", json={"name": "proto", "type": "acp",
                                        "command": sys.executable, "workdir": "/tmp"})
    r = client.post("/api/delegates/test", json={"name": "proto", "type": "acp"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_public_view_redacts_secrets_including_nested_env():
    raw = {
        "name": "proto", "type": "acp", "command": "proto", "workdir": "/tmp",
        "env": {"HOME": "/h", "OPENAI_BASE_URL": "https://g/v1", "OPENAI_API_KEY": "sk-LEAK"},
    }
    view = api._public_view(raw)
    assert "sk-LEAK" not in str(view)               # nested env secret redacted
    assert view["env"]["OPENAI_API_KEY"] == "***"
    assert view["env"]["HOME"] == "/h"              # non-secret env preserved


def test_public_view_drops_top_level_secrets():
    raw = {"name": "o", "type": "openai", "url": "https://g/v1", "model": "m", "api_key": "sk-X"}
    view = api._public_view(raw)
    assert "sk-X" not in str(view) and "api_key" not in view
    raw2 = {"name": "h", "type": "a2a", "url": "https://h/a2a",
            "auth": {"scheme": "bearer", "token": "SEKRET-TOKEN"}}
    view2 = api._public_view(raw2)
    assert "SEKRET-TOKEN" not in str(view2) and view2["auth"] == {"scheme": "bearer"}
