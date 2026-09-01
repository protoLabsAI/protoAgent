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

    written: dict = {"entries": None, "secrets": None, "extra": None}

    def _fake_write(entries, secrets, extra=None):
        written["entries"] = entries
        written["secrets"] = secrets
        written["extra"] = extra

    monkeypatch.setattr("operator_api.provider_routes._write_providers", _fake_write)
    # This focused route fixture has no running server. Keep it on the disk-writer
    # fallback even when another test in the process has populated the host singleton.
    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", None)
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


def test_add_uses_the_live_transactional_applier_when_the_server_wires_it(client, monkeypatch):
    _install(monkeypatch, **BASE)
    seen: dict = {}

    def _apply(updates):
        seen.update(updates)
        return True, ["reloaded"]

    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", _apply)
    r = client.post(
        "/api/config/providers",
        json={"id": "claude", "type": "anthropic-oauth", "label": "Claude"},
    )
    assert r.status_code == 200
    assert seen["providers"][-1] == {"id": "claude", "type": "anthropic-oauth", "label": "Claude"}
    assert client.written["entries"] is None  # the fallback writer was not used


def test_add_is_visible_to_an_immediate_get_after_the_live_apply(client, monkeypatch):
    _install(monkeypatch, **BASE)

    def _apply(updates):
        # Model the server's successful reload contract: STATE is swapped before the
        # route returns. This is the regression v0.150 missed — POST said success while
        # the following GET still read the old registry and the new row vanished.
        monkeypatch.setattr(STATE, "graph_config", LangGraphConfig.from_dict({**BASE, **updates}), raising=False)
        return True, ["reloaded"]

    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", _apply)
    r = client.post(
        "/api/config/providers",
        json={"id": "new-gw", "type": "openai-compat", "base_url": "https://new/v1", "api_key": "sk-1"},
    )
    assert r.status_code == 200
    providers = client.get("/api/config/providers").json()["providers"]
    assert [p["id"] for p in providers] == ["prod-gateway", "local-vllm", "new-gw"]
    assert next(p for p in providers if p["id"] == "new-gw")["has_key"] is True


def test_provider_write_survives_a_fresh_config_load(tmp_path, monkeypatch):
    from graph import config_io
    from operator_api.provider_routes import _write_providers

    config_path = tmp_path / "langgraph-config.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: config_path)
    monkeypatch.setattr(config_io, "secrets_yaml_path", lambda: secrets_path)
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})

    _write_providers(
        [{"id": "new-gw", "type": "openai-compat", "base_url": "https://new/v1"}],
        {"new-gw": "sk-1"},
    )
    fresh = LangGraphConfig.from_yaml(config_path)
    provider = fresh.provider_by_id("new-gw")
    assert provider is not None
    assert provider.base_url == "https://new/v1"
    assert provider.api_key == "sk-1"


def test_add_reports_a_live_reload_failure(client, monkeypatch):
    _install(monkeypatch, **BASE)
    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", lambda _updates: (False, ["graph rebuild failed"]))
    r = client.post("/api/config/providers", json={"id": "codex", "type": "openai-codex"})
    assert r.status_code == 400
    assert r.json()["detail"] == "graph rebuild failed"


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


def test_delete_refuses_the_migrated_gateway_implicitly_named_by_a_bare_model(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[{"id": "gateway", "type": "openai-compat", "base_url": "https://gateway/v1"}],
        model={"name": "protolabs/reasoning"},
    )
    listed = client.get("/api/config/providers").json()["providers"][0]
    assert listed["in_use_by"] == ["model.name=protolabs/reasoning (implicit gateway)"]
    r = client.delete("/api/config/providers/gateway")
    assert r.status_code == 409
    assert "implicit gateway" in r.json()["detail"]
    assert client.written["entries"] is None


def test_native_subscription_lead_does_not_mark_a_bare_model_as_using_the_gateway(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "gateway", "type": "openai-compat", "base_url": "https://gateway/v1"},
            {"id": "anthropic-oauth", "type": "anthropic-oauth"},
        ],
        model={"name": "claude-opus-4-6", "provider": "anthropic-oauth"},
    )
    providers = client.get("/api/config/providers").json()["providers"]
    gateway = next(p for p in providers if p["id"] == "gateway")
    assert gateway["in_use_by"] == []


def test_delete_requires_explicit_confirmation_for_the_last_unused_connection(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[{"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"}],
        model={"name": "acp:codex"},
    )
    r = client.delete("/api/config/providers/local-vllm")
    assert r.status_code == 409
    assert "last model connection" in r.json()["detail"]
    assert client.written["entries"] is None

    r = client.delete("/api/config/providers/local-vllm?confirm_last=true")
    assert r.status_code == 200
    assert client.written["entries"] == []


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


# ── structured references + the `releases` DELETE body (bd-neiz) ────────────────
#
# The bare/no-body DELETE keeps refusing an in-use connection (above); these cover the
# structured `in_use` the Providers panel needs and the `releases` body that lets the
# operator repoint/clear those slots in the SAME transaction that removes the connection.


def _apply_spy(monkeypatch, seen):
    def _apply(updates):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["updates"] = updates
        return True, ["reloaded"]

    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", _apply)
    return seen


def test_list_reports_structured_references_alongside_the_strings(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "prod-gateway:reasoning", "favorites": ["prod-gateway:a", "prod-gateway:c"]},
        routing={"aux_model": "prod-gateway:aux"},
        subagents={"researcher": {"model": "prod-gateway:sub"}},
    )
    providers = client.get("/api/config/providers").json()["providers"]
    prod = next(p for p in providers if p["id"] == "prod-gateway")
    by_key = {e["key"]: e for e in prod["in_use"]}
    # The lead model is a slot, and the ONE reference that can never be cleared.
    assert by_key["model.name"] == {
        "key": "model.name",
        "value": "prod-gateway:reasoning",
        "kind": "slot",
        "clearable": False,
    }
    assert by_key["routing.aux_model"] == {
        "key": "routing.aux_model",
        "value": "prod-gateway:aux",
        "kind": "slot",
        "clearable": True,
    }
    # Favorites collapse to ONE entry whose value is the matching favorites (not the rest).
    assert by_key["model.favorites"] == {
        "key": "model.favorites",
        "value": ["prod-gateway:a", "prod-gateway:c"],
        "kind": "favorite",
        "clearable": True,
    }
    # Each subagent tier is its own entry, keyed subagents.<name>.model.
    assert by_key["subagents.researcher.model"] == {
        "key": "subagents.researcher.model",
        "value": "prod-gateway:sub",
        "kind": "subagent",
        "clearable": True,
    }
    # The display strings are unchanged and still carried alongside.
    assert "model.name=prod-gateway:reasoning" in prod["in_use_by"]
    assert "subagents.researcher.model=prod-gateway:sub" in prod["in_use_by"]
    # An unused connection reports empty on both.
    local = next(p for p in providers if p["id"] == "local-vllm")
    assert local["in_use"] == [] and local["in_use_by"] == []


def test_list_reports_the_implicit_gateway_lead_as_a_model_name_reference(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[{"id": "gateway", "type": "openai-compat", "base_url": "https://gateway/v1"}],
        model={"name": "protolabs/reasoning"},
    )
    row = client.get("/api/config/providers").json()["providers"][0]
    assert row["in_use"] == [
        {"key": "model.name", "value": "protolabs/reasoning", "kind": "slot", "clearable": False}
    ]
    assert row["in_use_by"] == ["model.name=protolabs/reasoning (implicit gateway)"]


def test_delete_with_releases_resolves_every_slot_in_one_apply(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "prod-gateway:reasoning", "favorites": ["prod-gateway:fav"]},
        routing={"aux_model": "prod-gateway:aux"},
        subagents={"researcher": {"model": "prod-gateway:sub"}},
    )
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={
            "releases": {
                "model.name": "local-vllm:reasoning",
                "routing.aux_model": "local-vllm:aux",
                "subagents.researcher.model": "local-vllm:sub",
                "model.favorites": None,
            }
        },
    )
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "removed": "prod-gateway",
        "released": ["model.favorites", "model.name", "routing.aux_model", "subagents.researcher.model"],
    }
    # EXACTLY one transactional apply carrying BOTH halves.
    assert seen["calls"] == 1
    updates = seen["updates"]
    assert [e["id"] for e in updates["providers"]] == ["local-vllm"]  # pid gone
    assert updates["model"]["name"] == "local-vllm:reasoning"
    assert updates["model"]["favorites"] == []  # the only favorite was prod-gateway's → cleared
    assert updates["routing"]["aux_model"] == "local-vllm:aux"
    assert updates["subagents"] == {"researcher": {"model": "local-vllm:sub"}}
    assert client.written["entries"] is None  # the fallback disk writer was never touched


def test_delete_with_releases_that_leave_a_reference_is_refused(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "prod-gateway:reasoning"},
        routing={"aux_model": "prod-gateway:aux"},
    )
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"model.name": "local-vllm:reasoning"}},  # aux left dangling
    )
    assert r.status_code == 409
    assert "routing.aux_model=prod-gateway:aux" in r.json()["detail"]
    assert seen.get("calls", 0) == 0  # nothing applied
    assert client.written["entries"] is None


def test_delete_release_validation_rejects_bad_bodies(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "prod-gateway:reasoning", "favorites": ["prod-gateway:fav"]},
    )
    seen = _apply_spy(monkeypatch, {})

    def _delete(releases):
        return client.request("DELETE", "/api/config/providers/prod-gateway", json={"releases": releases})

    # A key that isn't a reference of this connection at all.
    assert _delete({"routing.aux_model": None}).status_code == 400
    # A target on a provider that doesn't exist.
    assert _delete({"model.name": "ghost:reasoning"}).status_code == 400
    # A target on the connection being removed.
    assert _delete({"model.name": "prod-gateway:reasoning"}).status_code == 400
    # A malformed target (no <provider>:<model> split).
    assert _delete({"model.name": "reasoning"}).status_code == 400
    # Clearing the lead model — it must be repointed, not emptied.
    assert _delete({"model.name": None}).status_code == 400
    # Favorites can only be cleared, never repointed with a string.
    assert _delete({"model.favorites": "local-vllm:fav"}).status_code == 400
    # None of the rejected bodies wrote anything.
    assert seen.get("calls", 0) == 0
    assert client.written["entries"] is None


def test_delete_release_clears_only_this_connections_favorites(client, monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "local-vllm:main", "favorites": ["prod-gateway:x", "local-vllm:y", "prod-gateway:z"]},
    )
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE", "/api/config/providers/prod-gateway", json={"releases": {"model.favorites": None}}
    )
    assert r.status_code == 200
    assert seen["calls"] == 1
    # Only local-vllm's favorite survives; both prod-gateway favorites are gone.
    assert seen["updates"]["model"]["favorites"] == ["local-vllm:y"]
    assert [e["id"] for e in seen["updates"]["providers"]] == ["local-vllm"]


def test_delete_release_repoints_and_clears_a_subagent_model(client, monkeypatch):
    def _setup():
        _install(
            monkeypatch,
            providers=[
                {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
                {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
            ],
            model={"name": "local-vllm:main"},
            subagents={"researcher": {"model": "prod-gateway:sub"}},
        )

    seen = _apply_spy(monkeypatch, {})

    # Repoint — the written shape is EXACTLY what the settings writer accepts for a
    # subagent model (subagents.<name>.model nested), alongside the trimmed providers.
    _setup()
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"subagents.researcher.model": "local-vllm:sub"}},
    )
    assert r.status_code == 200
    assert seen["updates"]["subagents"] == {"researcher": {"model": "local-vllm:sub"}}
    assert [e["id"] for e in seen["updates"]["providers"]] == ["local-vllm"]

    # Clear — the same key, emptied.
    _setup()
    seen.clear()
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"subagents.researcher.model": None}},
    )
    assert r.status_code == 200
    assert seen["updates"]["subagents"] == {"researcher": {"model": ""}}


def test_delete_release_no_host_writes_providers_and_slots_in_one_save(monkeypatch):
    """No host wired: providers (minus pid) AND the released slots land in a SINGLE
    save_yaml_doc, through the same YAML-doc seam the disk fallback already uses."""
    import graph.config_io as config_io

    app = FastAPI()
    register_provider_routes(app)
    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", None)
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "prod-gateway:reasoning"},
        routing={"aux_model": "prod-gateway:aux"},
    )

    doc = {
        "providers": [
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        "model": {"name": "prod-gateway:reasoning"},
        "routing": {"aux_model": "prod-gateway:aux"},
    }
    saved: dict = {"count": 0, "doc": None}
    monkeypatch.setattr(config_io, "load_yaml_doc", lambda path=None: doc)

    def _save(d, path=None):
        saved["count"] += 1
        saved["doc"] = d

    monkeypatch.setattr(config_io, "save_yaml_doc", _save)

    r = TestClient(app).request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"model.name": "local-vllm:reasoning", "routing.aux_model": None}},
    )
    assert r.status_code == 200
    assert saved["count"] == 1  # providers + released slots in ONE save
    assert [e["id"] for e in saved["doc"]["providers"]] == ["local-vllm"]
    assert saved["doc"]["model"]["name"] == "local-vllm:reasoning"
    assert saved["doc"]["routing"]["aux_model"] == ""  # cleared


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


@pytest.mark.parametrize(
    ("connection_id", "provider_type", "models"),
    [
        ("claude", "anthropic-oauth", ["claude-sonnet-5"]),
        ("codex", "openai-codex", ["gpt-5.6-sol"]),
    ],
)
def test_models_probe_routes_subscription_connections_by_type(
    client, monkeypatch, connection_id, provider_type, models
):
    _install(
        monkeypatch,
        providers=[{"id": connection_id, "type": provider_type}],
        model={"name": f"{connection_id}:{models[0]}"},
    )
    seen: dict = {}

    def _fake_list(ptype, cfg):
        seen.update(ptype=ptype, ids=cfg.provider_ids())
        return models, ""

    monkeypatch.setattr("graph.providers.discovery.list_provider_models", _fake_list)
    r = client.post(f"/api/config/providers/{connection_id}/models")
    assert r.status_code == 200
    assert r.json() == {"models": models, "error": ""}
    assert seen == {"ptype": provider_type, "ids": [connection_id]}


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
