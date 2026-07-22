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
    store.upsert_delegate(
        {"name": "helm", "type": "a2a", "url": "https://h/a2a", "auth": {"scheme": "bearer", "token": "SEKRET"}}
    )
    stored = fake_io["doc"]["delegates"][0]
    assert stored["auth"] == {"scheme": "bearer"}  # token stripped from config
    assert "SEKRET" not in str(stored)
    assert fake_io["secrets"]["delegate_secrets"]["helm.auth.token"] == "SEKRET"


def test_merged_delegates_overlays_secret(fake_io):
    store.upsert_delegate({"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m", "api_key": "K"})
    assert "K" not in str(fake_io["doc"]["delegates"])  # not in tracked config
    merged = store.merged_delegates()
    assert merged[0]["api_key"] == "K"  # overlaid back at load


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
    r = client.post(
        "/api/delegates", json={"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m", "api_key": "K"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    names = [d["name"] for d in r.json()["delegates"]]
    assert names == ["opus"]
    # secret routed, not echoed
    body = r.json()["delegates"][0]
    assert "K" not in str(body) and body["has_secret"] is True
    assert fake_io["secrets"]["delegate_secrets"]["opus.api_key"] == "K"

    # duplicate → 409
    assert (
        client.post(
            "/api/delegates", json={"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}
        ).status_code
        == 409
    )

    # list
    assert [d["name"] for d in client.get("/api/delegates").json()["delegates"]] == ["opus"]

    # update missing → 404
    assert (
        client.put("/api/delegates/nope", json={"type": "openai", "url": "https://g/v1", "model": "m"}).status_code
        == 404
    )
    # update ok
    r = client.put("/api/delegates/opus", json={"type": "openai", "url": "https://g/v1", "model": "m2"})
    assert r.status_code == 200
    assert client.get("/api/delegates").json()["delegates"][0]["model"] == "m2"

    # delete
    assert client.request("DELETE", "/api/delegates/opus").status_code == 200
    assert client.get("/api/delegates").json()["delegates"] == []


def test_create_invalid_returns_400(client):
    assert client.post("/api/delegates", json={"name": "x", "type": "nope"}).status_code == 400
    assert (
        client.post("/api/delegates", json={"name": "y", "type": "openai", "url": "https://g/v1"}).status_code == 400
    )  # no model


def test_test_endpoint_acp_probe(client, monkeypatch):
    import sys

    from plugins.coding_agent.acp_client import AcpClient

    # The acp probe does a real ACP `initialize`-only handshake (#1116/#1300), so mock
    # it — the python exe is on PATH + /tmp exists, but it doesn't speak ACP for real.
    async def _ok(self):
        self._protocol_version = 1

    async def _noop(self):
        pass

    monkeypatch.setattr(AcpClient, "handshake", _ok)
    monkeypatch.setattr(AcpClient, "close", _noop)
    r = client.post(
        "/api/delegates/test", json={"name": "t", "type": "acp", "command": sys.executable, "workdir": "/tmp"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_endpoint_unknown_type_400(client):
    assert client.post("/api/delegates/test", json={"type": "nope"}).status_code == 400


def test_list_includes_health_snapshot(client, monkeypatch):
    import plugins.delegates.health as H

    client.post("/api/delegates", json={"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"})
    monkeypatch.setattr(H, "health_snapshot", lambda: {"opus": {"ok": True, "latency_ms": 12, "detail": "ok"}})
    body = client.get("/api/delegates").json()["delegates"][0]
    assert body["health"]["ok"] is True and body["health"]["latency_ms"] == 12


def test_test_endpoint_probes_saved_delegate_by_name(client, monkeypatch):
    # The per-row Test button sends only {name, type}; the endpoint must probe the
    # STORED config (command/workdir), not fail on the missing fields.
    import sys

    from plugins.coding_agent.acp_client import AcpClient

    async def _ok(self):
        self._protocol_version = 1

    async def _noop(self):
        pass

    monkeypatch.setattr(AcpClient, "handshake", _ok)
    monkeypatch.setattr(AcpClient, "close", _noop)
    client.post("/api/delegates", json={"name": "proto", "type": "acp", "command": sys.executable, "workdir": "/tmp"})
    r = client.post("/api/delegates/test", json={"name": "proto", "type": "acp"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_public_view_redacts_secrets_including_nested_env():
    raw = {
        "name": "proto",
        "type": "acp",
        "command": "proto",
        "workdir": "/tmp",
        "env": {"HOME": "/h", "OPENAI_BASE_URL": "https://g/v1", "OPENAI_API_KEY": "sk-LEAK"},
    }
    view = api._public_view(raw)
    assert "sk-LEAK" not in str(view)  # nested env secret redacted
    assert view["env"]["OPENAI_API_KEY"] == "***"
    assert view["env"]["HOME"] == "/h"  # non-secret env preserved


def test_public_view_drops_top_level_secrets():
    raw = {"name": "o", "type": "openai", "url": "https://g/v1", "model": "m", "api_key": "sk-X"}
    view = api._public_view(raw)
    assert "sk-X" not in str(view) and "api_key" not in view
    raw2 = {"name": "h", "type": "a2a", "url": "https://h/a2a", "auth": {"scheme": "bearer", "token": "SEKRET-TOKEN"}}
    view2 = api._public_view(raw2)
    assert "SEKRET-TOKEN" not in str(view2) and view2["auth"] == {"scheme": "bearer"}


def test_public_view_serializes_env_remove_without_redacting_names():
    # env_remove holds env VAR NAMES, not secrets (#2117 acceptance #5) — they serialize
    # through the public view intact, while secret-named `env` VALUES are still redacted.
    raw = {
        "name": "coder",
        "type": "acp",
        "command": "proto",
        "workdir": "/tmp",
        "env_remove": ["PROTOAGENT_", "A2A_AUTH_TOKEN"],
        "env": {"HOME": "/h", "OPENAI_API_KEY": "sk-LEAK"},
    }
    view = api._public_view(raw)
    assert view["env_remove"] == ["PROTOAGENT_", "A2A_AUTH_TOKEN"]  # names pass through, not "***"
    assert view["env"]["OPENAI_API_KEY"] == "***"  # secret env value still redacted
    assert "sk-LEAK" not in str(view)


# ── per-delegate env editor + secret tier (#2114) ─────────────────────────────


def test_every_adapter_type_exposes_the_env_editor_field(client):
    # The env editor is available on ALL adapter types (a2a/openai/acp), so operators can
    # author env-carrying delegates from the console form for any type.
    for t in client.get("/api/delegate-types").json()["types"]:
        kinds = {f["kind"] for f in t["fields"]}
        assert "envmap" in kinds, f"{t['type']} is missing the env editor field"


def test_upsert_routes_marked_env_secret_and_keeps_empty_reference(fake_io):
    store.upsert_delegate(
        {
            "name": "coder",
            "type": "acp",
            "command": "claude-agent-acp",
            "workdir": "/repo",
            "env": {"ANTHROPIC_BASE_URL": "https://gw/v1", "ANTHROPIC_AUTH_TOKEN": "sk-secret"},
            "env_secret": ["ANTHROPIC_AUTH_TOKEN"],
        }
    )
    stored = fake_io["doc"]["delegates"][0]
    assert stored["env"]["ANTHROPIC_BASE_URL"] == "https://gw/v1"  # non-secret pair intact
    assert stored["env"]["ANTHROPIC_AUTH_TOKEN"] == ""  # empty reference in tracked config
    assert "sk-secret" not in str(stored)
    assert "env_secret" not in stored  # form-only marker never persisted
    assert fake_io["secrets"]["delegate_secrets"]["coder.env.ANTHROPIC_AUTH_TOKEN"] == "sk-secret"


def test_upsert_routes_explicitly_marked_secret_with_innocuous_name(fake_io):
    # A var whose NAME doesn't look secret-bearing still routes to secrets.yaml when the
    # operator toggled the row secret (the explicit-marker path).
    store.upsert_delegate(
        {
            "name": "coder",
            "type": "acp",
            "command": "c",
            "workdir": "/repo",
            "env": {"GATEWAY_ID": "g-123"},
            "env_secret": ["GATEWAY_ID"],
        }
    )
    stored = fake_io["doc"]["delegates"][0]
    assert stored["env"]["GATEWAY_ID"] == ""
    assert fake_io["secrets"]["delegate_secrets"]["coder.env.GATEWAY_ID"] == "g-123"


def test_upsert_auto_routes_secret_named_env_without_a_marker(fake_io):
    # A secret-NAMED env value (OPENAI_API_KEY) is routed even without the toggle, so a
    # credential never lands in tracked config by accident.
    store.upsert_delegate(
        {"name": "coder", "type": "acp", "command": "c", "workdir": "/repo", "env": {"OPENAI_API_KEY": "sk-LEAK"}}
    )
    stored = fake_io["doc"]["delegates"][0]
    assert stored["env"]["OPENAI_API_KEY"] == ""
    assert "sk-LEAK" not in str(stored)
    assert fake_io["secrets"]["delegate_secrets"]["coder.env.OPENAI_API_KEY"] == "sk-LEAK"


def test_merged_delegates_overlays_env_secret_back(fake_io):
    store.upsert_delegate(
        {
            "name": "coder",
            "type": "acp",
            "command": "c",
            "workdir": "/repo",
            "env": {"ANTHROPIC_BASE_URL": "https://gw/v1", "ANTHROPIC_AUTH_TOKEN": "sk-secret"},
            "env_secret": ["ANTHROPIC_AUTH_TOKEN"],
        }
    )
    assert "sk-secret" not in str(fake_io["doc"]["delegates"])  # not in tracked config
    merged = store.merged_delegates()[0]
    assert merged["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"  # real value overlaid at load
    assert merged["env"]["ANTHROPIC_BASE_URL"] == "https://gw/v1"


def test_upsert_no_env_leaves_config_and_secrets_untouched(fake_io):
    # Acceptance #4: an adapter with no env configured stores no `env` key and creates no
    # secrets.yaml entry.
    store.upsert_delegate({"name": "p", "type": "acp", "command": "proto", "workdir": "/tmp"})
    stored = fake_io["doc"]["delegates"][0]
    assert "env" not in stored
    assert fake_io["secrets"].get("delegate_secrets", {}) == {}


def test_upsert_persists_env_remove_as_a_list(fake_io):
    store.upsert_delegate(
        {
            "name": "coder",
            "type": "acp",
            "command": "c",
            "workdir": "/repo",
            "env_remove": ["PROTOAGENT_", "A2A_AUTH_TOKEN"],
        }
    )
    assert fake_io["doc"]["delegates"][0]["env_remove"] == ["PROTOAGENT_", "A2A_AUTH_TOKEN"]


def test_a2a_and_openai_adapters_carry_env(fake_io):
    # a2a/openai don't dispatch env themselves, but the field round-trips through config so
    # forks/plugins that DO consume it can be authored from the console.
    from plugins.delegates.adapters import ADAPTERS

    store.upsert_delegate(
        {"name": "peer", "type": "a2a", "url": "https://p/a2a", "env": {"HTTP_PROXY": "http://proxy:8080"}}
    )
    stored = fake_io["doc"]["delegates"][0]
    assert stored["env"] == {"HTTP_PROXY": "http://proxy:8080"}
    d = ADAPTERS["a2a"].parse(store.merged_delegates()[0])
    assert d.env == {"HTTP_PROXY": "http://proxy:8080"}


def test_public_view_masks_env_secret_by_overlay_and_flags_it(fake_io):
    # A secret env value comes back as a stored empty reference; the view masks it (even
    # with an innocuous name) using the overlay and sets has_env_secrets.
    fake_io["secrets"]["delegate_secrets"] = {"coder.env.GATEWAY_ID": "g-123"}
    raw = {
        "name": "coder",
        "type": "acp",
        "command": "c",
        "workdir": "/repo",
        "env": {"GATEWAY_ID": "", "PLAIN": "v"},
    }
    view = api._public_view(raw)
    assert view["env"]["GATEWAY_ID"] == "***"  # masked via the overlay, not the name
    assert view["env"]["PLAIN"] == "v"
    assert view["has_env_secrets"] is True
    assert "g-123" not in str(view)


def test_create_with_env_secret_routes_masks_and_merges(client, fake_io):
    # End-to-end acceptance #1/#2/#3: create a delegate with a non-secret + secret env pair
    # and an env_remove list; the value is routed, masked on read, and merged at load.
    r = client.post(
        "/api/delegates",
        json={
            "name": "coder",
            "type": "acp",
            "command": "claude-agent-acp",
            "workdir": "/repo",
            "env": {"ANTHROPIC_BASE_URL": "https://gw/v1", "ANTHROPIC_AUTH_TOKEN": "sk-secret"},
            "env_secret": ["ANTHROPIC_AUTH_TOKEN"],
            "env_remove": ["PROTOAGENT_"],
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "sk-secret" not in str(r.json())  # value never echoed to the client
    view = next(d for d in r.json()["delegates"] if d["name"] == "coder")
    assert view["env"]["ANTHROPIC_AUTH_TOKEN"] == "***"  # set-but-masked
    assert view["env"]["ANTHROPIC_BASE_URL"] == "https://gw/v1"
    assert view["has_env_secrets"] is True
    assert view["env_remove"] == ["PROTOAGENT_"]
    # secret VALUE in secrets.yaml under the env key; config holds an empty reference.
    assert fake_io["secrets"]["delegate_secrets"]["coder.env.ANTHROPIC_AUTH_TOKEN"] == "sk-secret"
    stored = next(d for d in fake_io["doc"]["delegates"] if d["name"] == "coder")
    assert stored["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
    # what the runtime sees (merged) carries the real secret value.
    assert store.merged_delegates()[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"


def test_edit_keeps_stored_env_secret_when_row_left_masked(client, fake_io):
    # Acceptance #2 round-trip: on edit, a masked secret row comes back blank; re-saving with
    # the key present + blank keeps the stored value rather than clobbering it.
    client.post(
        "/api/delegates",
        json={
            "name": "coder",
            "type": "acp",
            "command": "c",
            "workdir": "/repo",
            "env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret"},
            "env_secret": ["ANTHROPIC_AUTH_TOKEN"],
        },
    )
    # The form re-sends the masked row as an empty value, still marked secret.
    r = client.put(
        "/api/delegates/coder",
        json={
            "type": "acp",
            "command": "c",
            "workdir": "/repo",
            "env": {"ANTHROPIC_AUTH_TOKEN": ""},
            "env_secret": ["ANTHROPIC_AUTH_TOKEN"],
        },
    )
    assert r.status_code == 200
    assert fake_io["secrets"]["delegate_secrets"]["coder.env.ANTHROPIC_AUTH_TOKEN"] == "sk-secret"
    assert store.merged_delegates()[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"


def test_removing_a_secret_env_row_prunes_the_stored_value(tmp_path, monkeypatch):
    """QA panel major on #2150: deleting a secret row must delete its stored value —
    an orphaned delegate_secrets entry would re-inject at every spawn forever."""
    import yaml

    from plugins.delegates import store

    sp = tmp_path / "secrets.yaml"
    sp.write_text(
        yaml.safe_dump({"delegate_secrets": {"d1.env.TOKEN": "sk-1", "d1.env.HOST": "h", "d2.env.TOKEN": "sk-2"}})
    )
    monkeypatch.setattr("graph.config_io.secrets_yaml_path", lambda: sp)
    monkeypatch.setattr("graph.config_io.load_secrets", lambda path=None: yaml.safe_load(sp.read_text()) or {})

    store._prune_secrets("d1", {"HOST"})  # TOKEN row removed by the operator
    left = yaml.safe_load(sp.read_text())["delegate_secrets"]
    assert "d1.env.TOKEN" not in left
    assert left["d1.env.HOST"] == "h"  # kept row untouched
    assert left["d2.env.TOKEN"] == "sk-2"  # other delegates untouched

    store._prune_secrets("d1", None)  # delegate deleted → everything under d1. goes
    left = yaml.safe_load(sp.read_text()).get("delegate_secrets", {})
    assert not any(k.startswith("d1.") for k in left)
    assert left["d2.TOKEN" if "d2.TOKEN" in left else "d2.env.TOKEN"]
