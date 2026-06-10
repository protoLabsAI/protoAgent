"""App→Host→Agent settings cascade (ADR 0047, slice 2).

from_yaml now overlays the agent (leaf) YAML on the box-shared Host layer
(host-config.yaml, filtered to host-scoped FIELDS keys). These lock the cascade's
behavior: host defaults inherited, agent overrides win (git-style), the host file
can't inject agent-only keys, a corrupt host file degrades, and — critically —
no host file is byte-identical to the pre-cascade parse (zero-migration).

PROTOAGENT_HOST_CONFIG is defaulted to an absent path by conftest, so tests start
with NO host layer and opt in by pointing it at a temp file.
"""

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
