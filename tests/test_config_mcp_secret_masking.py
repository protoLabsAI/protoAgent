"""`GET /api/config` shows that an MCP server's env / header values are set, never what they
are — and a client writing the document back can't replace a real token with the mask."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime.state as rs
from graph.mcp_config import REDACTED_VALUE, redact_server_secrets, restore_server_secrets
from operator_api import config_routes

STORED = [
    {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "env": {"GITHUB_TOKEN": "ghp_real", "EMPTY": ""},
    },
    {"name": "remote", "transport": "http", "url": "https://mcp.example", "headers": {"Authorization": "Bearer real"}},
    {"name": "plain", "transport": "stdio", "command": "uvx"},
]


def test_redaction_masks_values_and_keeps_keys_and_shape():
    out = redact_server_secrets(STORED)
    assert out[0]["env"] == {"GITHUB_TOKEN": REDACTED_VALUE, "EMPTY": ""}
    assert out[1]["headers"] == {"Authorization": REDACTED_VALUE}
    assert out[2] == STORED[2]
    assert out[0]["command"] == "npx" and out[1]["url"] == "https://mcp.example"
    assert STORED[0]["env"]["GITHUB_TOKEN"] == "ghp_real"  # the stored list is untouched
    assert "ghp_real" not in repr(out) and "Bearer real" not in repr(out)


def test_a_round_trip_restores_what_was_masked():
    assert restore_server_secrets(redact_server_secrets(STORED), STORED) == STORED


def test_restore_keeps_real_edits_and_never_writes_the_mask():
    incoming = [
        {"name": "github", "transport": "stdio", "command": "npx", "env": {"GITHUB_TOKEN": "ghp_new"}},
        {
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example",
            "headers": {"Authorization": REDACTED_VALUE, "X-New": REDACTED_VALUE, "X-Typed": "v"},
        },
        {"name": "brand-new", "transport": "stdio", "command": "x", "env": {"K": REDACTED_VALUE}},
    ]
    out = restore_server_secrets(incoming, STORED)
    assert out[0]["env"] == {"GITHUB_TOKEN": "ghp_new"}
    assert out[1]["headers"] == {"Authorization": "Bearer real", "X-Typed": "v"}
    assert out[2]["env"] == {}
    assert REDACTED_VALUE not in repr(out)


@pytest.fixture
def client(monkeypatch):
    import graph.config_io as config_io
    import server.agent_init as agent_init

    writes: list = []
    monkeypatch.setattr(
        config_io, "config_to_dict", lambda cfg: {"mcp": {"enabled": True, "servers": [dict(s) for s in STORED]}}
    )
    monkeypatch.setattr(config_io, "read_soul", lambda: "")
    monkeypatch.setattr(
        agent_init, "_apply_settings_changes", lambda config=None, soul=None, **_k: (writes.append(config) or True, [])
    )
    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(mcp_servers=STORED), raising=False)
    app = FastAPI()
    config_routes.register_config_routes(app)
    c = TestClient(app)
    c.writes = writes
    return c


def test_get_config_never_returns_an_mcp_secret(client):
    body = client.get("/api/config")
    assert body.status_code == 200
    assert "ghp_real" not in body.text and "Bearer real" not in body.text
    servers = body.json()["config"]["mcp"]["servers"]
    assert servers[0]["env"]["GITHUB_TOKEN"] == REDACTED_VALUE


def test_posting_the_document_back_keeps_the_stored_secrets(client):
    doc = client.get("/api/config").json()["config"]
    assert client.post("/api/config", json={"config": doc}).status_code == 200
    assert client.writes[-1]["mcp"]["servers"] == STORED
