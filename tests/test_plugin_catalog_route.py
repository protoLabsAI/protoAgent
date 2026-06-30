"""GET /api/plugins/catalog — the Discover directory (ADR 0059), merged with state."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra.paths as paths
import runtime.state as rs
from graph.plugins import installer
from operator_api.plugin_routes import register_plugin_routes


def _client():
    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def _pin_paths(monkeypatch, root):
    """Pin instance_paths() at ``root`` so config_dir + bundle_dir + bundled plugins
    all resolve under a sandbox: config/bundle catalog at ``<root>/config``, bundled
    built-ins at ``<root>/plugins``."""
    fake = paths.InstancePaths(instance_id="t", box_root=root, instance_root=root, app_root=root)
    monkeypatch.setattr(paths, "_CURRENT_PATHS", fake)
    (root / "config").mkdir(parents=True, exist_ok=True)
    return root / "config"


def test_catalog_served_with_install_state(monkeypatch, tmp_path):
    cfg = _pin_paths(monkeypatch, tmp_path)
    (cfg / "plugin-catalog.json").write_text(
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
    # <root>/plugins doesn't exist → nothing bundled.
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

    r = _client().get("/api/plugins/catalog")
    assert r.status_code == 200
    plugs = {p["id"]: p for p in r.json()["plugins"]}
    assert len(plugs) == 3
    assert plugs["artifact"]["installed"] and plugs["artifact"]["enabled"]
    assert plugs["terminal"]["installed"] and plugs["terminal"]["enabled"] is False
    assert plugs["discord"]["installed"] is False and plugs["discord"]["bundled"] is False


def test_catalog_marks_bundled_builtin(monkeypatch, tmp_path):
    cfg = _pin_paths(monkeypatch, tmp_path)
    (cfg / "plugin-catalog.json").write_text(
        json.dumps({"plugins": [{"id": "discord", "name": "Discord", "repo": "https://github.com/x/discord-plugin"}]})
    )
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    # A bundled built-in: <root>/plugins/discord exists → that catalog entry is "bundled".
    (tmp_path / "plugins" / "discord").mkdir(parents=True)

    plugs = {p["id"]: p for p in _client().get("/api/plugins/catalog").json()["plugins"]}
    assert plugs["discord"]["bundled"] is True and plugs["discord"]["installed"] is False


def test_catalog_empty_when_no_file(monkeypatch, tmp_path):
    _pin_paths(monkeypatch, tmp_path)  # no plugin-catalog.json anywhere under root
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    assert _client().get("/api/plugins/catalog").json() == {"plugins": []}
