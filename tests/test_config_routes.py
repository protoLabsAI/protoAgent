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


def test_fs_projects_get(monkeypatch, tmp_path):
    _fs_state(monkeypatch, filesystem_enabled=True, filesystem_projects=[{"name": "docs", "path": str(tmp_path), "write": False}])
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
    monkeypatch.setitem(sys.modules, "server.agent_init", _fake_module("server.agent_init", _apply_settings_changes=_apply))
    body = _client().post(
        "/api/settings/filesystem-projects",
        json={"projects": [{"path": "~/Documents", "write": True}, {"name": "inbox", "path": str(inbox)}]},
    ).json()
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

    def _capture_schema(cfg, model_options=None, agent_doc=None, host_doc=None):
        captured["model_options"] = model_options
        return []

    monkeypatch.setattr("graph.settings_schema.build_schema", _capture_schema)

    resp = _client().get("/api/settings/schema").json()
    assert resp == {"groups": []}
    assert captured["model_options"] == discovered
