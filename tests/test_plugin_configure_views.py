"""Sandboxed plugin-owned Configure tabs (#3180)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_impl import auth
from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import _warn_unserved_views, load_plugins
from graph.plugins.manifest import PluginManifest, load_manifest


def _plugin(root: Path, manifest_extra: str) -> Path:
    directory = root / "boardy"
    directory.mkdir(parents=True)
    (directory / "protoagent.plugin.yaml").write_text(
        "id: boardy\nname: Boardy\nenabled: true\n" + manifest_extra,
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text("def register(registry):\n    pass\n", encoding="utf-8")
    return directory


def test_path_backed_tab_is_namespaced_public_chrome_and_not_a_settings_target(tmp_path, caplog) -> None:
    directory = _plugin(
        tmp_path,
        "settings_tabs:\n"
        "  - {id: projects, label: Projects, path: '/plugins/boardy/config/projects?mode=all#top'}\n"
        "  - {id: automation, label: Automation}\n"
        "settings:\n"
        "  - {key: bad, label: Bad target, type: bool, tab: projects}\n"
        "  - {key: cadence, label: Cadence, type: number, tab: automation}\n",
    )
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        manifest = load_manifest(directory)

    assert manifest is not None
    assert manifest.settings_tabs == [
        {
            "id": "projects",
            "label": "Projects",
            "path": "/plugins/boardy/config/projects?mode=all#top",
        },
        {"id": "automation", "label": "Automation"},
    ]
    assert manifest.public_paths == ["/plugins/boardy/config/projects"]
    assert "tab" not in manifest.settings[0]
    assert manifest.settings[1]["tab"] == "automation"
    assert "cannot target path-backed settings tab 'projects'" in caplog.text


def test_configure_paths_outside_the_exact_plugin_page_namespace_are_dropped(tmp_path, caplog) -> None:
    directory = _plugin(
        tmp_path,
        "settings_tabs:\n"
        "  - {id: other, label: Other, path: /plugins/other/config}\n"
        "  - {id: api, label: API, path: /api/plugins/boardy/config}\n"
        "  - {id: core, label: Core, path: /api/config}\n"
        "  - {id: escape, label: Escape, path: /plugins/boardy/../other/config}\n"
        "  - {id: encoded, label: Encoded, path: /plugins/boardy/%2e%2e/other/config}\n"
        "  - {id: double_encoded, label: Double encoded, path: /plugins/boardy/%252e%252e/other/config}\n"
        "  - {id: backslash, label: Backslash, path: '/plugins/boardy\\..\\other/config'}\n"
        "  - {id: remote, label: Remote, path: 'https://example.com/config'}\n",
    )
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        manifest = load_manifest(directory)

    assert manifest is not None
    assert manifest.settings_tabs == []
    assert manifest.public_paths == []
    assert caplog.text.count("Configure path") == 8


def test_configure_page_is_public_but_its_plugin_api_stays_bearer_gated(tmp_path, monkeypatch) -> None:
    directory = _plugin(
        tmp_path,
        "settings_tabs:\n"
        "  - {id: projects, label: Projects, path: /plugins/boardy/config/project%20registry}\n",
    )
    manifest = load_manifest(directory)
    assert manifest is not None
    # The auth middleware sees ASGI's decoded path, not the manifest URL spelling.
    assert manifest.public_paths == ["/plugins/boardy/config/project registry"]

    ok = PlainTextResponse("ok")
    app = Starlette(
        routes=[
            Route("/plugins/boardy/config/project registry", lambda _request: ok),
            Route("/api/plugins/boardy/projects", lambda _request: ok),
        ]
    )
    app.add_middleware(auth.A2AAuthMiddleware)
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    try:
        auth.set_public_prefixes(manifest.public_paths)
        client = TestClient(app)
        assert client.get("/plugins/boardy/config/project%20registry").status_code == 200
        assert client.get("/api/plugins/boardy/projects").status_code == 401
        assert client.get(
            "/api/plugins/boardy/projects",
            headers={"Authorization": "Bearer secret"},
        ).status_code == 200
    finally:
        auth.set_public_prefixes([])
        auth.configure(bearer_token="", api_key="", allowed_origins_raw="")


def test_runtime_metadata_exposes_custom_tabs_only_while_enabled(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    _plugin(
        root,
        "settings_tabs:\n"
        "  - {id: projects, label: Projects, path: /plugins/boardy/config/projects}\n",
    )
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda _config: [root])

    enabled = load_plugins(LangGraphConfig(plugins_enabled=["boardy"]))
    assert enabled.meta[0]["settings_tabs"] == [
        {"id": "projects", "label": "Projects", "path": "/plugins/boardy/config/projects"}
    ]

    disabled = load_plugins(LangGraphConfig(plugins_disabled=["boardy"]))
    assert disabled.meta[0]["settings_tabs"] == []


def test_unserved_warning_covers_configure_pages_and_strips_query_fragment(caplog, tmp_path) -> None:
    manifest = PluginManifest(
        id="boardy",
        name="Boardy",
        path=tmp_path,
        settings_tabs=[
            {
                "id": "projects",
                "label": "Projects",
                "path": "/plugins/boardy/config/project%20registry?mode=all#top",
            }
        ],
    )
    router = APIRouter()

    @router.get("/config/project registry")
    async def _projects() -> dict:
        return {}

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        _warn_unserved_views(manifest, [{"router": router, "prefix": "/plugins/boardy"}])
    assert "no registered router serves it" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        _warn_unserved_views(manifest, [])
    assert "Configure tab 'projects'" in caplog.text
    assert "no registered router serves it" in caplog.text
