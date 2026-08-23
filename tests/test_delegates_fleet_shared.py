"""Fleet-shared delegates (ADR 0105): a `scope: host` entry lives in the box's
host-config.yaml (+ host-secrets.yaml), every instance under the box reads it,
an agent entry shadows it by name, and only the hub writes it."""

from __future__ import annotations

import pytest
import yaml

from infra.paths import harden_private_file
from tests.privacy_asserts import assert_owner_only

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
        harden_private_file(p)  # what the real save_secrets does (0600 / Windows ACL)

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
    assert_owner_only(box["root"] / "host-secrets.yaml")  # 0600 on POSIX; ACL-checked on Windows
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


def test_scope_flip_carries_the_stored_secret_both_ways(box):
    """Flipping *Share with fleet* on an entry whose key the form left blank ("keep
    stored") must move the credential with it — never leave no layer holding it."""
    store.upsert_delegate({"name": "gw", "type": "openai", "api_base": "https://gw/v1", "model": "m", "api_key": "sk-1"})
    assert yaml.safe_load((box["leaf"] / "secrets.yaml").read_text())["delegate_secrets"] == {"gw.api_key": "sk-1"}
    # → host, key omitted
    store.upsert_delegate({"name": "gw", "type": "openai", "api_base": "https://gw/v1", "model": "m", "scope": "host"})
    hs = yaml.safe_load((box["root"] / "host-secrets.yaml").read_text())
    assert hs["delegate_secrets"] == {"gw.api_key": "sk-1"}
    assert not (yaml.safe_load((box["leaf"] / "secrets.yaml").read_text()) or {}).get("delegate_secrets")
    assert store.merged_delegates()[0]["api_key"] == "sk-1"
    # → back to agent, key omitted again
    store.upsert_delegate({"name": "gw", "type": "openai", "api_base": "https://gw/v1", "model": "m", "scope": "agent"})
    assert yaml.safe_load((box["leaf"] / "secrets.yaml").read_text())["delegate_secrets"] == {"gw.api_key": "sk-1"}
    assert not (yaml.safe_load((box["root"] / "host-secrets.yaml").read_text()) or {}).get("delegate_secrets")
    # a supplied key on the flip wins over the migrated one
    store.upsert_delegate({"name": "gw", "type": "openai", "api_base": "https://gw/v1", "model": "m", "api_key": "sk-2", "scope": "host"})
    assert yaml.safe_load((box["root"] / "host-secrets.yaml").read_text())["delegate_secrets"] == {"gw.api_key": "sk-2"}


def test_host_write_preserves_other_keys_and_refuses_an_unparseable_file(box):
    hp = box["root"] / "host-config.yaml"
    hp.write_text("model:\n  name: protolabs/smart\n  provider: openai\n")
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/x", "workdir": "/w", "scope": "host"})
    doc = yaml.safe_load(hp.read_text())
    assert doc["model"] == {"name": "protolabs/smart", "provider": "openai"}
    assert [d["name"] for d in doc["delegates"]] == ["cc"]
    hp.write_text(": not yaml [")
    with pytest.raises(store.DelegateScopeError, match="unreadable"):
        store.upsert_delegate({"name": "dd", "type": "acp", "command": "/x", "workdir": "/w", "scope": "host"})
    assert hp.read_text() == ": not yaml ["  # untouched
    hp.write_text("- a list\n")
    with pytest.raises(store.DelegateScopeError, match="not a mapping"):
        store.upsert_delegate({"name": "dd", "type": "acp", "command": "/x", "workdir": "/w", "scope": "host"})


def test_a_shadow_does_not_borrow_the_shared_secret(box, monkeypatch):
    """A member that shadows a shared coder to opt OUT of its key must not have the
    host key injected into its own entry."""
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/hub", "workdir": "/w", "scope": "host", "env": {"ANTHROPIC_API_KEY": "sk-shared"}})
    monkeypatch.setattr(store, "can_write_host_layer", lambda: False)
    store.upsert_delegate({"name": "cc", "type": "acp", "command": "/mine", "workdir": "/w"})
    merged = store.merged_delegates()
    assert merged[0]["scope"] == "agent"
    assert "ANTHROPIC_API_KEY" not in (merged[0].get("env") or {})


def test_real_member_guard_reads_workspace_yaml(tmp_path, monkeypatch):
    """The host-layer write guard rides the real member detection: a workspace.yaml at
    the instance root = member = read-only; none = hub = may write."""
    import infra.paths as paths

    ws = tmp_path / "ws"
    ws.mkdir()

    class _Paths:
        instance_root = ws

    monkeypatch.setattr(paths, "instance_paths", lambda: _Paths())
    assert store.can_write_host_layer() is True
    (ws / "workspace.yaml").write_text("id: pm\nname: pm\n")
    assert store.can_write_host_layer() is False
