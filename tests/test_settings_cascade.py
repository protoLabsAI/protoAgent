"""App→Host→Agent settings cascade (ADR 0047, slice 2).

from_yaml now overlays the agent (leaf) YAML on the box-shared Host layer
(host-config.yaml, filtered to host-scoped FIELDS keys). These lock the cascade's
behavior: host defaults inherited, agent overrides win (git-style), the host file
can't inject agent-only keys, a corrupt host file degrades, and — critically —
no host file is byte-identical to the pre-cascade parse (zero-migration).

PROTOAGENT_HOST_CONFIG is defaulted to an absent path by conftest, so tests start
with NO host layer and opt in by pointing it at a temp file.
"""

import logging
import textwrap
from pathlib import Path

from graph.config import LangGraphConfig


def _agent_yaml(tmp_path: Path, body: str) -> str:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def _host_yaml(tmp_path: Path, body: str, monkeypatch) -> None:
    hp = tmp_path / "host-config.yaml"
    hp.write_text(textwrap.dedent(body))
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hp))


def test_no_host_file_collapses_to_agent_only(tmp_path):
    """Zero-migration: with no host file, from_yaml parses exactly the agent doc."""
    path = _agent_yaml(tmp_path, "model:\n  name: agent-model\ngoal:\n  enabled: false\n")
    cfg = LangGraphConfig.from_yaml(path)
    # Identical to parsing the agent doc directly (the pre-cascade input).
    assert cfg.model_name == "agent-model"
    assert cfg.goal_enabled is False
    # Untouched host-scoped fields fall back to the dataclass (App) default.
    assert cfg.model_provider == LangGraphConfig.model_provider


def test_host_default_inherited_when_agent_silent(tmp_path, monkeypatch):
    """A host-scoped field set only in host-config.yaml flows to the agent."""
    _host_yaml(tmp_path, "model:\n  name: host-default-model\n  api_base: http://host-gw/v1\n", monkeypatch)
    path = _agent_yaml(tmp_path, "goal:\n  enabled: true\n")  # agent silent on model
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.model_name == "host-default-model"  # inherited from Host
    assert cfg.api_base == "http://host-gw/v1"


def test_agent_overrides_host(tmp_path, monkeypatch):
    """Git-style: the agent leaf wins over the host default for the same field."""
    _host_yaml(tmp_path, "model:\n  name: host-default-model\n", monkeypatch)
    path = _agent_yaml(tmp_path, "model:\n  name: agent-picked-model\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.model_name == "agent-picked-model"  # agent wins


def test_host_cannot_inject_agent_scoped_key(tmp_path, monkeypatch):
    """The host file is filtered to host-scoped keys — an agent-scoped key in it
    (goal.enabled) is dropped, not applied."""
    _host_yaml(tmp_path, "goal:\n  enabled: false\nmodel:\n  name: host-model\n", monkeypatch)
    path = _agent_yaml(tmp_path, "skills:\n  top_k: 3\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.goal_enabled is True  # host's agent-scoped goal.enabled IGNORED (default)
    assert cfg.model_name == "host-model"  # host's host-scoped key DID apply


def test_agent_shadowing_host_key_warns(tmp_path, monkeypatch, caplog):
    """A host-scoped key set in BOTH layers with differing values logs a shadow warning
    (issue #1459): the agent wins, so the box default is silently overridden — surface it."""
    _host_yaml(tmp_path, "model:\n  api_base: http://host-gw/v1\n", monkeypatch)
    path = _agent_yaml(tmp_path, "model:\n  api_base: http://agent-gw/v1\n")
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_base == "http://agent-gw/v1"  # agent wins (unchanged behavior)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("shadow" in m.lower() and "model.api_base" in m for m in msgs), msgs


def test_no_shadow_warning_when_values_match(tmp_path, monkeypatch, caplog):
    """No noise when the agent leaf merely repeats the host value — nothing is shadowed."""
    _host_yaml(tmp_path, "model:\n  api_base: http://same-gw/v1\n", monkeypatch)
    path = _agent_yaml(tmp_path, "model:\n  api_base: http://same-gw/v1\n")
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        LangGraphConfig.from_yaml(path)
    assert not any("shadow" in r.getMessage().lower() for r in caplog.records)


def test_no_shadow_warning_when_agent_silent(tmp_path, monkeypatch, caplog):
    """A host default the agent never sets is plain inheritance, not a shadow — no warning."""
    _host_yaml(tmp_path, "model:\n  api_base: http://host-gw/v1\n", monkeypatch)
    path = _agent_yaml(tmp_path, "goal:\n  enabled: true\n")
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_base == "http://host-gw/v1"  # inherited
    assert not any("shadow" in r.getMessage().lower() for r in caplog.records)


def test_host_cannot_set_a_secret(tmp_path, monkeypatch):
    """Secrets are agent-leaf only (D5): model.api_key is not host-scoped, so a host
    file can't set it — it's filtered out."""
    _host_yaml(tmp_path, "model:\n  api_key: SHOULD_NOT_LEAK\n  name: host-model\n", monkeypatch)
    path = _agent_yaml(tmp_path, "model:\n  temperature: 0.5\n")
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.api_key == ""  # host api_key dropped (no leaf secret either)
    assert cfg.model_name == "host-model"


def test_corrupt_host_file_degrades_without_crashing(tmp_path, monkeypatch):
    """A malformed host file is ignored (warn), not fatal — cascade collapses to leaf."""
    hp = tmp_path / "host-config.yaml"
    hp.write_text("model:\n  name: [unclosed\n")  # invalid YAML
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hp))
    path = _agent_yaml(tmp_path, "model:\n  name: agent-model\n")
    cfg = LangGraphConfig.from_yaml(path)  # must not raise
    assert cfg.model_name == "agent-model"


def test_host_only_no_agent_file(tmp_path, monkeypatch):
    """Host default applies even when the agent leaf file doesn't exist yet."""
    _host_yaml(tmp_path, "model:\n  name: host-model\n", monkeypatch)
    cfg = LangGraphConfig.from_yaml(str(tmp_path / "absent-langgraph-config.yaml"))
    assert cfg.model_name == "host-model"


def test_deep_nested_host_key_merges(tmp_path, monkeypatch):
    """A deep host-scoped key (prompt_cache.warm.enabled) merges with agent-set
    siblings rather than clobbering the section."""
    _host_yaml(tmp_path, "prompt_cache:\n  warm:\n    enabled: true\n", monkeypatch)
    # prompt_cache.* are all host-scoped, so set the sibling in the HOST file too and
    # confirm the deep merge keeps both leaves.
    cfg = LangGraphConfig.from_yaml(str(tmp_path / "none.yaml"))
    assert cfg.cache_warming_enabled is True


def test_no_host_file_matches_from_dict(tmp_path):
    """Belt-and-suspenders: no-host from_yaml == from_dict on the same doc, field-by-field
    over the dataclass (proves the cascade adds nothing when the host layer is empty)."""
    body = "model:\n  name: m\n  temperature: 0.7\ngoal:\n  max_iterations: 11\ncompaction:\n  enabled: false\n"
    path = _agent_yaml(tmp_path, body)
    import yaml as _yaml

    doc = _yaml.safe_load(open(path))
    via_yaml = LangGraphConfig.from_yaml(path)
    via_dict = LangGraphConfig.from_dict(doc, config_dir=tmp_path)
    import dataclasses

    for f in dataclasses.fields(via_yaml):
        if f.name == "plugin_config":
            continue  # resolution is config_dir-relative; equal here but skip to be safe
        assert getattr(via_yaml, f.name) == getattr(via_dict, f.name), f.name


def test_build_schema_reports_scope_and_source():
    """build_schema (slice 3a) tags each field with its cascade scope + the layer
    its live value came from — the data the UI's inherited-vs-overridden badge needs."""
    from graph.settings_schema import build_schema

    cfg = LangGraphConfig()  # App defaults
    groups = build_schema(
        cfg,
        # goal.max_iterations is an agent-scoped knob (goal.enabled is ui_hidden now —
        # goal mode is always on — so it no longer appears in build_schema output).
        agent_doc={"goal": {"max_iterations": 5}},  # agent leaf sets an agent-scoped key
        host_doc={"model": {"name": "host-m"}},  # host layer sets a host-scoped key
    )
    by_key = {e["key"]: e for g in groups for e in g["fields"]}

    # scope reflects ADR 0047 §2.1
    assert by_key["model.name"]["scope"] == "host"
    assert by_key["goal.max_iterations"]["scope"] == "agent"

    # source = the layer the live value came from
    assert by_key["model.name"]["source"] == "host"  # inherited from Host
    assert by_key["goal.max_iterations"]["source"] == "agent"  # set in the agent leaf
    assert by_key["compaction.enabled"]["source"] == "default"  # neither layer → App default


def test_build_schema_agent_override_of_host_field_shows_as_agent_source():
    """A host-scoped field overridden in the agent leaf reports source='agent'
    (the UI badges it 'overridden here', not 'inherited from Host')."""
    from graph.settings_schema import build_schema

    groups = build_schema(
        LangGraphConfig(),
        agent_doc={"model": {"name": "agent-m"}},
        host_doc={"model": {"name": "host-m"}},
    )
    by_key = {e["key"]: e for g in groups for e in g["fields"]}
    assert by_key["model.name"]["scope"] == "host"  # home layer is still host
    assert by_key["model.name"]["source"] == "agent"  # but the live value is the agent override


# ── Slice 3: the layer-aware WRITE half ───────────────────────────────────────


def _point_config_at(tmp_path, monkeypatch):
    """Repoint the live agent leaf (CONFIG_YAML_PATH) + secrets at a temp dir so a
    save touches a scratch file, not the repo's config/. Returns the leaf path."""
    import graph.config_io as cio

    leaf = tmp_path / "langgraph-config.yaml"
    secrets = tmp_path / "secrets.yaml"
    monkeypatch.setattr(cio, "CONFIG_YAML_PATH", leaf, raising=False)
    monkeypatch.setattr(cio, "SECRETS_YAML_PATH", secrets, raising=False)
    return leaf


def _host_file(tmp_path, monkeypatch):
    """Point PROTOAGENT_HOST_CONFIG at a temp host file (initially absent)."""
    hp = tmp_path / "host-config.yaml"
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hp))
    return hp


def _no_reload(monkeypatch):
    """Stub the heavy graph reload — these tests assert the file writes, not the
    graph rebuild."""
    import server.agent_init as ai

    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))


def test_host_layer_save_writes_host_config(tmp_path, monkeypatch):
    """layer='host' writes a host-scoped key to host-config.yaml, and the cascade
    then inherits it into a config loaded from the (silent) agent leaf."""
    import yaml as _yaml

    from server.agent_init import _apply_settings_changes

    leaf = _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    ok, _ = _apply_settings_changes(config={"model": {"name": "box-default-model"}}, layer="host")
    assert ok
    assert hp.exists()
    written = _yaml.safe_load(hp.read_text())
    assert written["model"]["name"] == "box-default-model"
    # The agent leaf was NOT touched by the host write.
    assert not leaf.exists()

    # Cascade: a config loaded from a silent agent leaf inherits the host default.
    leaf.write_text("goal:\n  enabled: true\n")
    cfg = LangGraphConfig.from_yaml(str(leaf))
    assert cfg.model_name == "box-default-model"


def test_host_save_clears_shadowing_agent_key(tmp_path, monkeypatch):
    """A host save deletes a leftover agent-layer copy of the same key so the host
    value actually wins (agent > host in the cascade) — without touching unrelated
    agent-scoped keys. Reproduces the 'Host edit keeps resetting' bug."""
    import yaml as _yaml

    from server.agent_init import _apply_settings_changes

    leaf = _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    # Agent leaf carries a stale api_base (a seed shadow) alongside an agent-scoped key.
    leaf.write_text("model:\n  api_base: http://stale-agent-gw/v1\n  temperature: 0.5\n")

    ok, messages = _apply_settings_changes(
        config={"model": {"api_base": "http://new-host-gw/v1"}}, layer="host"
    )
    assert ok

    # Host file got the new value.
    assert _yaml.safe_load(hp.read_text())["model"]["api_base"] == "http://new-host-gw/v1"

    # The shadowing agent key is gone; the unrelated agent-scoped key survives.
    agent_doc = _yaml.safe_load(leaf.read_text())
    assert "api_base" not in agent_doc["model"]
    assert agent_doc["model"]["temperature"] == 0.5

    # The clearing is surfaced to the operator.
    assert any("model.api_base" in m for m in messages)

    # Effective value now resolves from the host file, not the stale shadow.
    assert LangGraphConfig.from_yaml(str(leaf)).api_base == "http://new-host-gw/v1"


def test_host_save_prunes_emptied_parent_map(tmp_path, monkeypatch):
    """When clearing the only key under a nested map leaves it empty, the now-empty
    parent map is pruned too (no dangling ``model: {}`` in the agent leaf)."""
    import yaml as _yaml

    from server.agent_init import _apply_settings_changes

    leaf = _point_config_at(tmp_path, monkeypatch)
    _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    # model.api_base is the sole agent-leaf key; goal.enabled is unrelated.
    leaf.write_text("model:\n  api_base: http://stale/v1\ngoal:\n  enabled: true\n")

    ok, _ = _apply_settings_changes(config={"model": {"api_base": "http://new/v1"}}, layer="host")
    assert ok

    agent_doc = _yaml.safe_load(leaf.read_text())
    assert "model" not in agent_doc  # emptied parent pruned
    assert agent_doc["goal"]["enabled"] is True  # untouched


def test_host_layer_refuses_secret(tmp_path, monkeypatch):
    """D5: a secret-typed key (model.api_key) is stripped/refused on the host layer —
    the host file is non-secret only."""
    import yaml as _yaml

    from server.agent_init import _apply_settings_changes

    _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    ok, messages = _apply_settings_changes(
        config={"model": {"name": "box-model", "api_key": "sk-SHOULD-NOT-LAND"}},
        layer="host",
    )
    assert ok
    written = _yaml.safe_load(hp.read_text()) or {}
    assert written.get("model", {}).get("name") == "box-model"
    assert "api_key" not in written.get("model", {})  # secret refused
    # The refusal is surfaced to the operator.
    assert any("api_key" in m for m in messages)


def test_host_layer_refuses_agent_scoped_key(tmp_path, monkeypatch):
    """An agent-scoped key (goal.enabled) is filtered out of a host write — the
    host file can't accumulate agent settings (D1/D4)."""
    import yaml as _yaml

    from server.agent_init import _apply_settings_changes

    _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    ok, _ = _apply_settings_changes(
        config={"model": {"name": "box-model"}, "goal": {"enabled": False}},
        layer="host",
    )
    assert ok
    written = _yaml.safe_load(hp.read_text()) or {}
    assert written.get("model", {}).get("name") == "box-model"
    assert "goal" not in written  # agent-scoped key dropped


def test_agent_layer_save_unchanged(tmp_path, monkeypatch):
    """layer='agent' (the default) keeps today's behavior: write the leaf, split a
    secret to secrets.yaml, leave the host file untouched."""
    import yaml as _yaml

    import graph.config_io as cio
    from server.agent_init import _apply_settings_changes

    leaf = _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    ok, _ = _apply_settings_changes(
        config={"model": {"name": "agent-model", "api_key": "sk-secret"}},  # default layer
    )
    assert ok
    written = _yaml.safe_load(leaf.read_text())
    assert written["model"]["name"] == "agent-model"
    assert "api_key" not in written["model"]  # secret split out, as always
    secrets = _yaml.safe_load(cio.SECRETS_YAML_PATH.read_text())
    assert secrets["model"]["api_key"] == "sk-secret"
    assert not hp.exists()  # host file never created by an agent save


def test_reset_pops_leaf_key_falls_back_to_host(tmp_path, monkeypatch):
    """Reset pops the leaf key so the value falls back to the host default."""
    import yaml as _yaml

    from server.agent_init import _reset_settings_keys

    leaf = _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    hp.write_text("model:\n  name: host-default-model\n")
    leaf.write_text("model:\n  name: agent-override-model\ngoal:\n  enabled: true\n")

    ok, _ = _reset_settings_keys(["model.name"])
    assert ok
    written = _yaml.safe_load(leaf.read_text())
    assert "name" not in written.get("model", {})  # leaf override removed (empty model pruned)
    assert "model" not in written  # the now-empty model map was pruned
    assert written["goal"]["enabled"] is True  # sibling section untouched

    cfg = LangGraphConfig.from_yaml(str(leaf))
    assert cfg.model_name == "host-default-model"  # falls back to the host default


def test_reset_of_key_set_in_both_leaves_host_default(tmp_path, monkeypatch):
    """Reset of a key set in BOTH layers removes only the leaf copy, leaving the
    host default in place (and still on disk)."""
    import yaml as _yaml

    from server.agent_init import _reset_settings_keys

    leaf = _point_config_at(tmp_path, monkeypatch)
    hp = _host_file(tmp_path, monkeypatch)
    _no_reload(monkeypatch)

    hp.write_text("model:\n  name: host-model\n")
    leaf.write_text("model:\n  name: agent-model\n")

    ok, _ = _reset_settings_keys(["model.name"])
    assert ok
    # Host file is untouched — its default survives.
    assert _yaml.safe_load(hp.read_text())["model"]["name"] == "host-model"
    cfg = LangGraphConfig.from_yaml(str(leaf))
    assert cfg.model_name == "host-model"


def test_pop_keys_from_yaml_prunes_and_is_idempotent():
    """pop_keys_from_yaml deletes dotted keys, prunes emptied parents, and skips
    absent keys (idempotent)."""
    from graph.config_io import pop_keys_from_yaml

    doc = {"prompt_cache": {"warm": {"enabled": True}}, "model": {"name": "m", "temperature": 0.5}}
    pop_keys_from_yaml(doc, ["prompt_cache.warm.enabled", "model.temperature", "missing.key"])
    assert "prompt_cache" not in doc  # the whole empty chain pruned
    assert doc["model"] == {"name": "m"}  # sibling leaf kept, parent not pruned
    # Idempotent — popping again is a no-op, never raises.
    pop_keys_from_yaml(doc, ["prompt_cache.warm.enabled"])
    assert "prompt_cache" not in doc


# ── D8: box-runtime knobs promoted into the Host layer (ADR 0047 D8) ───────────
# bind interface / fleet port base / discovery range+mDNS / supervisor warm policy
# are now host-scoped FIELDS. Each resolves file > env > app-default (the env var is
# the zero-migration fallback), and the host process call sites read the live config.


def _fleet_env_clear(monkeypatch):
    """Start each env-precedence test from a clean slate — a dev's shell may already
    export these (conftest only defaults PROTOAGENT_HOST_CONFIG)."""
    for name in ("PROTOAGENT_HOST", "PROTOAGENT_FLEET_MAX_WARM", "PROTOAGENT_FLEET_WARM_GRACE"):
        monkeypatch.delenv(name, raising=False)


def test_d8_app_defaults_when_nothing_set(monkeypatch):
    """No file, no leaf, no env → the dataclass (App) defaults."""
    _fleet_env_clear(monkeypatch)
    cfg = LangGraphConfig.from_dict({})
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.fleet_port_base == 7870
    assert (cfg.discovery_port_min, cfg.discovery_port_max) == (7860, 7910)
    assert cfg.discovery_mdns is True
    assert cfg.fleet_max_warm == 0
    assert cfg.fleet_warm_grace_seconds == 0


def test_d8_env_fallback_when_key_absent(monkeypatch):
    """A promoted knob falls back to its PROTOAGENT_* env var when the merged dict
    omits the key — the bridge that makes promotion zero-migration."""
    _fleet_env_clear(monkeypatch)
    monkeypatch.setenv("PROTOAGENT_HOST", "0.0.0.0")
    monkeypatch.setenv("PROTOAGENT_FLEET_MAX_WARM", "3")
    monkeypatch.setenv("PROTOAGENT_FLEET_WARM_GRACE", "15")
    cfg = LangGraphConfig.from_dict({})
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.fleet_max_warm == 3
    assert cfg.fleet_warm_grace_seconds == 15


def test_d8_file_wins_over_env(monkeypatch):
    """File > env: a key present in the (host/leaf) dict beats the env var."""
    _fleet_env_clear(monkeypatch)
    monkeypatch.setenv("PROTOAGENT_HOST", "0.0.0.0")
    monkeypatch.setenv("PROTOAGENT_FLEET_MAX_WARM", "3")
    cfg = LangGraphConfig.from_dict({"network": {"bind": "127.0.0.1"}, "fleet": {"warm": {"max": 9}}})
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.fleet_max_warm == 9


def test_d8_bad_env_value_degrades_to_default(monkeypatch):
    """A non-integer env var for an int knob degrades to the app default, not a crash."""
    _fleet_env_clear(monkeypatch)
    monkeypatch.setenv("PROTOAGENT_FLEET_MAX_WARM", "not-a-number")
    assert LangGraphConfig.from_dict({}).fleet_max_warm == 0


def test_d8_host_file_sets_box_runtime_leaf_overrides(tmp_path, monkeypatch):
    """End-to-end cascade for fleet knobs: host-config.yaml sets the box default,
    a leaf value overrides it (git-style), env is the lowest fallback."""
    _fleet_env_clear(monkeypatch)
    monkeypatch.setenv("PROTOAGENT_FLEET_MAX_WARM", "1")  # lowest precedence
    _host_yaml(tmp_path, "fleet:\n  port_base: 8000\n  warm:\n    max: 5\n", monkeypatch)
    path = _agent_yaml(tmp_path, "fleet:\n  warm:\n    max: 7\n")  # leaf overrides warm, silent on port_base
    cfg = LangGraphConfig.from_yaml(path)
    assert cfg.fleet_max_warm == 7  # leaf wins over host + env
    assert cfg.fleet_port_base == 8000  # inherited from host


def test_d8_host_cannot_inject_via_unscoped_key(tmp_path, monkeypatch):
    """The fleet knobs are host-scoped, so a host file CAN set them (unlike an
    agent-scoped key) — confirms the scope tagging took effect."""
    _fleet_env_clear(monkeypatch)
    _host_yaml(tmp_path, "network:\n  bind: 0.0.0.0\n", monkeypatch)
    cfg = LangGraphConfig.from_yaml(str(tmp_path / "absent.yaml"))
    assert cfg.bind_host == "0.0.0.0"  # host-scoped key applied from the host file


def test_d8_supervisor_warm_policy_reads_live_config(monkeypatch):
    """supervisor.max_warm()/grace prefer the resolved config; fall back to env with
    no live config (the CLI/no-STATE path keeps today's behavior)."""
    import runtime.state as rs
    from graph.fleet import supervisor

    class _Cfg:
        fleet_max_warm = 4
        fleet_warm_grace_seconds = 12

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    assert supervisor.max_warm() == 4
    assert supervisor._warm_grace_seconds() == 12

    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setenv("PROTOAGENT_FLEET_MAX_WARM", "6")
    assert supervisor.max_warm() == 6


def test_d8_discovery_helpers_read_live_config(monkeypatch):
    """discovery resolves its port range + mDNS gate from the Host-layer config."""
    import runtime.state as rs
    from graph.fleet import discovery

    class _Cfg:
        discovery_port_min = 9000
        discovery_port_max = 9100
        discovery_mdns = False

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    assert discovery._config_port_range() == (9000, 9100)
    assert discovery._mdns_enabled() is False


def test_d8_manager_port_base_reads_live_config(monkeypatch):
    """_pick_port bases its scan on the resolved fleet.port_base."""
    import runtime.state as rs
    from graph.workspaces import manager

    class _Cfg:
        fleet_port_base = 8200

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    assert manager._port_base() == 8200
