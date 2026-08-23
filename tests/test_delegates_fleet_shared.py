"""Fleet-shared delegates (ADR 0105): a `scope: host` entry lives in the box's
host-config.yaml (+ host-secrets.yaml), every instance under the box reads it,
an agent entry shadows it by name, and only the hub writes it."""

from __future__ import annotations

import os

import pytest
import yaml

from plugins.delegates import store


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Real files: a box root with host-config.yaml / host-secrets.yaml and an
    instance leaf langgraph-config.yaml / secrets.yaml."""
    import graph.config_io as cio
    import infra.paths as paths

    root = tmp_path / "box"
    leaf = tmp_path / "inst" / "config"
    leaf.mkdir(parents=True)
    root.mkdir()
    (leaf / "langgraph-config.yaml").write_text("model:\n  name: m\n")
    monkeypatch.setattr(paths, "host_config_path", lambda: root / "host-config.yaml")
    monkeypatch.setattr(paths, "host_secrets_path", lambda: root / "host-secrets.yaml")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: leaf / "secrets.yaml")
    # load/save the leaf doc against the real file
    monkeypatch.setattr(cio, "load_yaml_doc", lambda path=None: yaml.safe_load((path or leaf / "langgraph-config.yaml").read_text()) or {})
    monkeypatch.setattr(cio, "save_yaml_doc", lambda doc, path=None: (path or leaf / "langgraph-config.yaml").write_text(yaml.safe_dump(doc, sort_keys=False)))

    def _load_secrets(path=None):
        p = path or leaf / "secrets.yaml"
        return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}

    def _save_secrets(upd, path=None):
        p = path or leaf / "secrets.yaml"
        cur = _load_secrets(p)
        for sec, vals in (upd or {}).items():
            cur.setdefault(sec, {}).update(vals)
        p.write_text(yaml.safe_dump(cur))
        os.chmod(p, 0o600)

    monkeypatch.setattr(cio, "load_secrets", _load_secrets)
    monkeypatch.setattr(cio, "save_secrets", _save_secrets)
    monkeypatch.setattr(store, "can_write_host_layer", lambda: True)  # hub by default
    return {"root": root, "leaf": leaf}


def test_host_scoped_entry_lands_in_the_box_and_reads_back_on_every_instance(box):
    store.upsert_delegate(
        {"name": "cc", "type": "acp", "command": "/abs/claude-agent-acp", "workdir": "/w", "scope": "host",
         "env": {"ANTHROPIC_API_KEY": "sk-shared"}}
    )
    host = yaml.safe_load((box["root"] / "host-config.yaml").read_text())
    assert [d["name"] for d in host["delegates"]] == ["cc"]
    assert "scope" not in host["delegates"][0]  # the layer IS the scope; not persisted
    assert host["delegates"][0]["env"] == {"ANTHROPIC_API_KEY": ""}  # value routed out
    hs = yaml.safe_load((box["root"] / "host-secrets.yaml").read_text())
    assert hs["delegate_secrets"] == {"cc.env.ANTHROPIC_API_KEY": "sk-shared"}
    assert oct(os.stat(box["root"] / "host-secrets.yaml").st_mode & 0o777) == "0o600"
    # The leaf is untouched; the effective roster carries it as scope=host with secrets overlaid.
    assert "delegates" not in yaml.safe_load((box["leaf"] / "langgraph-config.yaml").read_text())
    eff = store.read_delegates_raw()
    assert [(d["name"], d["scope"]) for d in eff] == [("cc", "host")]
    merged = store.merged_delegates()
    assert merged[0]["env"]["ANTHROPIC_API_KEY"] == "sk-shared"


def test_agent_entry_shadows_a_shared_one_and_member_cannot_write_the_box(box, monkeypatch):
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/hub/cc", "workdir": "/w", "scope": "host"})
    monkeypatch.setattr(store, "can_write_host_layer", lambda: False)  # now act as a member
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/mine/cc", "workdir": "/w"})
    eff = {d["name"]: d for d in store.read_delegates_raw()}
    assert eff["cc"]["scope"] == "agent" and eff["cc"]["command"] == "/mine/cc"
    # the host copy survived the shadow (a member never writes the box)
    host = yaml.safe_load((box["root"] / "host-config.yaml").read_text())
    assert host["delegates"][0]["command"] == "/hub/cc"
    with pytest.raises(store.DelegateScopeError):
        store.upsert_delegate({"name": "other", "type": "acp", "command": "/x", "workdir": "/w", "scope": "host"})
    # deleting removes the member's own entry; the shared one reappears; deleting THAT is refused
    store.delete_delegate("cc")
    assert store.read_delegates_raw()[0]["scope"] == "host"
    with pytest.raises(store.DelegateScopeError):
        store.delete_delegate("cc")


def test_hub_moves_an_entry_between_layers_on_scope_change(box):
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/x", "workdir": "/w"})
    assert store.read_delegates_raw()[0]["scope"] == "agent"
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/x", "workdir": "/w", "scope": "host"})
    assert [d["scope"] for d in store.read_delegates_raw()] == ["host"]
    assert "delegates" not in yaml.safe_load((box["leaf"] / "langgraph-config.yaml").read_text()) or \
        yaml.safe_load((box["leaf"] / "langgraph-config.yaml").read_text())["delegates"] == []
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/x", "workdir": "/w", "scope": "agent"})
    assert [d["scope"] for d in store.read_delegates_raw()] == ["agent"]
    assert yaml.safe_load((box["root"] / "host-config.yaml").read_text())["delegates"] == []
    store.delete_delegate("cc")
    assert store.read_delegates_raw() == []


def test_unreadable_or_absent_box_layer_contributes_nothing(box):
    (box["root"] / "host-config.yaml").write_text(": not yaml [")
    assert store.read_host_delegates_raw() == []
    (box["root"] / "host-config.yaml").write_text("- a list\n")
    assert store.read_host_delegates_raw() == []
