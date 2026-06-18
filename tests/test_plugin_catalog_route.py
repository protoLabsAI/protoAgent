"""GET /api/plugins/catalog — the Discover directory (ADR 0059), merged with state."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import graph.config_io as cio
import runtime.state as rs
from graph.plugins import installer
from operator_api.plugin_routes import register_plugin_routes


def _client(catalog_dir):
    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def test_catalog_served_with_install_state(monkeypatch, tmp_path):
    (tmp_path / "plugin-catalog.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"id": "discord", "name": "Discord", "repo": "https://github.com/protoLabsAI/discord-plugin"},
                    {"id": "artifact", "name": "Artifact", "repo": "https://github.com/protoLabsAI/artifact-plugin"},
                    {"id": "terminal", "name": "Terminal", "repo": "https://github.com/protoLabsAI/terminal-plugin"},
                ]
            }
        )
    )
    # Catalog resolves from the bundle dir; live dir empty; no built-ins present.
    monkeypatch.setattr(cio, "_BUNDLE_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path / "nolive"))
    monkeypatch.setattr(installer, "REPO_ROOT", tmp_path)  # tmp_path/plugins doesn't exist → nothing bundled
    # artifact installed (matched by repo URL, even with a trailing .git) + enabled; terminal installed, disabled.
    monkeypatch.setattr(
        installer,
        "list_installed",
        lambda: [
            {"id": "artifact", "source_url": "https://github.com/protoLabsAI/artifact-plugin.git", "present": True},
            {"id": "terminal", "source_url": "https://github.com/protoLabsAI/terminal-plugin", "present": True},
        ],
    )
    monkeypatch.setattr(rs.STATE, "plugin_meta", [{"id": "artifact", "enabled": True}], raising=False)

    r = _client(tmp_path).get("/api/plugins/catalog")
    assert r.status_code == 200
    plugs = {p["id"]: p for p in r.json()["plugins"]}
    assert len(plugs) == 3
    assert plugs["artifact"]["installed"] and plugs["artifact"]["enabled"]
    assert plugs["terminal"]["installed"] and plugs["terminal"]["enabled"] is False
    assert plugs["discord"]["installed"] is False and plugs["discord"]["bundled"] is False


def test_catalog_marks_bundled_builtin(monkeypatch, tmp_path):
    (tmp_path / "plugin-catalog.json").write_text(
        json.dumps({"plugins": [{"id": "discord", "name": "Discord", "repo": "https://github.com/x/discord-plugin"}]})
    )
    monkeypatch.setattr(cio, "_BUNDLE_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path / "nolive"))
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    # A repo root whose plugins/discord exists → that catalog entry is "bundled".
    (tmp_path / "plugins" / "discord").mkdir(parents=True)
    monkeypatch.setattr(installer, "REPO_ROOT", tmp_path)

    plugs = {p["id"]: p for p in _client(tmp_path).get("/api/plugins/catalog").json()["plugins"]}
    assert plugs["discord"]["bundled"] is True and plugs["discord"]["installed"] is False


def test_catalog_empty_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cio, "_BUNDLE_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path / "nolive"))
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    assert _client(tmp_path).get("/api/plugins/catalog").json() == {"plugins": []}
