"""Operator MCP routes — add/remove mcp.servers from the console (hot reload)."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from operator_api.mcp_routes import register_mcp_routes

    app = FastAPI()
    register_mcp_routes(app)
    return TestClient(app)


def _wire(monkeypatch, *, servers):
    captured: dict = {}
    fake = types.ModuleType("server.agent_init")

    def _apply(config=None, soul=None):
        captured["config"] = config
        return True, ["reloaded"]

    fake._apply_settings_changes = _apply
    monkeypatch.setitem(sys.modules, "server.agent_init", fake)

    import runtime.state as rs
    monkeypatch.setattr(rs.STATE, "graph_config",
                        types.SimpleNamespace(mcp_servers=list(servers)), raising=False)
    return captured


def test_add_stdio_server_enables_mcp_and_hot_reloads(monkeypatch):
    captured = _wire(monkeypatch, servers=[])
    body = _client().post("/api/mcp/servers", json={
        "name": "echo", "transport": "stdio", "command": "python", "args": "-m echo",
    }).json()
    assert body["ok"] and body["servers"] == ["echo"]
    mcp = captured["config"]["mcp"]
    assert mcp["enabled"] is True
    assert mcp["servers"][0] == {"name": "echo", "transport": "stdio", "command": "python", "args": ["-m", "echo"]}


def test_add_http_server_requires_url(monkeypatch):
    _wire(monkeypatch, servers=[])
    r = _client().post("/api/mcp/servers", json={"name": "remote", "transport": "http"})
    assert r.status_code == 400
    assert "url" in r.json()["detail"]


def test_add_upserts_by_name(monkeypatch):
    captured = _wire(monkeypatch, servers=[{"name": "echo", "transport": "stdio", "command": "old"}])
    _client().post("/api/mcp/servers", json={"name": "echo", "transport": "stdio", "command": "new"}).json()
    servers = captured["config"]["mcp"]["servers"]
    assert len(servers) == 1 and servers[0]["command"] == "new"  # replaced, not duplicated


def test_remove_server(monkeypatch):
    captured = _wire(monkeypatch, servers=[{"name": "echo", "transport": "stdio", "command": "x"},
                                           {"name": "keep", "transport": "stdio", "command": "y"}])
    body = _client().delete("/api/mcp/servers/echo").json()
    assert body["servers"] == ["keep"]
    assert [s["name"] for s in captured["config"]["mcp"]["servers"]] == ["keep"]


def test_name_required(monkeypatch):
    _wire(monkeypatch, servers=[])
    assert _client().post("/api/mcp/servers", json={"transport": "stdio", "command": "x"}).status_code == 400
