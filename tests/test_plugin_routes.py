"""Operator plugin routes — the Direct enable/disable toggle (hot reload)."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def _wire(monkeypatch, *, enabled, disabled, meta):
    """Fake the hot-reload apply + STATE; return a dict that captures the config patch."""
    captured: dict = {}
    fake = types.ModuleType("server.agent_init")

    def _apply(config=None, soul=None):
        captured["config"] = config
        return True, ["reloaded"]

    fake._apply_settings_changes = _apply
    monkeypatch.setitem(sys.modules, "server.agent_init", fake)

    import runtime.state as rs
    cfg = types.SimpleNamespace(plugins_enabled=list(enabled), plugins_disabled=list(disabled))
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_meta", meta, raising=False)
    return captured


def test_enable_moves_lists_and_hot_reloads(monkeypatch):
    captured = _wire(monkeypatch, enabled=["discord"], disabled=["github"],
                     meta=[{"id": "github", "views": []}])
    body = _client().post("/api/plugins/github/enabled", json={"enabled": True}).json()
    assert body == {"ok": True, "enabled": True, "reloaded": True, "restart_recommended": False}
    plugins = captured["config"]["plugins"]
    assert set(plugins["enabled"]) == {"discord", "github"}  # added, no dupes
    assert plugins["disabled"] == []                          # removed from disabled


def test_disable_moves_to_disabled(monkeypatch):
    captured = _wire(monkeypatch, enabled=["discord", "github"], disabled=[],
                     meta=[{"id": "github", "views": []}])
    body = _client().post("/api/plugins/github/enabled", json={"enabled": False}).json()
    assert body["enabled"] is False
    plugins = captured["config"]["plugins"]
    assert plugins["enabled"] == ["discord"]
    assert plugins["disabled"] == ["github"]


def test_enabling_a_view_plugin_recommends_restart(monkeypatch):
    _wire(monkeypatch, enabled=[], disabled=["boardy"],
          meta=[{"id": "boardy", "views": [{"id": "board"}]}])
    body = _client().post("/api/plugins/boardy/enabled", json={"enabled": True}).json()
    assert body["restart_recommended"] is True  # its view route mounts at init
