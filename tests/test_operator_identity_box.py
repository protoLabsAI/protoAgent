"""The operator's name and organization are the box's, not one agent's (#5812).

- ``identity.operator`` / ``identity.org`` are host-scoped, so members inherit them.
- ``promote_host_identity`` MOVES the host agent's own copy up to the Host layer once.
- Every agent's prompt carries one line naming who it works for.
- The settings schema says what the machine uses and, on the host, who sets their own.
"""

from __future__ import annotations

import yaml

from graph.config import LangGraphConfig
from graph.config_io import promote_host_identity
from graph.prompts import build_system_prompt, build_system_prompt_parts, operator_line
from graph.settings_schema import FIELDS, build_schema


def _setup(tmp_path, monkeypatch, *, leaf: dict, host: dict | None):
    hp = tmp_path / "host-config.yaml"
    lp = tmp_path / "config" / "langgraph-config.yaml"
    lp.parent.mkdir()
    lp.write_text(yaml.safe_dump(leaf), encoding="utf-8")
    if host is not None:
        hp.write_text(yaml.safe_dump(host), encoding="utf-8")
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hp))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: lp)
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: False)
    return hp, lp


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_both_identity_keys_are_box_wide():
    scopes = {f.key: f.scope for f in FIELDS if f.key in ("identity.operator", "identity.org")}
    assert scopes == {"identity.operator": "host", "identity.org": "host"}


def test_host_agents_name_moves_up_to_the_box(tmp_path, monkeypatch):
    hp, lp = _setup(
        tmp_path, monkeypatch, leaf={"identity": {"name": "protoagent", "operator": "Josh"}}, host={"model": {"name": "m"}}
    )
    assert promote_host_identity() is True
    assert _read(hp) == {"model": {"name": "m"}, "identity": {"operator": "Josh"}}
    assert _read(lp) == {"identity": {"name": "protoagent"}}
    assert promote_host_identity() is False  # moved once; nothing left to do


def test_creates_the_host_file_when_absent(tmp_path, monkeypatch):
    hp, _ = _setup(tmp_path, monkeypatch, leaf={"identity": {"operator": "Josh", "org": "protoLabs"}}, host=None)
    assert promote_host_identity() is True
    assert _read(hp) == {"identity": {"operator": "Josh", "org": "protoLabs"}}


def test_equal_or_empty_leaf_copy_is_dropped_and_a_different_one_kept(tmp_path, monkeypatch):
    hp, lp = _setup(
        tmp_path,
        monkeypatch,
        leaf={"identity": {"name": "a", "operator": "", "org": "Other Co"}},
        host={"identity": {"operator": "Josh", "org": "protoLabs"}},
    )
    assert promote_host_identity() is True
    # The empty Operator no longer hides the box's; the deliberate Org stays the host's own.
    assert _read(lp) == {"identity": {"name": "a", "org": "Other Co"}}
    assert _read(hp) == {"identity": {"operator": "Josh", "org": "protoLabs"}}


def test_members_and_named_sandboxes_never_write_box_state(tmp_path, monkeypatch):
    hp, lp = _setup(tmp_path, monkeypatch, leaf={"identity": {"operator": "Josh"}}, host={})
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    assert promote_host_identity() is False
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: False)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")
    monkeypatch.delenv("PROTOAGENT_HOME", raising=False)
    assert promote_host_identity() is False
    assert _read(hp) == {} and _read(lp) == {"identity": {"operator": "Josh"}}


def test_a_corrupt_host_file_is_left_alone(tmp_path, monkeypatch):
    hp, lp = _setup(tmp_path, monkeypatch, leaf={"identity": {"operator": "Josh"}}, host=None)
    hp.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert promote_host_identity() is False
    assert _read(lp) == {"identity": {"operator": "Josh"}}


def test_operator_line():
    c = LangGraphConfig()
    assert operator_line(c) == ""
    c.identity_operator = "Josh"
    assert operator_line(c) == "You work for Josh."
    c.identity_org = "protoLabs"
    assert operator_line(c) == "You work for Josh at protoLabs."
    c.identity_operator = "  "
    assert operator_line(c) == ""  # an agent that blanks Operator gets no line


def test_prompt_carries_the_operator_line_after_the_persona():
    parts = build_system_prompt_parts(operator_line="You work for Josh.")
    labels = [label for label, _ in parts]
    assert labels[:2] == ["SOUL", "Operator"]
    assert parts[1][1] == "You work for Josh."
    assert "You work for Josh." in build_system_prompt(operator_line="You work for Josh.")
    assert "Operator" not in [label for label, _ in build_system_prompt_parts()]


def _field(groups, key):
    return next(f for g in groups for f in g["fields"] if f["key"] == key)


def test_schema_reports_the_machine_value_placeholder_and_who_sets_their_own():
    c = LangGraphConfig()
    c.identity_operator = "Frank's operator"
    groups = build_schema(
        c,
        agent_doc={"identity": {"operator": "Frank's operator"}},
        host_doc={"identity": {"operator": "Josh"}},
        set_by_agents={"identity.operator": ["Frank"]},
    )
    op = _field(groups, "identity.operator")
    assert op["source"] == "agent" and op["scope"] == "host"
    assert op["host_value"] == "Josh"
    assert op["placeholder"] == "Your name"
    assert op["set_by_agents"] == ["Frank"]
    # A key the Host layer doesn't set carries no host_value.
    assert "host_value" not in _field(groups, "quiet_hours.timezone")
