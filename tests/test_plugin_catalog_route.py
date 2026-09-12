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
    # A bundled built-in: <root>/plugins/discord holds a manifest → that entry is "bundled".
    _bundle(tmp_path, "discord")

    plugs = {p["id"]: p for p in _client().get("/api/plugins/catalog").json()["plugins"]}
    assert plugs["discord"]["bundled"] is True and plugs["discord"]["installed"] is False


def _bundle(root, pid, folder=None):
    """A real bundled plugin (a manifest in ``<root>/plugins/<folder>``), which is what
    the installer's built-in rule looks for."""
    d = root / "plugins" / (folder or pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(f"id: {pid}\nname: {pid}\nversion: 0.1.0\n", encoding="utf-8")


def _catalog(cfg, *ids):
    base = "https://github.com/protoLabsAI/protoAgent/tree/main/plugins/"
    (cfg / "plugin-catalog.json").write_text(
        json.dumps({"plugins": [{"id": i, "name": i.title(), "repo": base + i} for i in ids]})
    )


def test_a_bundled_plugin_reports_its_real_on_off_state_and_why(monkeypatch, tmp_path):
    """Discover used to show every bundled plugin as a bare "bundled", always
    `enabled: false`. It never read the loader's state for the copy that ships in core.
    Now: on or off as the loader has it, and `enabled_by` when another bundled plugin's
    `enables:` is the reason it's on (#3450: cowork turns execute_code on)."""
    cfg = _pin_paths(monkeypatch, tmp_path)
    _catalog(cfg, "cowork", "execute_code", "telegram")
    for pid in ("cowork", "execute_code", "telegram"):
        _bundle(tmp_path, pid)
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(
        rs.STATE,
        "plugin_meta",
        [
            {"id": "cowork", "enabled": True, "enabled_by": []},
            {"id": "execute_code", "enabled": True, "enabled_by": ["cowork"]},
            {"id": "telegram", "enabled": False, "enabled_by": []},
        ],
        raising=False,
    )

    plugs = {p["id"]: p for p in _client().get("/api/plugins/catalog").json()["plugins"]}
    assert all(p["bundled"] and not p["installed"] for p in plugs.values())
    assert plugs["cowork"]["enabled"] is True and plugs["cowork"]["enabled_by"] == []
    assert plugs["execute_code"]["enabled"] is True and plugs["execute_code"]["enabled_by"] == ["cowork"]
    assert plugs["telegram"]["enabled"] is False and plugs["telegram"]["enabled_by"] == []


def test_a_leftover_folder_without_a_manifest_is_not_bundled(monkeypatch, tmp_path):
    """A ``__pycache__``-only dir left by a core→standalone extraction (git doesn't track
    it) is not a built-in, and the installer will install the standalone successor over it
    (#1731). Discover now asks the installer's rule instead of "does the folder exist", so
    it offers Install rather than a "bundled" pill for a plugin that doesn't ship."""
    cfg = _pin_paths(monkeypatch, tmp_path)
    (cfg / "plugin-catalog.json").write_text(
        json.dumps({"plugins": [{"id": "github", "name": "GitHub", "repo": "https://github.com/x/github-plugin"}]})
    )
    (tmp_path / "plugins" / "github" / "__pycache__").mkdir(parents=True)
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)

    plugs = {p["id"]: p for p in _client().get("/api/plugins/catalog").json()["plugins"]}
    assert plugs["github"]["bundled"] is False and plugs["github"]["installed"] is False


def test_a_bundled_manifest_id_is_found_whatever_its_folder_is_called(monkeypatch, tmp_path):
    """The loader keys plugins by manifest id, so a folder named `agent-browser` holding id
    `agent_browser` is the bundled `agent_browser`, and its catalog row must not offer Install."""
    cfg = _pin_paths(monkeypatch, tmp_path)
    _catalog(cfg, "agent_browser")
    _bundle(tmp_path, "agent_browser", folder="agent-browser")
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [{"id": "agent_browser", "enabled": True}], raising=False)

    plug = _client().get("/api/plugins/catalog").json()["plugins"][0]
    assert plug["bundled"] is True and plug["enabled"] is True


def test_catalog_empty_when_no_file(monkeypatch, tmp_path):
    _pin_paths(monkeypatch, tmp_path)  # no plugin-catalog.json anywhere under root
    monkeypatch.setattr(installer, "list_installed", lambda: [])
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    assert _client().get("/api/plugins/catalog").json() == {"plugins": []}
