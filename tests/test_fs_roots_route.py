"""``GET /api/fs/roots`` — the live fs fence as ``{name: absolute root}``.

The console turns a tool call's PROJECT-RELATIVE path into an "open in editor" link,
so the roots it joins against must be the ones the fs tools actually resolve through
(``tools.fs_tools._RegistryRef``), not the ADR 0095 registry ``/api/projects`` shows —
explicit ``filesystem.projects`` shadow that registry, and an absent one falls back to
the workspace default. Each case below pins the route to the tools' own registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from operator_api.browse_routes import register_browse_routes
from runtime.state import STATE
from tools.fs_tools import _RegistryRef


@pytest.fixture
def client(monkeypatch):
    def _make(cfg):
        monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
        app = FastAPI()
        register_browse_routes(app)
        return TestClient(app)

    return _make


def _tools_view(cfg) -> dict[str, str]:
    """What the fs tools themselves would resolve each project to."""
    reg = _RegistryRef(cfg).get()
    return {name: str(reg.get(name).root) for name in reg.names()}


def test_explicit_projects_win_and_missing_roots_drop(client, tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "shadowed").mkdir()
    cfg = LangGraphConfig(
        filesystem_projects=[
            {"name": "a", "path": str(tmp_path / "a")},
            {"name": "gone", "path": str(tmp_path / "nope")},
        ],
        # A populated registry is SHADOWED by explicit roots — must not leak in.
        projects=[{"name": "shadowed", "path": str(tmp_path / "shadowed")}],
    )
    roots = client(cfg).get("/api/fs/roots").json()["roots"]
    assert roots == {"a": str((tmp_path / "a").resolve())}
    assert roots == _tools_view(cfg)


def test_registry_projection_feeds_the_fence(client, tmp_path: Path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "optout").mkdir()
    cfg = LangGraphConfig(
        projects=[
            {"name": "repo", "path": str(tmp_path / "repo")},
            {"name": "optout", "path": str(tmp_path / "optout"), "fs": False},
        ],
    )
    roots = client(cfg).get("/api/fs/roots").json()["roots"]
    assert roots == {"repo": str((tmp_path / "repo").resolve())}
    assert roots == _tools_view(cfg)


def test_workspace_default_when_nothing_configured(client, tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(ws))
    cfg = LangGraphConfig()
    roots = client(cfg).get("/api/fs/roots").json()["roots"]
    assert roots == {"workspace": str(ws.resolve())}
    assert roots == _tools_view(cfg)


def test_read_only_never_creates_the_workspace(client, tmp_path: Path, monkeypatch):
    """A GET must not mkdir — an absent default workspace just isn't a root yet."""
    ws = tmp_path / "not-yet"
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(ws))
    assert client(LangGraphConfig()).get("/api/fs/roots").json() == {"roots": {}}
    assert not ws.exists()


def test_disabled_filesystem_has_no_roots(client, tmp_path: Path):
    (tmp_path / "a").mkdir()
    cfg = LangGraphConfig(
        filesystem_enabled=False,
        filesystem_projects=[{"name": "a", "path": str(tmp_path / "a")}],
    )
    assert client(cfg).get("/api/fs/roots").json() == {"roots": {}}


def test_no_config_yet(client):
    assert client(None).get("/api/fs/roots").json() == {"roots": {}}
