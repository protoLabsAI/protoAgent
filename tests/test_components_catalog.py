"""GET /api/components catalog + the frame map that feeds it (ADR 0118 D5, #4087, S11b).

Covers: the loader carries each component's frame through ``PluginLoadResult``; the catalog
lists core + plugin kinds with a public frame URL for frame kinds and null otherwise; a reload
(disabling a plugin) drops its frame kind; and the route is operator-bearer gated.

The registry/testkit validation of the frame itself is S11a's territory
(``tests/test_component_frames.py``); this card is strictly the carry-through + catalog half.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph import components
from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import PluginLoadResult, load_plugins
from operator_api.routes import register_operator_routes


def _v(props):
    return None


# A plugin that registers one FRAMED component and one plain (no-frame) component, so the
# catalog is exercised across both shapes in a single load.
_PLUGIN = '''
def _v(props):
    return None

def register(registry):
    registry.register_component("thing-ref", _v, frame="component.html")
    registry.register_component("plain-ref", _v)
'''


def _make_plugin(root: Path, pid: str) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: true\n"
        f"public_paths:\n  - /plugins/{pid}/component.html\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(_PLUGIN, encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_component_state():
    """Keep the process-global plugin component maps from leaking across tests."""
    yield
    components.set_plugin_components(None)
    components.set_plugin_component_frames(None)


async def _noop(*_args, **_kwargs):
    return ""


# ── loader: the frame rides PluginLoadResult ─────────────────────────────────────────────


def test_loader_carries_each_components_frame_through(tmp_path, monkeypatch):
    _make_plugin(tmp_path, "p")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    # The framed kind carries the owning plugin + the public /plugins/<id>/<frame> URL…
    assert res.component_frames["thing-ref"] == {"plugin": "p", "frame_url": "/plugins/p/component.html"}
    # …and a plain kind is still attributed, with no frame.
    assert res.component_frames["plain-ref"] == {"plugin": "p", "frame_url": None}


# ── catalog: core + plugin kinds, frame_url for frame kinds and null otherwise ───────────


def test_catalog_lists_core_and_plugin_kinds():
    components.set_plugin_components({"thing-ref": _v, "plain-ref": _v})
    components.set_plugin_component_frames(
        {
            "thing-ref": {"plugin": "p", "frame_url": "/plugins/p/component.html"},
            "plain-ref": {"plugin": "p", "frame_url": None},
        }
    )
    rows = {r["name"]: r for r in components.component_catalog()}
    # Every core widget is listed with no plugin and no frame.
    for core in components.COMPONENT_TYPES:
        assert rows[core] == {"name": core, "plugin": None, "frame_url": None}
    # The frame kind carries its public URL; the plain plugin kind is attributed but frameless.
    assert rows["thing-ref"] == {"name": "thing-ref", "plugin": "p", "frame_url": "/plugins/p/component.html"}
    assert rows["plain-ref"] == {"name": "plain-ref", "plugin": "p", "frame_url": None}


# ── reload: disabling a plugin drops its frame kind ──────────────────────────────────────


def test_reload_rebind_drops_a_disabled_plugins_frame_kind():
    import server.plugin_wiring as plugin_wiring

    # The plugin is loaded: the server pushes its bundle, and the frame kind is in the catalog.
    enabled = PluginLoadResult()
    enabled.components = {"thing-ref": _v}
    enabled.component_frames = {"thing-ref": {"plugin": "p", "frame_url": "/plugins/p/component.html"}}
    plugin_wiring._apply_plugin_registries(enabled)
    rows = {r["name"]: r for r in components.component_catalog()}
    assert rows["thing-ref"]["frame_url"] == "/plugins/p/component.html"

    # The plugin is disabled: the reloaded bundle no longer carries it, so it leaves the catalog.
    plugin_wiring._apply_plugin_registries(PluginLoadResult())
    assert "thing-ref" not in {r["name"] for r in components.component_catalog()}


# ── route: operator-bearer gated + serves the live catalog ───────────────────────────────


def _operator_app() -> FastAPI:
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_noop,
        subagent_batch=_noop,
    )
    return app


def test_route_requires_operator_bearer_and_returns_catalog(tmp_path, monkeypatch):
    from a2a_impl import auth

    _make_plugin(tmp_path, "p")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    components.set_plugin_components(res.components)
    components.set_plugin_component_frames(res.component_frames)

    # An explicit bearer, no env fallback, origin verification off — a bare GET is default-deny.
    auth.configure(bearer_token="op-secret", api_key="", allowed_origins_raw="")
    app = _operator_app()
    app.add_middleware(auth.A2AAuthMiddleware)
    c = TestClient(app)

    assert c.get("/api/components").status_code == 401

    resp = c.get("/api/components", headers={"Authorization": "Bearer op-secret"})
    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()}
    for core in components.COMPONENT_TYPES:
        assert rows[core] == {"name": core, "plugin": None, "frame_url": None}
    assert rows["thing-ref"] == {"name": "thing-ref", "plugin": "p", "frame_url": "/plugins/p/component.html"}
    assert rows["plain-ref"] == {"name": "plain-ref", "plugin": "p", "frame_url": None}
