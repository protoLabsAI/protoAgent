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


def _resolve(updates):
    """The live applier resolves a read-modify-write callable against the committed
    config, inside its lock (#2743) — a fake applier must do the same."""
    return updates(STATE.graph_config) if callable(updates) else updates


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
        updates = _resolve(updates)
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
        updates = _resolve(updates)
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


KEYED = {"model": {"name": "gateway:gpt-4o", "api_base": "https://prod.example/v1", "api_key": "stored-key"}}


def test_patch_cannot_move_a_keyed_connection_to_another_server_without_its_key(client, monkeypatch):
    """A blank key keeps the stored one — so re-aiming the endpoint alone would send that key
    to the new address on the next model list or turn."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _install(monkeypatch, **KEYED)
    for moved in ("https://elsewhere.example/v1", "http://prod.example/v1", "https://prod.example:8443/v1"):
        r = client.patch("/api/config/providers/gateway", json={"base_url": moved})
        assert r.status_code == 400, moved
        assert "Enter its API key again" in r.json()["detail"]
    assert client.written["entries"] is None  # nothing was written


def test_patch_moves_a_keyed_connection_when_the_key_is_given_again(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _install(monkeypatch, **KEYED)
    # …and the key is stored trimmed: a paste with a stray newline is still the key.
    r = client.patch("/api/config/providers/gateway", json={"base_url": "https://elsewhere.example/v1", "api_key": " new\n"})
    assert r.status_code == 200
    assert client.written["secrets"] == {"gateway": "new"}


def test_patch_on_the_same_server_keeps_the_stored_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _install(monkeypatch, **KEYED)
    for same in ("https://prod.example/v2", "https://PROD.example/v1/", "https://prod.example/v1"):
        assert client.patch("/api/config/providers/gateway", json={"base_url": same}).status_code == 200, same


def test_patch_the_migrated_gateway_label_keeps_its_endpoint_and_key(client, monkeypatch):
    """#4597: the migrated gateway has a null endpoint (the SDK default) and its key in the
    legacy `model.api_key`. A console label edit must not strand either — the GET lists its
    endpoint as the explicit `sdk-default` sentinel, so the panel round-trips that (not "",
    which used to remove it), and the write lifts the key into `secrets.providers.gateway`.
    The env (`OPENAI_BASE_URL`) never leaks into the saved endpoint."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-host.example/v1")  # must NOT be persisted
    _install(monkeypatch, model={"name": "gateway:gpt-4o", "api_base": None, "api_key": "test-key"})

    # The GET the panel seeds its draft from shows the sentinel, never a blank or a URL.
    listed = client.get("/api/config/providers").json()["providers"][0]
    assert listed["base_url"] == "sdk-default"

    r = client.patch("/api/config/providers/gateway", json={"label": "My gateway"})
    assert r.status_code == 200
    written = {e["id"]: e for e in client.written["entries"]}
    assert written["gateway"]["label"] == "My gateway"
    assert written["gateway"]["base_url"] == "sdk-default"  # endpoint survives as the sentinel
    assert client.written["secrets"] == {"gateway": "test-key"}  # key preserved, not stranded


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


def test_memory_selector_reference_is_listed_and_blocks_provider_deletion(client, monkeypatch):
    _install(monkeypatch, **BASE, memory={"select_model": "local-vllm:selector"})
    providers = client.get("/api/config/providers").json()["providers"]
    local = next(p for p in providers if p["id"] == "local-vllm")
    assert local["in_use_by"] == ["memory.select_model=local-vllm:selector"]
    assert local["in_use"] == [{
        "key": "memory.select_model", "value": "local-vllm:selector",
        "kind": "slot", "clearable": True,
    }]
    response = client.delete("/api/config/providers/local-vllm")
    assert response.status_code == 409
    assert "memory.select_model=local-vllm:selector" in response.json()["detail"]
    assert client.written["entries"] is None


@pytest.mark.parametrize("target", [None, "prod-gateway:selector"])
@pytest.mark.parametrize("live_apply", [False, True])
def test_delete_releases_memory_selector_with_provider(client, monkeypatch, target, live_apply):
    _install(monkeypatch, **BASE, memory={"select_model": "local-vllm:selector"})
    seen = _apply_spy(monkeypatch, {}) if live_apply else {}
    response = client.request(
        "DELETE", "/api/config/providers/local-vllm",
        json={"releases": {"memory.select_model": target}},
    )
    assert response.status_code == 200
    assert response.json()["released"] == ["memory.select_model"]
    if live_apply:
        assert seen["calls"] == 1
        updates = seen["updates"]
        entries = updates["providers"]
        assert client.written["entries"] is None
    else:
        updates = client.written["extra"]
        entries = client.written["entries"]
    assert updates["memory"]["select_model"] == (target or "")
    assert [p["id"] for p in entries] == ["prod-gateway"]


# ── structured references + the `releases` DELETE body (bd-neiz) ────────────────
#
# The bare/no-body DELETE keeps refusing an in-use connection (above); these cover the
# structured `in_use` the Providers panel needs and the `releases` body that lets the
# operator repoint/clear those slots in the SAME transaction that removes the connection.


def _apply_spy(monkeypatch, seen):
    def _apply(updates):
        updates = _resolve(updates)
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
    assert row["in_use"] == [{"key": "model.name", "value": "protolabs/reasoning", "kind": "slot", "clearable": False}]
    assert row["in_use_by"] == ["model.name=protolabs/reasoning (implicit gateway)"]


# ── ADR 0133: a model slot may be an ordered chain; a connection named by ANY entry ──
# is in use. `str([...])` would render the chain as "['conn:model']" and the delete guard's
# startswith check would miss it, so a connection referenced only by a chain entry could be
# deleted out from under the slot, leaving a dangling route.


def _chain_fixture(monkeypatch):
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "local-vllm:lead"},
        routing={"aux_model": ["local-vllm:fast", "prod-gateway:aux"]},  # prod named by the FALLBACK
    )


def test_delete_sees_a_reference_from_a_non_primary_chain_entry(client, monkeypatch):
    _chain_fixture(monkeypatch)
    prod = next(p for p in client.get("/api/config/providers").json()["providers"] if p["id"] == "prod-gateway")
    # The chain entry naming prod is listed, and collapses to ONE structured slot entry
    # whose value is the matching entry (the scalar shape, since only one entry matched).
    assert "routing.aux_model=prod-gateway:aux" in prod["in_use_by"]
    by_key = {e["key"]: e for e in prod["in_use"]}
    assert by_key["routing.aux_model"] == {
        "key": "routing.aux_model",
        "value": "prod-gateway:aux",
        "kind": "slot",
        "clearable": True,
    }
    # …and the delete is refused, not allowed through as it was when the chain stringified.
    r = client.delete("/api/config/providers/prod-gateway")
    assert r.status_code == 409
    assert "routing.aux_model=prod-gateway:aux" in r.json()["detail"]


def test_delete_releases_a_chain_slot_by_repointing_it(client, monkeypatch):
    _chain_fixture(monkeypatch)
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"routing.aux_model": "local-vllm:aux"}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "prod-gateway", "released": ["routing.aux_model"]}
    # Repointing touches ONLY the entry that named prod-gateway; the healthy local-vllm:fast
    # primary survives, so the chain becomes [local-vllm:fast, local-vllm:aux] — a repoint
    # never discards a fallback that pointed elsewhere.
    assert seen["updates"]["routing"]["aux_model"] == ["local-vllm:fast", "local-vllm:aux"]


def test_delete_clears_only_the_matching_chain_entry(client, monkeypatch):
    _chain_fixture(monkeypatch)  # routing.aux_model = [local-vllm:fast, prod-gateway:aux]
    seen = _apply_spy(monkeypatch, {})
    # Clearing (target null) a chain slot drops only the prod-gateway entry; the local-vllm
    # fallback is kept, so the slot stays a usable one-entry chain rather than being emptied.
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"routing.aux_model": None}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "prod-gateway", "released": ["routing.aux_model"]}
    assert seen["updates"]["routing"]["aux_model"] == ["local-vllm:fast"]


@pytest.mark.parametrize(
    "slot", ["memory.harvest_model", "memory.facts_model", "memory.gist_model", "knowledge.image_describe_model"]
)
def test_delete_sees_a_reference_from_a_newly_routed_slot(client, monkeypatch, slot):
    # ADR 0133 added four route-bearing model roles; the delete in-use guard (`_SLOT_ATTRS`)
    # must see each, or a connection referenced ONLY by one is reported unused and removed,
    # leaving a dangling route.
    section, key = slot.split(".", 1)
    _install(
        monkeypatch,
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"name": "local-vllm:lead"},  # the lead names local-vllm, so prod is used ONLY by `slot`
        **{section: {key: "prod-gateway:worker"}},
    )
    prod = next(p for p in client.get("/api/config/providers").json()["providers"] if p["id"] == "prod-gateway")
    assert f"{slot}=prod-gateway:worker" in prod["in_use_by"]
    r = client.delete("/api/config/providers/prod-gateway")
    assert r.status_code == 409
    assert f"{slot}=prod-gateway:worker" in r.json()["detail"]


def test_delete_with_releases_that_leave_a_chain_entry_is_refused(client, monkeypatch):
    _chain_fixture(monkeypatch)
    seen = _apply_spy(monkeypatch, {})
    # Clearing the UNRELATED model.name leaves the aux chain's prod entry dangling.
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {}},
    )
    assert r.status_code == 409
    assert "routing.aux_model=prod-gateway:aux" in r.json()["detail"]
    assert seen.get("calls", 0) == 0


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
    r = client.request("DELETE", "/api/config/providers/prod-gateway", json={"releases": {"model.favorites": None}})
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


def test_overlay_subagent_model_pin_is_in_use_and_releasable(client, monkeypatch):
    """A connection named ONLY by a generalised `subagents.<name>.model` overlay (ADR 0128
    S1b) — not a typed tier — must count as in use: reported, refused on a bare delete, and
    repointable in the same transaction. Else the connection is deletable and the pin dangles
    until that subagent's dispatch."""
    doc = {
        "providers": [
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        "model": {"name": "local-vllm:main"},  # prod-gateway is referenced ONLY by the overlay
        "subagents": {"review-finder": {"model": "prod-gateway:overlaid"}},
    }
    _install(monkeypatch, **doc)

    prod = next(p for p in client.get("/api/config/providers").json()["providers"] if p["id"] == "prod-gateway")
    by_key = {e["key"]: e for e in prod["in_use"]}
    assert by_key["subagents.review-finder.model"] == {
        "key": "subagents.review-finder.model",
        "value": "prod-gateway:overlaid",
        "kind": "subagent",
        "clearable": True,
    }
    assert "subagents.review-finder.model=prod-gateway:overlaid" in prod["in_use_by"]

    seen = _apply_spy(monkeypatch, {})
    # A bare delete is refused and names the overlay pin.
    bare = client.request("DELETE", "/api/config/providers/prod-gateway")
    assert bare.status_code == 409
    assert "subagents.review-finder.model=prod-gateway:overlaid" in bare.json()["detail"]
    assert seen.get("calls", 0) == 0

    # Repoint the overlay pin in the same transaction that removes the connection.
    _install(monkeypatch, **doc)
    r = client.request(
        "DELETE",
        "/api/config/providers/prod-gateway",
        json={"releases": {"subagents.review-finder.model": "local-vllm:sub"}},
    )
    assert r.status_code == 200
    assert seen["updates"]["subagents"] == {"review-finder": {"model": "local-vllm:sub"}}
    assert [e["id"] for e in seen["updates"]["providers"]] == ["local-vllm"]


# ── ADR 0133 M5: a subagent tier pin is a chain too; a connection named by ANY entry is in use.
# Before the fix the subagents dict stringified the pin (`str(['conn:model'])`), hiding a
# chain-only reference from the delete guard's startswith check — a connection the tier's PRIMARY
# still routed through could be deleted out from under it.


def _chain_providers():
    return [
        {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
        {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
    ]


def test_delete_sees_and_releases_a_subagent_chain_primary(client, monkeypatch):
    # researcher pinned to a two-model chain: prod-gateway is the PRIMARY, local-vllm only the
    # fallback; model.name is on local-vllm, so prod is named ONLY by the subagent chain.
    _install(
        monkeypatch,
        providers=_chain_providers(),
        model={"name": "local-vllm:lead"},
        subagents={"researcher": {"model": ["prod-gateway:sub", "local-vllm:sub-fb"]}},
    )
    prod = next(p for p in client.get("/api/config/providers").json()["providers"] if p["id"] == "prod-gateway")
    # One entry matched, so the in_use entry keeps the scalar shape; the bare delete is refused.
    assert "subagents.researcher.model=prod-gateway:sub" in prod["in_use_by"]
    assert {e["key"]: e for e in prod["in_use"]}["subagents.researcher.model"] == {
        "key": "subagents.researcher.model",
        "value": "prod-gateway:sub",
        "kind": "subagent",
        "clearable": True,
    }
    assert client.delete("/api/config/providers/prod-gateway").status_code == 409
    # Releasing repoints only the prod-gateway PRIMARY; the healthy local-vllm:sub-fb fallback
    # survives in order, so the tier stays a usable chain — a scalar release would have dropped it.
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE", "/api/config/providers/prod-gateway", json={"releases": {"subagents.researcher.model": "local-vllm:sub"}}
    )
    assert r.status_code == 200
    assert seen["updates"]["subagents"] == {"researcher": {"model": ["local-vllm:sub", "local-vllm:sub-fb"]}}
    assert [e["id"] for e in seen["updates"]["providers"]] == ["local-vllm"]


def test_delete_clears_only_the_matching_subagent_chain_entry(client, monkeypatch):
    # researcher = [local-vllm:fast, prod-gateway:sub]: prod named ONLY by the fallback entry.
    _install(
        monkeypatch,
        providers=_chain_providers(),
        model={"name": "local-vllm:lead"},
        subagents={"researcher": {"model": ["local-vllm:fast", "prod-gateway:sub"]}},
    )
    seen = _apply_spy(monkeypatch, {})
    r = client.request(
        "DELETE", "/api/config/providers/prod-gateway", json={"releases": {"subagents.researcher.model": None}}
    )
    assert r.status_code == 200
    # Clearing drops only the prod-gateway fallback; the local-vllm primary is kept, so the tier
    # stays a usable one-entry chain rather than being emptied.
    assert seen["updates"]["subagents"] == {"researcher": {"model": ["local-vllm:fast"]}}


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


def test_a_device_scope_caller_cannot_choose_a_connection_endpoint(client, monkeypatch):
    """ADR 0121 D4: the endpoint is where every prompt goes. Echoing the stored one is fine."""
    _install(monkeypatch, **BASE)
    monkeypatch.setattr("a2a_impl.auth.open_mode", lambda: False)  # unstamped ⇒ device scope
    assert client.patch("/api/config/providers/local-vllm", json={"base_url": "https://x/v1"}).status_code == 403
    assert client.post("/api/config/providers", json={"id": "x", "base_url": "https://x/v1"}).status_code == 403
    assert client.post("/api/config/providers", json={"id": "y", "api_key": "k"}).status_code == 403  # any add
    assert client.delete("/api/config/providers/local-vllm").status_code == 403  # nor remove one
    assert client.patch("/api/config/providers/local-vllm", json={"api_key": "k"}).status_code == 403  # nor rekey one
    r = client.patch("/api/config/providers/local-vllm", json={"label": "L", "base_url": "http://localhost:8000/v1"})
    assert r.status_code == 200 and client.written["entries"]


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


# ── GET /api/config/model-roles: each role's chain, inherited flag, last fallback ──
# (ADR 0133 M6). Read-only status for Settings › Models (M7).

from types import SimpleNamespace  # noqa: E402

from langchain_core.runnables import RunnableLambda  # noqa: E402

from graph import model_roles  # noqa: E402


@pytest.fixture
def clear_fallbacks(monkeypatch):
    """M2's last-fallback registry is a process-global; isolate it per test."""
    monkeypatch.setattr(model_roles, "_last_fallbacks", {})
    monkeypatch.setattr(model_roles, "_streaks", set())


class _Transient(Exception):
    def __init__(self):
        super().__init__("transient")
        self.status_code = 429


def _record_fallback(monkeypatch, role, field, primary, winner):
    """Drive one real retryable fallback through M2's seam so it records last_fallback(role).

    The record is keyed by role name alone, independent of the config the route reads — so a
    fake-model chain here populates the record the status surface then reports for that role.
    """
    def _boom(_):
        raise _Transient()

    models = {primary: RunnableLambda(_boom), winner: RunnableLambda(lambda _: "ok")}
    monkeypatch.setattr(model_roles, "create_llm", lambda config, model_name, **kw: models[model_name])
    llm = model_roles.create_role_llm(SimpleNamespace(**{field: [primary, winner]}), role)
    assert llm.invoke("x") == "ok"


def test_model_roles_reports_chain_inherited_and_last_fallback(client, monkeypatch, clear_fallbacks):
    _install(monkeypatch, **{**BASE, "routing": {"aux_model": ["prod-gateway:a", "prod-gateway:b"]}})
    roles = {r["role"]: r for r in client.get("/api/config/model-roles").json()["roles"]}

    # The main model's own chain, marked set (not inherited).
    assert roles["main"]["chain"] == ["prod-gateway:protolabs/reasoning"]
    assert roles["main"]["inherited"] is False and roles["main"]["source"] == "main"
    # aux is a two-model chain, set here.
    assert roles["aux"]["chain"] == ["prod-gateway:a", "prod-gateway:b"]
    assert roles["aux"]["inherited"] is False and roles["aux"]["source"] == "aux"
    # A blank aux-family role inherits the WHOLE aux chain (ADR 0133 D2), flagged inherited.
    assert roles["compaction"]["chain"] == ["prod-gateway:a", "prod-gateway:b"]
    assert roles["compaction"]["inherited"] is True and roles["compaction"]["source"] == "aux"
    # No fallback has fired yet.
    assert roles["aux"]["last_fallback"] is None

    # A retryable failure makes aux answer from its fallback; the status surface reports it,
    # reason redacted to the exception type/status (never a provider payload).
    _record_fallback(monkeypatch, "aux", "aux_model", "prod-gateway:a", "prod-gateway:b")
    roles = {r["role"]: r for r in client.get("/api/config/model-roles").json()["roles"]}
    record = roles["aux"]["last_fallback"]
    assert record["from"] == ["prod-gateway:a"] and record["to"] == "prod-gateway:b"
    assert "prod-gateway:a" in record["reason"] and "429" in record["reason"]


def test_model_roles_lists_every_core_role(client, monkeypatch, clear_fallbacks):
    from graph.config import _ROLE_FIELDS

    _install(monkeypatch, **BASE)
    roles = client.get("/api/config/model-roles").json()["roles"]
    assert [r["role"] for r in roles] == list(_ROLE_FIELDS)


def test_model_roles_image_describe_unavailable_without_vision(client, monkeypatch, clear_fallbacks):
    # BASE: the main model is set, model.vision defaults off, image_describe is blank.
    _install(monkeypatch, **BASE)
    describe = next(
        r for r in client.get("/api/config/model-roles").json()["roles"] if r["role"] == "image_describe"
    )
    # A blank describe model inherits the MAIN chain (D2 exception), but the main model can't
    # read images (model.vision off), so image description reports itself unavailable.
    assert describe["chain"] == ["prod-gateway:protolabs/reasoning"]
    assert describe["inherited"] is True and describe["source"] == "main"
    assert describe["status"] == "unavailable" and describe["reason"] == "no-vision"


def test_model_roles_image_describe_available_when_the_main_model_reads_images(client, monkeypatch, clear_fallbacks):
    _install(monkeypatch, **{**BASE, "model": {**BASE["model"], "vision": True}})
    describe = next(
        r for r in client.get("/api/config/model-roles").json()["roles"] if r["role"] == "image_describe"
    )
    # The inherited main model reads images natively → available, no reason reported.
    assert describe["status"] == "ok" and "reason" not in describe
    assert describe["inherited"] is True and describe["source"] == "main"


def test_model_roles_image_describe_available_with_its_own_chain(client, monkeypatch, clear_fallbacks):
    _install(monkeypatch, **{**BASE, "knowledge": {"image_describe_model": "prod-gateway:vision"}})
    describe = next(
        r for r in client.get("/api/config/model-roles").json()["roles"] if r["role"] == "image_describe"
    )
    # A SET describe model is vision-capable by the field's purpose — available and not inherited.
    assert describe["chain"] == ["prod-gateway:vision"]
    assert describe["inherited"] is False and describe["source"] == "image_describe"
    assert describe["status"] == "ok" and "reason" not in describe
