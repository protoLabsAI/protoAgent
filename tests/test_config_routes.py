"""Config / setup / settings routes (ADR 0023 phase 3 extraction) — the
registrar wires the surface and the handlers delegate to config_io /
settings_schema / agent_init as before."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from operator_api.config_routes import register_config_routes

    app = FastAPI()
    register_config_routes(app)
    return TestClient(app)


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_get_config_delegates(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module("graph.config_io", config_to_dict=lambda c: {"model": "x"}, read_soul=lambda: "SOUL"),
    )
    import runtime.state as rs
    monkeypatch.setattr(rs.STATE, "graph_config", object(), raising=False)
    body = _client().get("/api/config").json()
    assert body == {"config": {"model": "x"}, "soul": "SOUL"}


def test_setup_status_and_reset(monkeypatch):
    seen = {}
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module(
            "graph.config_io",
            is_setup_complete=lambda: True,
            list_soul_presets=lambda: ["default"],
            reset_setup=lambda: seen.setdefault("reset", True),
        ),
    )
    c = _client()
    assert c.get("/api/config/setup-status").json() == {"setup_complete": True, "presets": ["default"]}
    assert c.post("/api/config/reset-setup").json()["ok"] is True
    assert seen["reset"] is True


def test_post_config_offloads_to_apply(monkeypatch):
    import operator_api.config_routes as cr

    captured = {}

    def _apply(config=None, soul=None):
        captured["config"], captured["soul"] = config, soul
        return True, ["reloaded"]

    monkeypatch.setattr(cr, "_apply_settings_changes", _apply)
    resp = _client().post("/api/config", json={"config": {"a": 1}, "soul": "S"}).json()
    assert resp == {"ok": True, "messages": ["reloaded"]}
    assert captured == {"config": {"a": 1}, "soul": "S"}


def test_save_settings_rejects_invalid(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            validate_flat=lambda u: (False, "bad key"),
            nest_updates=lambda u: u,
            restart_keys=lambda u: [],
        ),
    )
    resp = _client().post("/api/settings", json={"updates": {"x": 1}}).json()
    assert resp["ok"] is False and "validation: bad key" in resp["messages"]


def test_save_settings_threads_layer(monkeypatch):
    """POST /api/settings passes the chosen cascade layer to _apply_settings_changes."""
    import operator_api.config_routes as cr

    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            validate_flat=lambda u: (True, None),
            nest_updates=lambda u: {"nested": u},
            restart_keys=lambda u: [],
        ),
    )
    captured = {}

    def _apply(config=None, layer="agent"):
        captured["config"], captured["layer"] = config, layer
        return True, ["host config saved"]

    monkeypatch.setattr(cr, "_apply_settings_changes", _apply)
    resp = _client().post("/api/settings", json={"updates": {"model.name": "m"}, "layer": "host"}).json()
    assert resp["ok"] is True
    assert captured["layer"] == "host"
    assert captured["config"] == {"nested": {"model.name": "m"}}


def test_save_settings_defaults_to_agent_layer(monkeypatch):
    """No layer in the body ⇒ the agent leaf (today's behavior)."""
    import operator_api.config_routes as cr

    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            validate_flat=lambda u: (True, None),
            nest_updates=lambda u: u,
            restart_keys=lambda u: [],
        ),
    )
    captured = {}

    def _apply(config=None, layer="agent"):
        captured["layer"] = layer
        return True, ["config saved"]

    monkeypatch.setattr(cr, "_apply_settings_changes", _apply)
    _client().post("/api/settings", json={"updates": {"x": 1}})
    assert captured["layer"] == "agent"


def test_reset_settings_pops_known_keys(monkeypatch):
    """POST /api/settings/reset delegates to _reset_settings_keys for known keys."""
    import operator_api.config_routes as cr

    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module("graph.settings_schema", is_known_key=lambda k: k == "model.name"),
    )
    captured = {}

    def _reset(keys):
        captured["keys"] = keys
        return True, ["reset 1 key(s) to inherited", "reloaded"]

    monkeypatch.setattr(cr, "_reset_settings_keys", _reset)
    resp = _client().post("/api/settings/reset", json={"keys": ["model.name"]}).json()
    assert resp["ok"] is True
    assert captured["keys"] == ["model.name"]


def test_reset_settings_rejects_unknown_key(monkeypatch):
    """An unknown key is rejected before any disk touch."""
    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module("graph.settings_schema", is_known_key=lambda k: False),
    )
    resp = _client().post("/api/settings/reset", json={"keys": ["bogus.key"]}).json()
    assert resp["ok"] is False
    assert any("unknown setting: bogus.key" in m for m in resp["messages"])
