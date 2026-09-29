"""Developer flags API (ADR 0068, slice 2) — GET /api/flags serves resolved_flags()."""

from __future__ import annotations

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
        if k.startswith("PROTOAGENT_FLAG_") or k in ("PROTOAGENT_CHANNEL", "PROTOAGENT_INSTANCE", "PROTOAGENT_AUTO_SCOPE"):
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
            "id": "chat.new", "description": "A new thing", "tier": "beta",
            "owner": "kj", "remove_by": "v2", "enabled": True, "source": "channel",
        }
    ]


def test_flags_route_empty_registry(monkeypatch):
    monkeypatch.setattr(flags, "FLAGS", [])
    body = _client().get("/api/flags").json()
    assert body["flags"] == [] and body["channel"] in ("prod", "beta", "dev")
