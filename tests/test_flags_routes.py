"""Developer flags API (ADR 0068, slice 2) — GET /api/flags serves resolved_flags()."""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.flags_routes import register_flags_routes
from runtime import flags
from runtime.flags import Flag


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("PROTOAGENT_FLAG_") or k in (
            "PROTOAGENT_CHANNEL",
            "PROTOAGENT_INSTANCE",
            "PROTOAGENT_AUTO_SCOPE",
        ):
            monkeypatch.delenv(k, raising=False)
    yield


def _client() -> TestClient:
    app = FastAPI()
    register_flags_routes(app)
    return TestClient(app)


def test_flags_route_serves_resolved_payload(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_CHANNEL", "beta")
    monkeypatch.setattr(flags, "FLAGS", [Flag("chat.new", "A new thing", tier="beta", owner="kj", remove_by="v2")])

    r = _client().get("/api/flags")
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "beta"
    assert body["flags"] == [
        {
            "id": "chat.new",
            "description": "A new thing",
            "tier": "beta",
            "owner": "kj",
            "remove_by": "v2",
            "enabled": True,
            "source": "channel",
            "default": True,
        }
    ]


def test_flags_route_empty_registry(monkeypatch):
    monkeypatch.setattr(flags, "FLAGS", [])
    body = _client().get("/api/flags").json()
    assert body["flags"] == [] and body["channel"] in ("prod", "beta", "dev")


# ── stored overrides: the Developer panel's toggle reaches the server ─────────────────


@pytest.fixture
def instance(tmp_path, monkeypatch):
    from infra.paths import reset_instance_paths

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    monkeypatch.setattr(flags, "FLAGS", [Flag("home_conversation", "Home", tier="off"), Flag("beta.thing", "B", tier="on")])
    yield tmp_path
    reset_instance_paths()


def _state(body, flag_id):
    row = next(f for f in body["flags"] if f["id"] == flag_id)
    return row["enabled"], row["source"], row["default"]


def test_toggle_turns_a_server_gated_flag_on_and_it_survives_a_restart(instance):
    client = _client()
    assert flags.flag_enabled("home_conversation") is False
    r = client.put("/api/flags/home_conversation", json={"enabled": True})
    assert r.status_code == 200
    assert _state(r.json(), "home_conversation") == (True, "override", False)
    # What server code asks, not only what the panel is told.
    assert flags.flag_enabled("home_conversation") is True
    # A new process reads the same file: drop the in-memory cache and ask again.
    flags._overrides_cache[:] = [None, None, {}]
    assert flags.flag_enabled("home_conversation") is True
    assert json.loads((instance / "developer-flags.json").read_text()) == {"home_conversation": True}


def test_toggle_can_turn_an_on_flag_off_and_reset_restores_the_default(instance):
    client = _client()
    off = client.put("/api/flags/beta.thing", json={"enabled": False})
    assert _state(off.json(), "beta.thing") == (False, "override", True)
    assert flags.flag_enabled("beta.thing") is False
    reset = client.delete("/api/flags/beta.thing")
    assert reset.status_code == 200
    assert _state(reset.json(), "beta.thing") == (True, "channel", True)
    assert client.delete("/api/flags/beta.thing").status_code == 200  # idempotent


def test_reset_all_clears_every_override(instance):
    client = _client()
    client.put("/api/flags/home_conversation", json={"enabled": True})
    client.put("/api/flags/beta.thing", json={"enabled": False})
    body = client.delete("/api/flags").json()
    assert [f["source"] for f in body["flags"]] == ["channel", "channel"]
    assert flags.stored_overrides() == {}


def test_unknown_flag_is_refused_and_writes_nothing(instance):
    client = _client()
    assert client.put("/api/flags/not.a.flag", json={"enabled": True}).status_code == 404
    assert client.delete("/api/flags/not.a.flag").status_code == 404
    assert client.put("/api/flags/home_conversation", json={"enabled": "yes please"}).status_code == 422
    assert not (instance / "developer-flags.json").exists()


def test_a_flag_pinned_by_its_environment_variable_cannot_be_toggled(instance, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLAG_HOME_CONVERSATION", "1")
    client = _client()
    r = client.put("/api/flags/home_conversation", json={"enabled": False})
    assert r.status_code == 409
    assert "PROTOAGENT_FLAG_HOME_CONVERSATION" in r.json()["detail"]
    assert _state(client.get("/api/flags").json(), "home_conversation") == (True, "env", False)


def test_the_environment_variable_still_wins_over_a_stored_override(instance, monkeypatch):
    _client().put("/api/flags/home_conversation", json={"enabled": True})
    monkeypatch.setenv("PROTOAGENT_FLAG_HOME_CONVERSATION", "off")
    assert flags.flag_enabled("home_conversation") is False


def test_a_corrupt_or_stale_file_never_turns_a_flag_on(instance):
    (instance / "developer-flags.json").write_text("{not json")
    assert flags.flag_enabled("home_conversation") is False
    (instance / "developer-flags.json").write_text(json.dumps({"home_conversation": "true", "removed.flag": True, "beta.thing": False}))
    flags._overrides_cache[:] = [None, None, {}]
    assert flags.stored_overrides() == {"beta.thing": False}  # non-boolean and unregistered entries dropped
    assert flags.flag_enabled("home_conversation") is False


def test_the_real_home_flag_follows_the_panel_toggle(tmp_path, monkeypatch):
    """The case that prompted this: Home is tier "off", so before stored overrides only an
    environment variable could enable its server half."""
    from graph import home_entries
    from runtime.home_session import FLAG_ID
    from infra.paths import reset_instance_paths

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    try:
        assert home_entries._enabled() is False
        assert _client().put(f"/api/flags/{FLAG_ID}", json={"enabled": True}).status_code == 200
        assert home_entries._enabled() is True
        assert _client().delete(f"/api/flags/{FLAG_ID}").status_code == 200
        assert home_entries._enabled() is False
    finally:
        reset_instance_paths()
