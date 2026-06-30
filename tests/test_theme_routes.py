"""Per-agent theme persistence (ADR 0042) — save/load/reset, scoped to the config dir."""

from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from operator_api.theme_routes import register_theme_routes

    app = FastAPI()
    register_theme_routes(app)
    return TestClient(app)


def test_save_load_reset(client):
    assert client.get("/api/theme").json()["theme"] is None  # none yet

    theme = {"mode": "dark", "tokens": {"--accent": "#7c5cff", "--border": "rgba(0,0,0,0.08)"}}
    assert client.put("/api/theme", json={"theme": theme}).json()["ok"]
    assert client.get("/api/theme").json()["theme"] == theme  # round-trips verbatim

    assert client.delete("/api/theme").json()["ok"]
    assert client.get("/api/theme").json()["theme"] is None  # reset to defaults


def test_accepts_raw_blob(client):
    # a client that PUTs the blob directly (no {theme:…} wrapper) still persists
    client.put("/api/theme", json={"mode": "light"})
    assert client.get("/api/theme").json()["theme"] == {"mode": "light"}
