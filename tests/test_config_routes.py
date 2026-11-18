"""Config / setup / settings routes (ADR 0023 phase 3 extraction) — the
registrar wires the surface and the handlers delegate to config_io /
settings_schema / agent_init as before."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.platform_sensitive

# Imported at COLLECTION, before any test swaps a stub into ``sys.modules["graph.config_io"]``:
# registering the routes imports ``server.settings_apply``, which pulls names from the real
# module at import time. Left to the first stubbed test, that import ran against the stub and
# failed whenever this file ran on its own (#5155).
import server.settings_apply  # noqa: E402, F401


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
    # Asked before a config has loaded: an empty document, not a 500.
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    assert _client().get("/api/config").json() == {"config": {}, "soul": "SOUL"}


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
    import server.agent_init as agent_init

    captured = {}

    def _apply(config=None, soul=None):
        captured["config"], captured["soul"] = config, soul
        return True, ["reloaded"]

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _apply)
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
    import server.agent_init as agent_init

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

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _apply)
    resp = _client().post("/api/settings", json={"updates": {"model.name": "m"}, "layer": "host"}).json()
    assert resp["ok"] is True
    assert captured["layer"] == "host"
    assert captured["config"] == {"nested": {"model.name": "m"}}


@pytest.mark.parametrize("updates, expected_tail", [
    ({"model.name": ["primary", "legacy"]}, []),
    ({"model.name": ["primary"]}, []),
    ({"model.name": "replacement"}, ["legacy"]),
    ({"routing.aux_model": ["aux"]}, ["legacy"]),
    ({"model.name": ["primary"], "routing.fallback_models": ["explicit"]}, ["explicit"]),
])
def test_save_main_chain_retires_legacy_tail_on_reload(monkeypatch, tmp_path, updates, expected_tail):
    from graph.config import LangGraphConfig
    from graph.config_io import apply_updates_to_yaml, load_yaml_doc, save_yaml_doc
    from graph.settings_schema import build_schema
    import runtime.state as rs
    import server.agent_init as agent_init

    path = tmp_path / "config.yaml"
    save_yaml_doc({"model": {"name": "primary"}, "routing": {"fallback_models": ["legacy"]}}, path)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig.from_dict(load_yaml_doc(path)))

    def persist(config=None, layer="agent"):
        doc = apply_updates_to_yaml(load_yaml_doc(path), config)
        save_yaml_doc(doc, path)
        return True, []

    monkeypatch.setattr(agent_init, "_apply_settings_changes", persist)
    response = _client().post("/api/settings", json={"updates": updates}).json()
    assert response["ok"] is True
    reloaded = LangGraphConfig.from_dict(load_yaml_doc(path))
    main = next(f for g in build_schema(reloaded) for f in g["fields"] if f["key"] == "model.name")
    assert main["legacy_fallbacks"] == expected_tail
    expected_main = updates.get("model.name", "primary")
    assert main["value"] == ([expected_main] if isinstance(expected_main, str) else expected_main)


def test_save_settings_defaults_to_agent_layer(monkeypatch):
    """No layer in the body ⇒ the agent leaf (today's behavior)."""
    import server.agent_init as agent_init

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

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _apply)
    _client().post("/api/settings", json={"updates": {"x": 1}})
    assert captured["layer"] == "agent"


def test_reset_settings_pops_known_keys(monkeypatch):
    """POST /api/settings/reset delegates to _reset_settings_keys for known keys."""
    import operator_api.config_routes as cr

    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            is_known_key=lambda k: k == "model.name",
            is_hidden_setting=lambda k, hidden=None: False,
            expand_reset_keys=lambda keys: list(keys),  # no coupling in this fake
        ),
    )
    captured = {}

    def _reset(keys):
        captured["keys"] = keys
        return True, ["reset 1 key(s) to inherited", "reloaded"]

    monkeypatch.setattr(cr, "_reset_settings_keys", _reset)
    resp = _client().post("/api/settings/reset", json={"keys": ["model.name"]}).json()
    assert resp["ok"] is True
    assert captured["keys"] == ["model.name"]


def test_reset_settings_expands_the_model_group(monkeypatch):
    """Resetting any coupled model key resets the whole group (#2528): name alone
    validated the inherited value against a still-overridden provider, so the
    rebuild refused it and the reset rolled back forever. The expansion happens
    BEFORE validation so hidden/known checks cover the pulled keys too, and the
    response says which siblings came along."""
    import operator_api.config_routes as cr
    from graph.settings_schema import MODEL_RESET_GROUP, expand_reset_keys, is_hidden_setting, is_known_key

    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            is_known_key=is_known_key,
            is_hidden_setting=is_hidden_setting,
            expand_reset_keys=expand_reset_keys,
        ),
    )
    captured = {}

    def _reset(keys):
        captured["keys"] = keys
        return True, ["reset 3 key(s) to inherited", "reloaded"]

    monkeypatch.setattr(cr, "_reset_settings_keys", _reset)
    resp = _client().post("/api/settings/reset", json={"keys": ["model.name"]}).json()
    assert resp["ok"] is True
    assert captured["keys"] == list(MODEL_RESET_GROUP)
    assert any("reset as one group" in m for m in resp["messages"])
    assert any("model.provider" in m and "model.api_base" in m for m in resp["messages"])


def test_reset_settings_rejects_unknown_key(monkeypatch):
    """An unknown key is rejected before any disk touch."""
    monkeypatch.setitem(
        sys.modules,
        "graph.settings_schema",
        _fake_module(
            "graph.settings_schema",
            is_known_key=lambda k: False,
            is_hidden_setting=lambda k, hidden=None: False,
            expand_reset_keys=lambda keys: list(keys),
        ),
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
        _fake_module(
            "graph.settings_schema",
            is_known_key=lambda k: True,
            is_hidden_setting=lambda k, hidden=None: True,
            expand_reset_keys=lambda keys: list(keys),
        ),
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
        _fake_module(
            "graph.config_io",
            # Mirrors the real signature (#3128): the route passes `allow_env_key` so a
            # declared connection fails closed. A fake accepting fewer parameters turns
            # signature drift into a TypeError inside a worker thread instead of failing here.
            validate_model_connection=lambda b, k, m, timeout=20.0, allow_env_key=True: (ok, "" if ok else "401"),
        ),
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

    import server.agent_init as agent_init

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _fake_apply)
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

    import server.agent_init as agent_init

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _fake_apply)
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


def test_fs_projects_get(monkeypatch, tmp_path):
    _fs_state(
        monkeypatch,
        filesystem_enabled=True,
        filesystem_projects=[{"name": "docs", "path": str(tmp_path), "write": False}],
    )
    body = _client().get("/api/settings/filesystem-projects").json()
    assert body["enabled"] is True and body["projects"][0]["name"] == "docs"


def test_fs_projects_get_reports_missing_folders(monkeypatch, tmp_path):
    """A root whose folder is gone is SKIPPED when the fs tools are built, and if
    it was the last one the whole toolset unbinds with only a log line. GET reports
    liveness per row so the editor can say which folder went missing."""
    _fs_state(
        monkeypatch,
        filesystem_enabled=True,
        filesystem_projects=[
            {"name": "here", "path": str(tmp_path), "write": False},
            {"name": "gone", "path": str(tmp_path / "nope"), "write": False},
        ],
    )
    rows = _client().get("/api/settings/filesystem-projects").json()["projects"]
    assert [r["exists"] for r in rows] == [True, False]


def test_fs_projects_set_normalizes_and_enables(monkeypatch, tmp_path):
    captured = {}

    def _apply(config=None, soul=None):
        captured["config"] = config
        return True, ["reloaded"]

    docs = tmp_path / "Documents"
    inbox = tmp_path / "inbox"
    docs.mkdir()
    inbox.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expands ~ via USERPROFILE, not HOME
    monkeypatch.setitem(
        sys.modules, "server.agent_init", _fake_module("server.agent_init", _apply_settings_changes=_apply)
    )
    body = (
        _client()
        .post(
            "/api/settings/filesystem-projects",
            json={"projects": [{"path": "~/Documents", "write": True}, {"name": "inbox", "path": str(inbox)}]},
        )
        .json()
    )
    assert body["ok"] is True
    fs = captured["config"]["filesystem"]
    assert fs["enabled"] is True
    assert fs["projects"][0]["name"] == "Documents" and fs["projects"][0]["write"] is True
    assert not fs["projects"][0]["path"].startswith("~"), "paths are ~-expanded"
    assert fs["projects"][1] == {"name": "inbox", "path": str(inbox), "write": False}


def test_fs_projects_set_offloads_apply_off_the_event_loop(monkeypatch, tmp_path):
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
    body = _client().post("/api/settings/filesystem-projects", json={"projects": [{"path": str(tmp_path)}]}).json()
    assert body["ok"] is True
    assert captured["on_event_loop"] is False, "apply ran synchronously on the event loop"


def test_fs_projects_set_rejections(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    c = _client()
    assert c.post("/api/settings/filesystem-projects", json={"projects": "nope"}).status_code == 400
    assert c.post("/api/settings/filesystem-projects", json={"projects": [{"path": " "}]}).status_code == 400
    assert (
        c.post(
            "/api/settings/filesystem-projects",
            json={"projects": [{"name": "x", "path": str(a)}, {"name": "x", "path": str(b)}]},
        ).status_code
        == 400
    )


def test_fs_projects_set_refuses_an_unacknowledged_removal(monkeypatch, tmp_path):
    """POST is replace-all, so a caller that means "add discord-plugin" and posts a
    one-entry list strips every other root — and `filesystem.projects` IS the ADR 0007
    fence, so that is capability loss, silently (#2556). Removals must be asked for."""
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    keep, add = tmp_path / "repo", tmp_path / "plugin"
    keep.mkdir()
    add.mkdir()
    _fs_state(monkeypatch, filesystem_enabled=True, filesystem_projects=[{"name": "repo", "path": str(keep)}])

    r = _client().post("/api/settings/filesystem-projects", json={"projects": [{"path": str(add)}]})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "repo" in detail and str(keep) in detail, detail
    assert "replace" in detail


def test_fs_projects_set_allows_an_acknowledged_removal_and_reports_it(monkeypatch, tmp_path):
    """The console's editor DOES mean replace-all — removing a row is how you delete a
    root. Acknowledged, it goes through, and says what it took away."""
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    keep, drop = tmp_path / "repo", tmp_path / "old"
    keep.mkdir()
    drop.mkdir()
    _fs_state(
        monkeypatch,
        filesystem_enabled=True,
        filesystem_projects=[{"name": "repo", "path": str(keep)}, {"name": "old", "path": str(drop)}],
    )

    r = _client().post(
        "/api/settings/filesystem-projects",
        json={"projects": [{"path": str(keep)}], "replace": True},
    )

    assert r.status_code == 200
    assert [e["name"] for e in r.json()["removed"]] == ["old"]


def test_fs_projects_set_adding_a_root_needs_no_acknowledgement(monkeypatch, tmp_path):
    """The additive case stays a plain POST — the gate is on removal, not on writing."""
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    keep, add = tmp_path / "repo", tmp_path / "plugin"
    keep.mkdir()
    add.mkdir()
    _fs_state(monkeypatch, filesystem_enabled=True, filesystem_projects=[{"name": "repo", "path": str(keep)}])

    r = _client().post(
        "/api/settings/filesystem-projects",
        json={"projects": [{"name": "repo", "path": str(keep)}, {"name": "plugin", "path": str(add)}]},
    )

    assert r.status_code == 200 and r.json()["removed"] == []


def test_fs_projects_set_refuses_unusable_folders(monkeypatch, tmp_path):
    """The save path used to accept ANY non-blank string. build_fs_tools drops
    every root that isn't a directory, and an empty registry unbinds the ENTIRE fs
    toolset, so one fat-fingered folder silently took read_file/list_dir/write_file
    away from the agent. Reject at the door instead, with a reason."""
    monkeypatch.setitem(
        sys.modules,
        "server.agent_init",
        _fake_module("server.agent_init", _apply_settings_changes=lambda config=None, soul=None: (True, [])),
    )
    c = _client()
    # Relative — would resolve against the SERVER's cwd ("/" under the desktop sidecar).
    rel = c.post("/api/settings/filesystem-projects", json={"projects": [{"path": "daf sda f s"}]})
    assert rel.status_code == 400 and "absolute" in rel.json()["detail"]
    # Absolute but nonexistent.
    missing = c.post("/api/settings/filesystem-projects", json={"projects": [{"path": str(tmp_path / "nope")}]})
    assert missing.status_code == 400 and "no such folder" in missing.json()["detail"]
    # Absolute and existing, but a FILE — resolve() succeeds, the fence needs a dir.
    f = tmp_path / "notes.txt"
    f.write_text("x")
    assert c.post("/api/settings/filesystem-projects", json={"projects": [{"path": str(f)}]}).status_code == 400


# ── GET /api/projects — the ADR 0095 managed-projects registry (read-only) ────


def _projects_state(monkeypatch, **attrs):
    """Real LangGraphConfig (not a SimpleNamespace) so fenced_projects() is live."""
    import runtime.state as rs

    from graph.config import LangGraphConfig

    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(**attrs), raising=False)


def test_projects_get_reports_registry_and_liveness(monkeypatch, tmp_path):
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "here", "path": str(tmp_path), "github": "o/here"},
            {"name": "gone", "path": str(tmp_path / "nope")},
        ],
    )
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "registry"
    assert [r["exists"] for r in body["projects"]] == [True, False]
    assert body["projects"][0]["github"] == "o/here"  # identity fields survive
    # A registered folder that isn't there contributes NOTHING to the real fence —
    # fenced_projects() is a pure config projection, but _registry_from_config drops
    # roots that aren't directories. Reporting it as fenced would be the same
    # declared-vs-enforced lie this endpoint exists to expose.
    assert [r["fenced"] for r in body["projects"]] == [True, False]


def test_projects_get_flags_fs_false_as_unfenced(monkeypatch, tmp_path):
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "fenced", "path": str(tmp_path)},
            {"name": "tracked", "path": str(tmp_path), "fs": False},
        ],
    )
    rows = _client().get("/api/projects").json()["projects"]
    assert [r["fenced"] for r in rows] == [True, False]


def test_projects_get_says_when_explicit_roots_shadow_the_registry(monkeypatch, tmp_path):
    """An explicit filesystem.projects WINS over the registry (that's what makes ADR
    0095 non-regressing) — so a fully-populated registry can be driving nothing.
    Reporting that is the whole point: silent divergence between declared and
    enforced is the failure 0095 exists to kill."""
    _projects_state(
        monkeypatch,
        filesystem_projects=[{"name": "legacy", "path": str(tmp_path), "write": True}],
        projects=[{"name": "ignored", "path": str(tmp_path)}],
    )
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "explicit"
    assert body["projects"][0]["fenced"] is False  # registered, but NOT in effect


def test_projects_get_workspace_default_and_disabled(monkeypatch):
    _projects_state(monkeypatch)  # nothing registered, fs on
    assert _client().get("/api/projects").json()["fence_source"] == "workspace_default"

    _projects_state(monkeypatch, filesystem_enabled=False, projects=[{"name": "a", "path": "/tmp"}])
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "disabled" and body["enabled"] is False
    assert body["projects"][0]["fenced"] is False


def test_projects_get_marks_only_the_first_of_a_duplicate_name(monkeypatch, tmp_path):
    """fenced_projects() keeps the FIRST of a duplicate name, so the API must not
    report both rows as fenced — only one root is actually reachable."""
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "dup", "path": str(tmp_path)},
            {"name": "dup", "path": str(tmp_path)},
        ],
    )
    rows = _client().get("/api/projects").json()["projects"]
    assert [r["fenced"] for r in rows] == [True, False]


def test_projects_get_reports_unbound_when_every_folder_is_gone(monkeypatch, tmp_path):
    """A registry that projects rows but whose paths are all missing resolves to an
    EMPTY fence — build_fs_tools then unbinds the whole toolset (#2251). Reporting
    "registry" there would claim a fence that doesn't exist."""
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "gone", "path": str(tmp_path / "nope")},
            {"name": "alsogone", "path": str(tmp_path / "nope2")},
        ],
    )
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "unbound"
    assert [r["fenced"] for r in body["projects"]] == [False, False]


def test_projects_get_stays_registry_when_one_folder_survives(monkeypatch, tmp_path):
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "here", "path": str(tmp_path)},
            {"name": "gone", "path": str(tmp_path / "nope")},
        ],
    )
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "registry"
    assert [r["fenced"] for r in body["projects"]] == [True, False]


def test_projects_get_reports_unbound_when_every_entry_opts_out(monkeypatch, tmp_path):
    """`fs: false` everywhere is now honoured literally — no workspace-default
    substitution — so the API must say the fence is unbound rather than pretending
    a registry is driving it."""
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "a", "path": str(tmp_path), "fs": False},
            {"name": "b", "path": str(tmp_path), "fs": False},
        ],
    )
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "unbound"
    assert [r["fenced"] for r in body["projects"]] == [False, False]


def test_projects_get_does_not_credit_a_duplicate_with_a_different_path(monkeypatch, tmp_path):
    """The nastiest divergence: fenced_projects() keeps the FIRST duplicate, which
    only validates absoluteness — not existence. So [{dup,/missing},{dup,/exists}]
    puts /missing in the fence, fs_tools drops it as a non-directory, and the REAL
    fence is empty. Matching rows by name alone credited the /exists row as live —
    exactly the declared-vs-enforced lie this endpoint exists to expose."""
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "dup", "path": str(tmp_path / "missing")},
            {"name": "dup", "path": str(tmp_path)},  # exists, but NOT the fenced entry
        ],
    )
    body = _client().get("/api/projects").json()
    assert [r["fenced"] for r in body["projects"]] == [False, False]
    assert body["fence_source"] == "unbound"


def test_projects_get_matches_a_tilde_row_against_its_expanded_fence_entry(monkeypatch, tmp_path):
    """fenced_projects() expands `~`, so a row written as `~/x` has to be compared
    against the EXPANDED path or it would never match its own fence entry."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expands ~ via USERPROFILE, not HOME
    (tmp_path / "proj").mkdir()
    _projects_state(monkeypatch, projects=[{"name": "p", "path": "~/proj"}])
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "registry"
    assert body["projects"][0]["fenced"] is True


# ── PATCH /api/projects/{name} — switch a project's access mode (projectBoard-plugin#783) ──


def _wire_project_apply(monkeypatch, *, ok=True, messages=(), before_locked_read=lambda: None):
    """Stand in for the server's ``HOST.apply_settings`` wiring: record each settings
    patch and (on success) fold it into ``STATE.graph_config`` the way the real
    apply + reload does, so a following GET reads the new state."""
    from dataclasses import replace

    from graph.plugins.host import HOST
    import runtime.state as rs

    applied: list[dict] = []

    def _apply(updates):
        if callable(updates):  # run under the config write lock, against the committed config
            before_locked_read()
            updates = updates(rs.STATE.graph_config)
        if updates is None:  # another writer already made the change — nothing to write
            return ok, list(messages)
        applied.append(updates)
        if ok:
            changes = {}
            if "projects" in updates:
                changes["projects"] = updates["projects"]
            if "projects" in (updates.get("filesystem") or {}):
                changes["filesystem_projects"] = updates["filesystem"]["projects"]
            rs.STATE.graph_config = replace(rs.STATE.graph_config, **changes)
        return ok, list(messages)

    monkeypatch.setattr(HOST, "config", lambda: rs.STATE.graph_config)
    monkeypatch.setattr(HOST, "apply_settings", _apply)
    return applied


def test_project_patch_raises_a_read_only_registry_entry_to_read_write(monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "app", "path": str(tmp_path), "github": "o/app", "write": False},
            {"name": "other", "path": str(other)},
        ],
    )
    applied = _wire_project_apply(monkeypatch)
    client = _client()

    r = client.patch("/api/projects/app", json={"write": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "app", "path": str(tmp_path), "write": True}
    # ONE patch, touching only the registry, changing only `write` on the named entry.
    assert applied == [
        {
            "projects": [
                {"name": "app", "path": str(tmp_path), "github": "o/app", "write": True},
                {"name": "other", "path": str(other)},
            ]
        }
    ]
    rows = {p["name"]: p for p in client.get("/api/projects").json()["projects"]}
    assert rows["app"]["write"] is True
    assert "write" not in rows["other"]


def test_project_patch_lowers_back_and_a_same_mode_patch_writes_nothing(monkeypatch, tmp_path):
    _projects_state(monkeypatch, projects=[{"name": "app", "path": str(tmp_path), "write": True}])
    applied = _wire_project_apply(monkeypatch)
    client = _client()

    r = client.patch("/api/projects/app", json={"write": False})
    assert r.status_code == 200, r.text
    assert r.json()["write"] is False
    assert applied == [{"projects": [{"name": "app", "path": str(tmp_path), "write": False}]}]
    assert client.get("/api/projects").json()["projects"][0]["write"] is False

    # Same mode again: 200, and nothing reaches the settings applier.
    r = client.patch("/api/projects/app", json={"write": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "app", "path": str(tmp_path), "write": False}
    assert len(applied) == 1


def test_project_patch_reads_a_registry_entrys_mode_the_way_the_fence_projects_it(monkeypatch, tmp_path):
    """`fenced_projects()` reads a registry `write` with `_falsey`: a hand-edited string
    "false" is read-only and a MISSING key is read-write. The same-mode check must agree,
    or a PATCH answers 200 "already" while the enforced mode stays the opposite."""
    import runtime.state as rs

    other = tmp_path / "other"
    other.mkdir()
    rows = [{"name": "app", "path": str(tmp_path), "write": "false"}, {"name": "other", "path": str(other)}]
    _projects_state(monkeypatch, projects=rows)
    applied = _wire_project_apply(monkeypatch)
    client = _client()
    for name, same in (("app", False), ("other", True)):  # already that mode, as enforced
        assert client.patch(f"/api/projects/{name}", json={"write": same}).status_code == 200
    assert applied == []
    for name, same in (("app", False), ("other", True)):
        assert client.patch(f"/api/projects/{name}", json={"write": not same}).status_code == 200
    assert applied[-1]["projects"] == [{**rows[0], "write": True}, {**rows[1], "write": False}]
    assert [p["write"] for p in rs.STATE.graph_config.fenced_projects()] == [True, False]


def test_project_patch_keeps_a_change_another_writer_committed_first(monkeypatch, tmp_path):
    """The lists are re-read inside the apply (under the host's config write lock), not
    written back from the read the request started with — so an overlapping PATCH or
    an agent registration that committed in between is not reverted."""
    from dataclasses import replace

    import runtime.state as rs

    app = {"name": "app", "path": str(tmp_path), "write": False}
    other = {"name": "other", "path": str(tmp_path / "other"), "write": False}
    new = {"name": "new", "path": str(tmp_path / "new"), "write": False}
    _projects_state(monkeypatch, projects=[app, other])

    def _other_writer():  # commits `other` → read-write and registers `new`
        rs.STATE.graph_config = replace(rs.STATE.graph_config, projects=[app, {**other, "write": True}, new])

    applied = _wire_project_apply(monkeypatch, before_locked_read=_other_writer)
    assert _client().patch("/api/projects/app", json={"write": True}).status_code == 200
    assert applied == [{"projects": [{**app, "write": True}, {**other, "write": True}, new]}]

    # The identical change landing first leaves nothing of ours to write: still a 200.
    _projects_state(monkeypatch, projects=[app, other])
    applied.clear()
    assert _client().patch("/api/projects/other", json={"write": True}).status_code == 200
    assert applied == []


def test_project_patch_flips_the_mirrored_explicit_fence_entry_and_nothing_else(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _projects_state(
        monkeypatch,
        projects=[
            {"name": "app", "path": str(tmp_path), "default_branch": "main", "write": False},
            {"name": "legacy", "path": str(legacy), "write": True},
        ],
        filesystem_projects=[
            {"name": "legacy", "path": str(legacy), "write": True},
            {"name": "app", "path": str(tmp_path), "write": False, "github": "o/app"},
        ],
    )
    applied = _wire_project_apply(monkeypatch)

    r = _client().patch("/api/projects/app", json={"write": True})
    assert r.status_code == 200, r.text
    assert applied == [
        {
            "projects": [
                {"name": "app", "path": str(tmp_path), "default_branch": "main", "write": True},
                {"name": "legacy", "path": str(legacy), "write": True},
            ],
            # Only the list is sent — no `enabled`, no added entry, same order.
            "filesystem": {
                "projects": [
                    {"name": "legacy", "path": str(legacy), "write": True},
                    {"name": "app", "path": str(tmp_path), "write": True, "github": "o/app"},
                ]
            },
        }
    ]


def test_project_patch_does_not_materialize_the_registry_into_the_fence(monkeypatch, tmp_path):
    """An explicit fence that does NOT mirror the project is left alone: PATCH edits
    `write` in place and never appends a fence entry."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _projects_state(
        monkeypatch,
        projects=[{"name": "app", "path": str(tmp_path), "write": False}],
        filesystem_projects=[{"name": "legacy", "path": str(legacy)}],
    )
    applied = _wire_project_apply(monkeypatch)
    assert _client().patch("/api/projects/app", json={"write": True}).status_code == 200
    assert applied == [{"projects": [{"name": "app", "path": str(tmp_path), "write": True}]}]


def test_project_patch_finds_a_fence_only_entry_by_name(monkeypatch, tmp_path):
    _projects_state(monkeypatch, filesystem_projects=[{"name": "legacy", "path": str(tmp_path), "write": False}])
    applied = _wire_project_apply(monkeypatch)
    r = _client().patch("/api/projects/legacy", json={"write": True})
    assert r.status_code == 200, r.text
    assert applied == [{"filesystem": {"projects": [{"name": "legacy", "path": str(tmp_path), "write": True}]}}]


@pytest.mark.parametrize("selected", ["second", "stale"])
def test_project_patch_selects_duplicate_name_by_path_without_fallback(monkeypatch, tmp_path, selected):
    projects = [
        {"name": "app", "path": str(tmp_path / folder), "write": False}
        for folder in ("first", "second")
    ]
    _projects_state(monkeypatch, projects=projects)
    applied = _wire_project_apply(monkeypatch)
    client = _client()
    response = client.patch(
        "/api/projects/app", params={"path": str(tmp_path / selected)}, json={"write": True}
    )
    assert response.status_code == (200 if selected == "second" else 404)
    expected = [projects[0], {**projects[1], "write": selected == "second"}]
    rows = client.get("/api/projects").json()["projects"]
    assert rows[0]["write"] is False
    assert rows[1]["write"] is (selected == "second")
    assert applied == ([{"projects": expected}] if selected == "second" else [])


def test_project_patch_unknown_name_is_a_404(monkeypatch, tmp_path):
    _projects_state(monkeypatch, projects=[{"name": "app", "path": str(tmp_path)}])
    applied = _wire_project_apply(monkeypatch)
    r = _client().patch("/api/projects/nope", json={"write": True})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]
    assert applied == []


@pytest.mark.parametrize("body", [{}, {"write": "true"}, {"write": 1}, {"write": None}, {"other": True}])
def test_project_patch_rejects_a_missing_or_non_bool_write(monkeypatch, tmp_path, body):
    _projects_state(monkeypatch, projects=[{"name": "app", "path": str(tmp_path)}])
    applied = _wire_project_apply(monkeypatch)
    r = _client().patch("/api/projects/app", json=body)
    assert r.status_code == 400
    assert applied == []


def test_project_patch_surfaces_a_failed_apply_as_a_500(monkeypatch, tmp_path):
    _projects_state(monkeypatch, projects=[{"name": "app", "path": str(tmp_path), "write": False}])
    _wire_project_apply(monkeypatch, ok=False, messages=["config.yaml is not writable"])
    r = _client().patch("/api/projects/app", json={"write": True})
    assert r.status_code == 500
    assert "config.yaml is not writable" in r.json()["detail"]
    assert _client().get("/api/projects").json()["projects"][0].get("write") is not True


def test_available_models_route_reports_every_lane(monkeypatch):
    """The picker's endpoint: one call, every lane, each option carrying the qualified
    name that selects it — so the console never composes that string itself."""
    import types as _t

    import runtime.state as rs
    from graph.providers import discovery

    monkeypatch.setattr(
        rs.STATE, "graph_config", _t.SimpleNamespace(api_base="https://gw/v1", api_key="k"), raising=False
    )
    monkeypatch.setattr("graph.config_io.list_gateway_models", lambda base, key, **kw: (["protolabs/coder"], ""))
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, p == "anthropic-oauth", "s", "", "hint")
    )
    monkeypatch.setattr(discovery, "list_provider_models", lambda provider, cfg: (["claude-sonnet-5"], ""))

    body = _client().get("/api/config/models/available").json()

    assert body["options"] == ["gateway:protolabs/coder", "anthropic-oauth:claude-sonnet-5"]
    lanes = {lane["provider"]: lane for lane in body["lanes"]}
    assert lanes["openai-codex"]["configured"] is False  # reported, not omitted
    assert lanes["openai-codex"]["error"]


def test_available_models_route_is_safe_before_setup(monkeypatch):
    """No config yet (fresh install, wizard not finished) → an empty answer, not a 500."""
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)

    assert _client().get("/api/config/models/available").json() == {"lanes": [], "options": []}


def test_settings_schema_lists_native_oauth_models(monkeypatch, tmp_path):
    """#2473: with a native OAuth provider configured, the schema's model.name
    options must come from subscription discovery (list_provider_models), not the
    gateway probe — /model and the composer read the schema, so a gateway-only
    probe collapsed their pickers to one card while Settings ▸ Get models saw nine."""
    import types as _t

    import runtime.state as rs

    monkeypatch.setattr(
        rs.STATE,
        "graph_config",
        _t.SimpleNamespace(model_provider="openai-codex", api_base="", api_key=""),
        raising=False,
    )
    discovered = [f"gpt-5.6-sol-{i}" for i in range(9)]
    monkeypatch.setattr(
        "graph.providers.discovery.list_provider_models",
        lambda provider, cfg: (discovered, ""),
    )

    def _gateway_probe_must_not_run(base, key):  # the pre-#2473 path
        raise AssertionError("gateway probe called for a native OAuth provider")

    monkeypatch.setattr("graph.config_io.list_gateway_models", _gateway_probe_must_not_run)
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: tmp_path / "absent.yaml")
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    captured = {}

    def _capture_schema(cfg, model_options=None, slot_model_options=None, agent_doc=None, host_doc=None):
        captured["model_options"] = model_options
        captured["slot_model_options"] = slot_model_options
        return []

    monkeypatch.setattr("graph.settings_schema.build_schema", _capture_schema)

    resp = _client().get("/api/settings/schema").json()
    assert resp == {"groups": []}
    assert captured["model_options"] == discovered


def test_finish_setup_delegates_offloaded(monkeypatch):
    """#2486 coverage gap: POST /api/config/setup forwards config+soul to the
    finish_setup callback and returns its outcome."""
    import operator_api.config_routes as cr

    seen = {}

    def _finish(config, soul):
        seen["config"], seen["soul"] = config, soul
        return True, "agent is ready"

    monkeypatch.setattr(cr, "_build_settings_callbacks", lambda: {"finish_setup": _finish})
    body = _client().post("/api/config/setup", json={"config": {"a": 1}, "soul": "S"}).json()
    assert body == {"ok": True, "message": "agent is ready"}
    assert seen == {"config": {"a": 1}, "soul": "S"}


def test_finish_setup_records_the_host_capability_contract(monkeypatch):
    """#2277 / ADR 0100: the wizard's host path had no twin of the member's
    workspace.yaml `requires_tools`, so a wizard-installed Project Manager never got
    the contract banner. POST /api/config/setup now persists it — after a successful
    finish only."""
    import operator_api.config_routes as cr
    from graph.config_io import read_host_archetype

    monkeypatch.setattr(cr, "_build_settings_callbacks", lambda: {"finish_setup": lambda c, s: (True, "ready")})
    body = (
        _client()
        .post(
            "/api/config/setup",
            json={"config": {}, "soul": "S", "requires_tools": ["github_create_issue", " ", ""]},
        )
        .json()
    )
    assert body == {"ok": True, "message": "ready"}
    assert read_host_archetype() == {"requires_tools": ["github_create_issue"]}


def test_finish_setup_failure_leaves_no_contract_behind(monkeypatch):
    import operator_api.config_routes as cr
    from graph.config_io import read_host_archetype

    monkeypatch.setattr(
        cr, "_build_settings_callbacks", lambda: {"finish_setup": lambda c, s: (False, "model connection failed")}
    )
    body = _client().post("/api/config/setup", json={"config": {}, "soul": "S", "requires_tools": ["x"]}).json()
    assert body["ok"] is False
    assert read_host_archetype() is None


def test_finish_setup_without_a_contract_clears_a_stale_record(monkeypatch):
    """A contract-free persona (the console sends `[]`; an omitted field defaults to
    the same) must not keep yesterday's contract from an earlier wizard run."""
    import operator_api.config_routes as cr
    from graph.config_io import read_host_archetype, write_host_archetype

    monkeypatch.setattr(cr, "_build_settings_callbacks", lambda: {"finish_setup": lambda c, s: (True, "ready")})

    write_host_archetype(["github_create_issue"])
    _client().post("/api/config/setup", json={"config": {}, "soul": "S", "requires_tools": []})
    assert read_host_archetype() is None

    write_host_archetype(["github_create_issue"])
    _client().post("/api/config/setup", json={"config": {}, "soul": "S"})
    assert read_host_archetype() is None


def test_finish_setup_reports_an_unwritable_contract_instead_of_500ing(monkeypatch):
    """Setup is already marked complete by the time the contract is written, so a
    read-only config dir must surface in the message, not un-finish setup with a 500."""
    import operator_api.config_routes as cr

    def _boom(_tools):
        raise OSError("read-only file system")

    monkeypatch.setattr(cr, "_build_settings_callbacks", lambda: {"finish_setup": lambda c, s: (True, "ready")})
    monkeypatch.setattr("graph.config_io.write_host_archetype", _boom)
    resp = _client().post("/api/config/setup", json={"config": {}, "soul": "S", "requires_tools": ["x"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "archetype contract not recorded: read-only file system" in body["message"]


def test_config_explain_delegates(monkeypatch):
    """#2486 coverage gap: GET /api/config/explain returns build_config_explain's
    dict for the LIVE config verbatim."""
    import runtime.state as rs

    cfg = object()
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    seen = {}

    def _explain(c):
        seen["cfg"] = c
        return {"identity": "x", "paths": {}}

    monkeypatch.setattr("graph.config_explain.build_config_explain", _explain)
    body = _client().get("/api/config/explain").json()
    assert body == {"identity": "x", "paths": {}}
    assert seen["cfg"] is cfg


def test_read_preset_404_on_unknown_200_on_known(monkeypatch):
    """#2486: an unknown preset name is a 404 — the old empty-string 200 was
    indistinguishable from a real-but-blank preset. Membership decides."""
    monkeypatch.setattr("graph.config_io.list_soul_presets", lambda: ["cowork", "blank-slate"])
    monkeypatch.setattr("graph.config_io.read_soul_preset", lambda name: f"# {name}")
    c = _client()
    assert c.get("/api/config/presets/nope").status_code == 404
    ok = c.get("/api/config/presets/cowork")
    assert ok.status_code == 200 and ok.json() == {"name": "cowork", "content": "# cowork"}


def test_projects_route_survives_none_config(monkeypatch):
    """Regression pin for a review claim that turned out FALSE (#2486): every
    config access in _api_projects is getattr-defaulted / hasattr-guarded, so a
    pre-setup None config yields a clean disabled-fence response, not a 500."""
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    body = _client().get("/api/projects").json()
    assert body["fence_source"] == "disabled"
    assert body["projects"] == []


def test_reset_setup_offloaded(monkeypatch):
    seen = []
    monkeypatch.setattr("graph.config_io.reset_setup", lambda: seen.append(1))
    body = _client().post("/api/config/reset-setup").json()
    assert body["ok"] is True and seen == [1]


def test_save_plugin_restart_field_reports_real_restart_keys(monkeypatch):
    from types import SimpleNamespace

    import graph.settings_schema as schema
    import server.agent_init as agent_init

    plugin = SimpleNamespace(section="boardy", defaults={"workers": 2}, test=False)
    monkeypatch.setattr(
        schema,
        "_plugin_field_specs",
        lambda: [
            (plugin, "boardy.workers", "workers", {"key": "workers", "type": "number", "restart": True}),
        ],
    )
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda config=None, layer="agent": (True, []))
    resp = _client().post("/api/settings", json={"updates": {"boardy.workers": 4}}).json()
    assert resp["ok"] is True
    assert resp["restart_required"] == ["boardy.workers"]
