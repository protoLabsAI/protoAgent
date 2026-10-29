"""Seam guard for the ``server/settings_apply.py`` extraction (#3848).

The settings apply / reset path, its snapshot-rollback helpers, the autostart sync, the
``CONFIG_WRITE_LOCK`` decorator and the console Settings + setup-wizard callbacks moved
out of ``server/agent_init.py`` and are re-exported there, so ``agent_init.<name>`` still
RESOLVES — but a monkeypatch on ``agent_init`` no longer INTERCEPTS a collaborator the
moved code calls by bare name: it resolves in ``settings_apply``'s globals. Such a patch
is dead, and the test around it can pass silently against the real thing. This scans the
suite so a stale target fails loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import server.agent_init as agent_init
import server.settings_apply as settings_apply
from tests._seam_scan import stale_patches

# Names whose only live binding (as a callee / read) is in settings_apply. Deliberately
# absent: ``_apply_settings_changes`` — its published address stays
# ``server.agent_init``: operator_api (config_routes included, #3856) and the devkit plugin
# import it from there at call time, and maintenance_loops / plugin_wiring / the plugin
# host / ``save_all`` call it through agent_init, so a patch on agent_init DOES intercept
# every caller. And
# ``_reload_langgraph_agent``, which never moved (settings_apply calls it through
# agent_init at call time).
_MOVED_COLLABORATORS = {
    "_serialized_config_write",
    "_sync_autostart_with_config",
    "_filter_nested_to_host_keys",
    "_prune_shadowing_agent_keys",
    "_config_files_to_snapshot",
    "_snapshot_config_files",
    "_restore_config_files",
    "_ROLLBACK_NOTE",
    "_WRITE_ANNOUNCEMENTS",
    "_drop_undone_write_messages",
    "_reset_settings_keys",
    "_build_settings_callbacks",
    "_event_bus",  # the plugin.changed publish; agent_init no longer imports it at all
}

_RE_EXPORTED = (_MOVED_COLLABORATORS - {"_event_bus"}) | {"_apply_settings_changes", "_CONFIG_WRITE_LOCK"}
# ``server/__init__`` re-exports these as names only — nothing calls them through the
# package, so a patch on ``server.<name>`` intercepts nothing (``server._event_bus`` is the
# package's OWN bus, not a copy).
_PACKAGE_COPIES = (_MOVED_COLLABORATORS - {"_event_bus"}) | {"_apply_settings_changes"}


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale = stale_patches("server.agent_init", _MOVED_COLLABORATORS)
    assert not stale, "patch these on server.settings_apply, not agent_init (#3848): " + ", ".join(stale)


def test_no_test_patches_the_package_level_copies():
    """``server.<name>`` resolves (re-export) but no caller reads it there (#3856)."""
    stale = stale_patches("server", _PACKAGE_COPIES)
    assert not stale, "patch these on server.agent_init / server.settings_apply, not server: " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """agent_init / server re-export the moved names by identity (no stale copies); the
    config write lock has ONE home (graph.config_io)."""
    import graph.config_io as cio
    import server

    for name in _RE_EXPORTED:
        assert getattr(agent_init, name) is getattr(settings_apply, name), name
    for name in ("_apply_settings_changes", "_sync_autostart_with_config"):
        assert getattr(server, name) is getattr(settings_apply, name), name
    assert settings_apply._CONFIG_WRITE_LOCK is cio.CONFIG_WRITE_LOCK
    assert not hasattr(agent_init, "_event_bus")
    # The seams settings_apply reaches through agent_init must NOT be bound here — a
    # module-level copy would silently bypass a patch on agent_init.
    assert not hasattr(settings_apply, "_reload_langgraph_agent")


def _isolate_config(monkeypatch, tmp_path: Path) -> None:
    import graph.config_io as cio
    import infra.paths as paths

    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    monkeypatch.setattr(paths, "host_config_path", lambda: tmp_path / "host-config.yaml", raising=False)
    monkeypatch.setattr(settings_apply, "_event_bus", type("_Bus", (), {"publish": lambda *a, **k: None})())


def test_reload_is_called_through_agent_init(monkeypatch, tmp_path):
    """``_reload_langgraph_agent`` stays in agent_init; every moved caller looks it up there
    at call time, so the suite's many ``agent_init._reload_langgraph_agent`` patches still
    intercept the apply / reset / finish-setup paths."""
    import graph.config_io as cio

    _isolate_config(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(agent_init, "_reload_langgraph_agent", lambda: calls.append("reload") or (True, "fake"))

    assert settings_apply._apply_settings_changes() == (True, ["fake"])
    assert settings_apply._reset_settings_keys([]) == (True, ["fake"])

    monkeypatch.setattr(cio, "mark_setup_complete", lambda: None)
    monkeypatch.setattr(cio, "reset_setup", lambda: None)
    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"](None, None)
    assert ok and msg.endswith("fake")
    assert calls == ["reload", "reload", "reload"]


def test_save_all_calls_apply_through_agent_init(monkeypatch):
    """``save_all`` reaches ``_apply_settings_changes`` through agent_init — the one patch
    point every caller shares."""
    seen: list = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: seen.append(kw) or (True, ["a", "b"]))
    assert settings_apply._build_settings_callbacks()["save_all"]({"x": 1}, None) == (True, "a • b")
    assert seen == [{"config": {"x": 1}, "soul": None}]


def test_callers_resolve_apply_through_agent_init():
    """maintenance_loops / plugin_wiring / config_routes / the devkit plugin look the apply
    path up on agent_init at call time, never via a module-level ``from … import`` of it;
    and ``server/__init__`` never reads its re-exported copy."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("server/maintenance_loops.py", "server/plugin_wiring.py", "server/settings_apply.py"):
        tree = ast.parse((root / "src" / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"server.agent_init", "server.settings_apply"}:
                raise AssertionError(f"{rel}: module-level import from {node.module}")
    for rel in ("operator_api/config_routes.py", "operator_api/mcp_routes.py", "operator_api/plugin_routes.py"):
        tree = ast.parse((root / "src" / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"server.agent_init", "server.settings_apply"}:
                names = {a.name for a in node.names}
                assert "_apply_settings_changes" not in names, f"{rel}: module-level import of the apply path"
    pkg = ast.parse((root / "src/server/__init__.py").read_text(encoding="utf-8"))
    reads = [
        n.lineno
        for n in ast.walk(pkg)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == "_apply_settings_changes"
    ]
    assert not reads, f"server/__init__ calls its re-exported copy (a patch on agent_init misses it): {reads}"


def _wire_finish_setup(monkeypatch, tmp_path, *, probe):
    """Shared wiring for the finish_setup seam tests (#4566): isolate config I/O and stub
    the persist / autostart / reload collaborators the terminal action calls through the
    config_io module (and settings_apply / agent_init), recording every model-probe call so
    a test can assert the probe did — or did not — run. ``probe`` is the ``(ok, err)`` the
    stubbed ``validate_model_connection`` returns."""
    import graph.config_io as cio
    from graph.config import LangGraphConfig
    from runtime.state import STATE

    _isolate_config(monkeypatch, tmp_path)
    # Explore mode (#4566): the finish's "no model connected yet" note now reflects the
    # RESULTING config — ``model_is_configured(STATE.graph_config)`` after the reload — not the
    # submitted payload. The reload is stubbed here, so STATE.graph_config IS that resulting
    # config: stand up a no-model live config and clear the conftest env key so a no-model
    # finish reads as genuine explore mode. A test that wants the env-default case re-sets
    # OPENAI_API_KEY itself (it stays explore for the probe-skip, which is payload-only).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    rec: dict[str, list] = {"probe": [], "marked": [], "reset": []}

    def _probe(base, key, model, timeout=20.0, allow_env_key=True):
        rec["probe"].append((base, key, model))
        return probe

    monkeypatch.setattr(cio, "validate_model_connection", _probe)
    monkeypatch.setattr(cio, "mark_setup_complete", lambda: rec["marked"].append(True))
    monkeypatch.setattr(cio, "reset_setup", lambda: rec["reset"].append(True))
    # Persist helpers — a seam test writes no real YAML / secrets.
    monkeypatch.setattr(cio, "validate_config_dict", lambda c: (True, ""))
    monkeypatch.setattr(cio, "split_secret_updates", lambda c: (c, {}))
    monkeypatch.setattr(cio, "save_secrets", lambda s: None)
    monkeypatch.setattr(cio, "load_yaml_doc", lambda: {})
    monkeypatch.setattr(cio, "apply_updates_to_yaml", lambda doc, main: None)
    monkeypatch.setattr(cio, "strip_secrets_from_doc", lambda doc: None)
    monkeypatch.setattr(cio, "save_yaml_doc", lambda doc: None)
    monkeypatch.setattr(cio, "write_soul", lambda s: ["/tmp/SOUL.md"])
    # Autostart + reload are reached by bare name through settings_apply / agent_init.
    monkeypatch.setattr(settings_apply, "_sync_autostart_with_config", lambda config: "")
    monkeypatch.setattr(agent_init, "_reload_langgraph_agent", lambda: (True, "reloaded"))
    return rec


def test_finish_setup_without_a_model_skips_the_probe(monkeypatch, tmp_path):
    """Explore mode (#4566): a finish with no connection and a blank model name completes
    ok, marks setup complete, and NEVER probes a model — the model block is present but the
    name is empty, so only the new no-model skip (not the pre-existing isinstance guard)
    keeps the probe from running. The "no model connected yet" note is driven by the RESULTING
    config (no env key here, so model_is_configured is False), not the payload."""
    rec = _wire_finish_setup(monkeypatch, tmp_path, probe=(False, "should never be called"))

    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"]({"model": {"name": ""}}, None)

    assert ok is True
    assert rec["probe"] == []  # validate_model_connection untouched
    assert rec["marked"] == [True]  # setup marked complete
    assert "Settings ▸ Model" in msg and "no model" in msg.lower()


def test_finish_setup_without_a_model_but_env_key_does_not_claim_no_model(monkeypatch, tmp_path):
    """#4566 review: the "no model connected yet" note reflects the RESULTING config, not the
    submitted payload. With OPENAI_API_KEY in the environment the default route resolves it, so
    ``model_is_configured`` of the reloaded config is True — the reload compiles a graph and the
    console reports model_configured=true. The finish must NOT then tell the operator "no model
    connected yet" (the stale, payload-only behaviour). The probe is still skipped: a payload
    that names nothing has nothing to 1-token-complete against."""
    rec = _wire_finish_setup(monkeypatch, tmp_path, probe=(False, "should never be called"))
    # An env default IS a usable model for the resulting config (STATE.graph_config is the
    # stubbed-reload's resulting config, a default LangGraphConfig that resolves this key).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-default")

    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"]({"model": {"name": ""}}, None)

    assert ok is True
    assert rec["probe"] == []  # payload named nothing → still nothing to probe
    assert rec["marked"] == [True]
    assert "no model connected yet" not in msg.lower()  # the env default IS a model


def test_finish_setup_with_a_named_model_still_probes_and_refuses(monkeypatch, tmp_path):
    """A config that NAMES a model is unchanged: it still probes, and a probe failure
    refuses the finish without marking setup complete (#4566 leaves this path intact)."""
    rec = _wire_finish_setup(monkeypatch, tmp_path, probe=(False, "401 bad key"))
    config = {"model": {"name": "gpt-x", "api_base": "http://g/v1", "api_key": "sk-bad"}}

    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"](config, None)

    assert ok is False
    assert len(rec["probe"]) == 1  # the probe ran
    assert rec["marked"] == []  # setup NOT marked complete
    assert "model connection failed" in msg


def test_finish_setup_explore_from_the_shipped_template_lands_declared_empty(monkeypatch, tmp_path):
    """#4566 review: an explore finish started from the SHIPPED template must land a
    DECLARED-EMPTY registry, not keep the template's placeholder ``gateway`` connection.

    ``config/langgraph-config.example.yaml`` declares a ``gateway`` openai-compat connection
    with only a ``base_url`` (and no key). A plain merge would leave it in place, so the
    reloaded config would read ``model_is_configured == True`` on that base_url alone — the
    reload gate would then build a graph that can't authenticate, defeating explore mode. The
    finish must overwrite ``providers`` with ``[]`` so the loaded config is declared-empty.

    This drives the finish through the REAL config round-trip (no stubbed YAML I/O): copy the
    shipped template to a tmp leaf, run the explore finish, then load the persisted leaf back
    and assert the placeholder is gone, the config reads unconfigured, the runtime status
    reports ``model_configured == false``, and the reload gate builds no graph. A declared
    base_url-only connection and a subscription still read configured — those are pinned in
    tests/test_operator_api_runtime.py and ``model_is_configured`` is unchanged here."""
    import shutil

    import graph.config_io as cio
    from graph.config import LangGraphConfig, model_is_configured
    from operator_api.runtime import build_runtime_status
    from runtime.state import STATE

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    example = Path(__file__).resolve().parent.parent / "config" / "langgraph-config.example.yaml"
    leaf = tmp_path / "langgraph-config.yaml"
    shutil.copyfile(example, leaf)

    # Sanity: the shipped template genuinely declares the placeholder gateway and so reads as
    # CONFIGURED on its base_url alone — exactly the shape the finish must neutralise. Without
    # this the rest of the test could pass against a template that never had the bug.
    seed = LangGraphConfig.from_yaml(leaf)
    assert seed.providers_declared is True and [p.id for p in seed.providers] == ["gateway"]
    assert model_is_configured(seed) is True

    # Real persist I/O: only neutralise the no-arg load's one-shot HOME-touching seed/migration
    # (the leaf already exists, so the seed is a no-op) and the non-I/O collaborators. The
    # load/apply/save round-trip itself runs for real against the tmp leaf.
    monkeypatch.setattr(cio, "ensure_live_config", lambda: False)
    monkeypatch.setattr(cio, "validate_config_dict", lambda c: (True, ""))
    monkeypatch.setattr(cio, "save_secrets", lambda s, path=None: None)
    monkeypatch.setattr(cio, "mark_setup_complete", lambda: None)
    monkeypatch.setattr(cio, "reset_setup", lambda: None)
    monkeypatch.setattr(cio, "validate_model_connection", lambda *a, **k: (False, "should never be called"))
    monkeypatch.setattr(settings_apply, "_sync_autostart_with_config", lambda config: "")

    # A faithful stand-in for the reload: load the just-written leaf into STATE.graph_config and
    # build a graph ONLY when a model is configured — the SAME gate agent_init uses (#4566), so
    # "graph built" == "model_is_configured" here too.
    graph_sentinel = object()
    monkeypatch.setattr(STATE, "graph", graph_sentinel, raising=False)

    def _reload():
        STATE.graph_config = LangGraphConfig.from_yaml(cio.config_yaml_path())
        STATE.graph = graph_sentinel if model_is_configured(STATE.graph_config) else None
        return True, "config reloaded"

    monkeypatch.setattr(agent_init, "_reload_langgraph_agent", _reload)

    ok, msg = settings_apply._build_settings_callbacks()["finish_setup"]({"model": {"name": ""}}, None)

    assert ok is True
    # The persisted leaf no longer carries the template's placeholder gateway — it is a
    # DECLARED-EMPTY registry, which is what reads as "no model".
    reloaded = LangGraphConfig.from_yaml(leaf)
    assert reloaded.providers == [] and reloaded.providers_declared is True
    assert model_is_configured(reloaded) is False
    # No graph built in explore mode (the reload gate skipped the compile).
    assert STATE.graph is None
    # The runtime status the console polls reports it as unconfigured.
    status = build_runtime_status(
        config=reloaded,
        setup_complete=True,
        graph_loaded=STATE.graph is not None,
        model_configured=model_is_configured(reloaded),
    )
    assert status["model_configured"] is False
    assert "no model connected yet" in msg.lower()


def test_agent_init_patch_reaches_the_config_routes(monkeypatch):
    """Behavioral proof (#3856): a patch on ``agent_init._apply_settings_changes`` is what
    ``POST /api/config`` and the SOUL-history restore run."""
    import sys
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.config_routes import register_config_routes

    seen: list[dict] = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: seen.append(kw) or (True, ["patched"]))
    cio = types.ModuleType("graph.config_io")
    cio.read_soul_version = lambda vid: "archived persona"
    cio.read_soul = lambda: "current persona"
    app = FastAPI()
    register_config_routes(app)
    client = TestClient(app)

    assert client.post("/api/config", json={"config": {"a": 1}}).json() == {"ok": True, "messages": ["patched"]}
    monkeypatch.setitem(sys.modules, "graph.config_io", cio)
    assert client.post("/api/config/soul/history/v1/restore").json()["messages"] == ["patched"]
    assert seen == [{"config": {"a": 1}, "soul": None}, {"soul": "archived persona"}]
