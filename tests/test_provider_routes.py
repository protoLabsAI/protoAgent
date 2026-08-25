"""Provider registry CRUD routes (ADR 0106 S3).

The two invariants worth testing are the ones a generic settings form could not
express: an id is immutable once a slot may reference it, and a connection still in
use cannot be deleted out from under those slots.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from operator_api.provider_routes import register_provider_routes
from runtime.state import STATE


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    register_provider_routes(app)

    written: dict = {"entries": None, "secrets": None}

    def _fake_write(entries, secrets):
        written["entries"] = entries
        written["secrets"] = secrets

    monkeypatch.setattr("operator_api.provider_routes._write_providers", _fake_write)
    c = TestClient(app)
    c.written = written  # type: ignore[attr-defined]
    return c


def _install(monkeypatch, **doc):
    cfg = LangGraphConfig.from_dict(doc)
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    return cfg


BASE = {
    "providers": [
        {"id": "prod-gateway", "type": "openai-compat", "label": "Production", "base_url": "https://prod/v1"},
        {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
    ],
    "model": {"name": "prod-gateway:protolabs/reasoning"},
}


def test_list_redacts_keys_and_reports_use(client, monkeypatch):
    _install(monkeypatch, **BASE)
    body = client.get("/api/config/providers").json()["providers"]
    ids = [p["id"] for p in body]
    assert ids == ["prod-gateway", "local-vllm"]
    assert all("api_key" not in p for p in body)  # never echoed back
    prod = next(p for p in body if p["id"] == "prod-gateway")
    assert prod["display"] == "Production"
    assert "model.name=prod-gateway:protolabs/reasoning" in prod["in_use_by"]
    assert next(p for p in body if p["id"] == "local-vllm")["in_use_by"] == []


def test_add_rejects_an_id_the_grammar_could_not_parse(client, monkeypatch):
    _install(monkeypatch, **BASE)
    r = client.post("/api/config/providers", json={"id": "has:colon"})
    assert r.status_code == 400
    assert "grammar reserves" in r.json()["detail"]


def test_add_rejects_a_duplicate_and_an_unknown_type(client, monkeypatch):
    _install(monkeypatch, **BASE)
    assert client.post("/api/config/providers", json={"id": "local-vllm"}).status_code == 409
    assert client.post("/api/config/providers", json={"id": "x", "type": "nope"}).status_code == 400


def test_add_routes_the_key_to_the_secrets_overlay(client, monkeypatch):
    _install(monkeypatch, **BASE)
    r = client.post(
        "/api/config/providers",
        json={"id": "new-gw", "type": "openai-compat", "base_url": "https://new/v1", "api_key": "sk-1"},
    )
    assert r.status_code == 200
    entries = client.written["entries"]
    assert [e["id"] for e in entries] == ["prod-gateway", "local-vllm", "new-gw"]
    # The key goes to secrets.yaml, never into the config document.
    assert all("api_key" not in e for e in entries)
    assert client.written["secrets"] == {"new-gw": "sk-1"}


def test_patch_edits_the_label_but_there_is_no_way_to_change_an_id(client, monkeypatch):
    _install(monkeypatch, **BASE)
    r = client.patch("/api/config/providers/local-vllm", json={"label": "Local vLLM", "id": "renamed"})
    assert r.status_code == 200
    entries = {e["id"]: e for e in client.written["entries"]}
    assert "renamed" not in entries  # the field is not on the model — silently ignored
    assert entries["local-vllm"]["label"] == "Local vLLM"


def test_patch_without_a_key_leaves_the_stored_one_alone(client, monkeypatch):
    _install(monkeypatch, **BASE)
    client.patch("/api/config/providers/local-vllm", json={"base_url": "http://moved:9000/v1"})
    assert client.written["secrets"] == {}


def test_delete_refuses_a_connection_a_slot_still_names(client, monkeypatch):
    _install(monkeypatch, **BASE)
    r = client.delete("/api/config/providers/prod-gateway")
    assert r.status_code == 409
    assert "model.name=prod-gateway:protolabs/reasoning" in r.json()["detail"]
    assert client.written["entries"] is None  # nothing written


def test_delete_removes_an_unused_connection(client, monkeypatch):
    _install(monkeypatch, **BASE)
    r = client.delete("/api/config/providers/local-vllm")
    assert r.status_code == 200
    assert [e["id"] for e in client.written["entries"]] == ["prod-gateway"]


def test_delete_sees_a_reference_from_any_slot_not_just_the_main_model(client, monkeypatch):
    _install(monkeypatch, **{**BASE, "routing": {"aux_model": "local-vllm:qwen3-32b"}})
    r = client.delete("/api/config/providers/local-vllm")
    assert r.status_code == 409
    assert "routing.aux_model=local-vllm:qwen3-32b" in r.json()["detail"]


def test_delete_sees_a_reference_from_favorites(client, monkeypatch):
    _install(monkeypatch, **{**BASE, "model": {**BASE["model"], "favorites": ["local-vllm:qwen3-32b"]}})
    assert client.delete("/api/config/providers/local-vllm").status_code == 409


def test_unknown_ids_are_404_not_500(client, monkeypatch):
    _install(monkeypatch, **BASE)
    assert client.patch("/api/config/providers/nope", json={"label": "x"}).status_code == 404
    assert client.delete("/api/config/providers/nope").status_code == 404
    assert client.post("/api/config/providers/nope/models").status_code == 404


def test_models_probe_uses_the_connection_own_endpoint(client, monkeypatch):
    _install(monkeypatch, **BASE)
    seen: dict = {}

    def _fake_list(base, key, **kw):
        seen["base"], seen["key"] = base, key
        return ["m1", "m2"], ""

    import graph.config_io as cio

    monkeypatch.setattr(cio, "list_gateway_models", _fake_list, raising=False)
    r = client.post("/api/config/providers/local-vllm/models")
    assert r.json()["models"] == ["m1", "m2"]
    assert seen["base"] == "http://localhost:8000/v1"


# ── first run writes a connection, not the retired triple (ADR 0106 S6) ────────


def test_setup_splits_a_connection_key_into_the_secrets_overlay():
    """A key the wizard collects must never reach the tracked YAML — that file gets
    exported, backed up and forked. Same rule model.api_key always had, but the registry
    is a LIST, which `secret_paths()`'s (section, key) pairs cannot describe."""
    from graph.config_io import split_secret_updates

    main, secrets = split_secret_updates(
        {
            "providers": [
                {"id": "gateway", "type": "openai-compat", "base_url": "https://gw/v1", "api_key": "sk-1"},
                {"id": "local", "type": "openai-compat", "base_url": "http://l/v1"},
            ],
            "model": {"name": "gateway:protolabs/reasoning"},
        }
    )
    assert secrets == {"providers": {"gateway": "sk-1"}}
    assert all("api_key" not in entry for entry in main["providers"])
    assert main["providers"][0]["base_url"] == "https://gw/v1"  # everything else survives


def test_a_blank_connection_key_leaves_the_stored_one_alone():
    """Blank means "didn't re-enter it", the rule every other secret follows."""
    from graph.config_io import split_secret_updates

    _, secrets = split_secret_updates({"providers": [{"id": "gateway", "api_key": "   "}]})
    assert secrets == {}
