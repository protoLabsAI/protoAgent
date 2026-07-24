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


def test_acp_agents_route_serves_the_catalog():
    # The canonical ACP catalog is served for the web pickers (single source).
    body = _client().get("/api/acp-agents").json()
    agents = body["agents"]
    ids = {a["id"] for a in agents}
    assert {"proto", "claude", "gemini"} <= ids
    claude = next(a for a in agents if a["id"] == "claude")
    assert claude["label"] and claude["command"] and isinstance(claude["args"], list)


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
            validate_flat=lambda u, hidden=None: (False, "bad key"),
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
            validate_flat=lambda u, hidden=None: (True, None),
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
            validate_flat=lambda u, hidden=None: (True, None),
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
        _fake_module("graph.settings_schema", is_known_key=lambda k: k == "model.name", is_hidden_setting=lambda k, hidden=None: False),
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
        _fake_module("graph.settings_schema", is_known_key=lambda k: False, is_hidden_setting=lambda k, hidden=None: False),
    )
    resp = _client().post("/api/settings/reset", json={"keys": ["bogus.key"]}).json()
    assert resp["ok"] is False
    assert any("unknown setting: bogus.key" in m for m in resp["messages"])


def test_reset_settings_rejects_hidden_key(monkeypatch):
    """A settings.hidden-locked key can't be reset back to inherited (#2172) — a reset
    writes too (pops the leaf), so the lock covers it like a save."""
    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module("graph.settings_schema", is_known_key=lambda k: True, is_hidden_setting=lambda k, hidden=None: True),
    )
    resp = _client().post("/api/settings/reset", json={"keys": ["goal.eval_model"]}).json()
    assert resp["ok"] is False
    assert any("locked by settings.hidden" in m for m in resp["messages"])


class _BreakerStore:
    def __init__(self):
        self.reset_calls = 0

    def reset_embed_breaker(self):
        self.reset_calls += 1
        return True


def _wire_test_model(monkeypatch, *, ok: bool):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module("graph.config_io", validate_model_connection=lambda b, k, m: (ok, "" if ok else "401")),
    )
    import runtime.state as rs

    cfg = types.SimpleNamespace(api_base="http://g/v1", api_key="live-key", model_name="m")
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    store = _BreakerStore()
    monkeypatch.setattr(rs.STATE, "knowledge_store", store, raising=False)
    return store


def test_test_model_success_clears_embed_breaker(monkeypatch):
    """A passing Test-connection of the LIVE key (no form override) clears the
    embedding circuit breaker so semantic recall recovers without the cooldown."""
    store = _wire_test_model(monkeypatch, ok=True)
    resp = _client().post("/api/config/test-model", json={}).json()
    assert resp["ok"] is True
    assert store.reset_calls == 1


def test_test_model_failure_does_not_clear_breaker(monkeypatch):
    store = _wire_test_model(monkeypatch, ok=False)
    resp = _client().post("/api/config/test-model", json={}).json()
    assert resp["ok"] is False
    assert store.reset_calls == 0


def test_test_model_with_form_key_does_not_clear_breaker(monkeypatch):
    """Testing a CANDIDATE key (form override) must not touch the live store —
    that key isn't what the running embedder uses yet."""
    store = _wire_test_model(monkeypatch, ok=True)
    resp = _client().post("/api/config/test-model", json={"api_key": "candidate"}).json()
    assert resp["ok"] is True
    assert store.reset_calls == 0


# ── SOUL.md version history (#1691) ───────────────────────────────────────────


def test_soul_history_lists_versions(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module(
            "graph.config_io",
            list_soul_versions=lambda: [{"id": "v1", "saved_at": "t", "size": 3, "preview": "abc"}],
        ),
    )
    body = _client().get("/api/config/soul/history").json()
    assert body == {"versions": [{"id": "v1", "saved_at": "t", "size": 3, "preview": "abc"}]}


def test_soul_history_get_one_ok(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module("graph.config_io", read_soul_version=lambda vid: "the persona" if vid == "v1" else None),
    )
    body = _client().get("/api/config/soul/history/v1").json()
    assert body == {"id": "v1", "content": "the persona"}


def test_soul_history_get_one_404_for_unknown(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module("graph.config_io", read_soul_version=lambda vid: None),
    )
    resp = _client().get("/api/config/soul/history/nope")
    assert resp.status_code == 404


def test_soul_history_restore_reapplies_through_the_save_path(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module(
            "graph.config_io",
            read_soul_version=lambda vid: "restored persona",
            read_soul=lambda: "a different current persona",  # not current → really restores
        ),
    )
    calls = {}

    def _fake_apply(config=None, soul=None):
        calls["soul"] = soul
        return True, ["SOUL saved (1 path)"]

    import operator_api.config_routes as cr

    monkeypatch.setattr(cr, "_apply_settings_changes", _fake_apply)
    body = _client().post("/api/config/soul/history/v1/restore").json()
    assert body["ok"] is True and body["restored"] == "v1"
    # Restore re-saves the archived text through the tested save+reload path (which snapshots
    # the current persona first, so a roll-back is itself reversible).
    assert calls["soul"] == "restored persona"


def test_soul_history_restore_current_version_is_a_noop(monkeypatch):
    # Restoring the version that's already live skips the expensive graph recompile.
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module(
            "graph.config_io",
            read_soul_version=lambda vid: "live persona",
            read_soul=lambda: "live persona",  # already current
        ),
    )
    applied = {"called": False}

    def _fake_apply(config=None, soul=None):
        applied["called"] = True
        return True, []

    import operator_api.config_routes as cr

    monkeypatch.setattr(cr, "_apply_settings_changes", _fake_apply)
    body = _client().post("/api/config/soul/history/v1/restore").json()
    assert body["ok"] is True and "already the current persona" in body["messages"]
    assert applied["called"] is False  # no recompile for a no-op restore


def test_soul_history_restore_404_for_unknown(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "graph.config_io",
        _fake_module("graph.config_io", read_soul_version=lambda vid: None, read_soul=lambda: ""),
    )
    resp = _client().post("/api/config/soul/history/nope/restore")
    assert resp.status_code == 404


# ── filesystem.projects editor routes (fenced fs roots, ADR 0007) ─────────────


def _fs_state(monkeypatch, **attrs):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", types.SimpleNamespace(**attrs), raising=False)


def test_fs_projects_get(monkeypatch):
    _fs_state(monkeypatch, filesystem_enabled=True, filesystem_projects=[{"name": "docs", "path": "/d", "write": False}])
    body = _client().get("/api/settings/filesystem-projects").json()
    assert body["enabled"] is True and body["projects"][0]["name"] == "docs"


def test_fs_projects_set_normalizes_and_enables(monkeypatch):
    captured = {}

    def _apply(config=None, soul=None):
        captured["config"] = config
        return True, ["reloaded"]

    monkeypatch.setitem(sys.modules, "server.agent_init", _fake_module("server.agent_init", _apply_settings_changes=_apply))
    body = _client().post(
        "/api/settings/filesystem-projects",
        json={"projects": [{"path": "~/Documents", "write": True}, {"name": "inbox", "path": "/tmp/inbox"}]},
    ).json()
    assert body["ok"] is True
    fs = captured["config"]["filesystem"]
    assert fs["enabled"] is True
    assert fs["projects"][0]["name"] == "Documents" and fs["projects"][0]["write"] is True
    assert not fs["projects"][0]["path"].startswith("~"), "paths are ~-expanded"
    assert fs["projects"][1] == {"name": "inbox", "path": "/tmp/inbox", "write": False}


def test_fs_projects_set_offloads_apply_off_the_event_loop(monkeypatch):
    """#2210 — the fs-projects settings write must run _apply_settings_changes via
    asyncio.to_thread like every sibling call site (#497): the apply does file I/O plus
    a full graph reload, and calling it synchronously stalls the whole event loop. The
    fake detects the loop by thread: get_running_loop() raises in a to_thread worker."""
    import asyncio as aio

    captured = {}

    def _apply(config=None, soul=None):
        try:
            aio.get_running_loop()
            captured["on_event_loop"] = True
        except RuntimeError:
            captured["on_event_loop"] = False
        return True, ["reloaded"]

    # Complete fake (all three names config_routes imports at module level), so this
    # test also passes when run solo — unlike the sibling fakes, which rely on an
    # earlier test having imported config_routes against the real server.agent_init.
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module(
            "server.agent_init",
            _apply_settings_changes=_apply,
            _build_settings_callbacks=lambda: {},
            _reset_settings_keys=lambda keys: (True, []),
        ),
    )
    body = _client().post("/api/settings/filesystem-projects", json={"projects": [{"path": "/tmp/x"}]}).json()
    assert body["ok"] is True
    assert captured["on_event_loop"] is False, "apply ran synchronously on the event loop"


def test_fs_projects_set_rejections(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    c = _client()
    assert c.post("/api/settings/filesystem-projects", json={"projects": "nope"}).status_code == 400
    assert c.post("/api/settings/filesystem-projects", json={"projects": [{"path": " "}]}).status_code == 400
    assert (
        c.post(
            "/api/settings/filesystem-projects",
            json={"projects": [{"name": "x", "path": "/a"}, {"name": "x", "path": "/b"}]},
        ).status_code
        == 400
    )
