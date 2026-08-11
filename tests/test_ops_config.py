"""ops.config (ADR 0075 D2) — read + set config over the op the settings route + CLI share."""

from __future__ import annotations

import types

from ops import OpContext, registry
from ops.config import get_config, set_config


async def test_set_live_uses_injected_applier():
    seen: dict = {}

    def _apply(updates):
        seen["updates"] = updates
        return True, ["reloaded"]

    res = await set_config({"model": {"name": "x"}}, apply_settings=_apply)
    assert res.ok is True and res.reloaded is True and res.messages == ["reloaded"]
    assert seen["updates"] == {"model": {"name": "x"}}


async def test_set_live_failure_surfaces_messages():
    res = await set_config({"a": 1}, apply_settings=lambda u: (False, ["compile failed"]))
    assert res.ok is False and res.reloaded is False and "compile failed" in res.messages


async def test_set_empty_is_noop():
    res = await set_config({}, apply_settings=lambda u: (True, ["x"]))
    assert res.ok is True and res.reloaded is False and res.messages == ["no changes"]


async def test_set_disk_only_writes_yaml(monkeypatch):
    import graph.config_io as cio

    captured: dict = {}
    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: {"a": 1})
    monkeypatch.setattr(cio, "apply_updates_to_yaml", lambda doc, updates: {**doc, **updates})
    monkeypatch.setattr(cio, "save_yaml_doc", lambda doc, p=None: captured.update(doc=doc))

    res = await set_config({"b": 2}, apply_settings=None)  # no applier → disk-only
    assert res.ok is True and res.reloaded is False
    assert captured["doc"] == {"a": 1, "b": 2}


# ── #2575: the disk-only path must route secrets exactly like a live server ──


def _isolate_config_files(monkeypatch, tmp_path):
    """Point BOTH config files at temp paths — `strip_secrets_from_doc` relocates
    inline secrets, so an unisolated run would write the developer's real overlay."""
    from graph import config_io

    cfg, secrets = tmp_path / "langgraph-config.yaml", tmp_path / "secrets.yaml"
    cfg.write_text("model:\n  name: m\n", encoding="utf-8")
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: cfg)
    monkeypatch.setattr(config_io, "secrets_yaml_path", lambda: secrets)
    return cfg, secrets


async def test_set_disk_only_routes_secrets_to_the_overlay(monkeypatch, tmp_path):
    """The bug: `protoagent config set auth.token=…` on a stopped instance wrote the
    credential in plaintext into the tracked-shape YAML and never made secrets.yaml."""
    from tests.privacy_asserts import assert_owner_only

    cfg, secrets = _isolate_config_files(monkeypatch, tmp_path)

    res = await set_config({"auth": {"token": "sk-super-secret"}}, apply_settings=None)

    assert res.ok is True and res.reloaded is False
    assert "sk-super-secret" not in cfg.read_text(encoding="utf-8")  # never in the tracked file
    assert "sk-super-secret" in secrets.read_text(encoding="utf-8")
    assert_owner_only(secrets)
    assert any("secrets.yaml" in m for m in res.messages)  # and it says where it went


async def test_set_disk_only_still_writes_non_secret_keys(monkeypatch, tmp_path):
    import yaml

    cfg, secrets = _isolate_config_files(monkeypatch, tmp_path)

    res = await set_config({"model": {"temperature": 0.3}}, apply_settings=None)

    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert doc["model"]["temperature"] == 0.3
    assert doc["model"]["name"] == "m"  # untouched sibling preserved
    assert not secrets.exists()  # no overlay conjured for a plain setting
    assert any("config.yaml" in m for m in res.messages)


async def test_set_disk_only_splits_a_mixed_update(monkeypatch, tmp_path):
    import yaml

    cfg, secrets = _isolate_config_files(monkeypatch, tmp_path)

    res = await set_config(
        {"model": {"api_key": "sk-live", "api_base": "http://gw:4000/v1"}},
        apply_settings=None,
    )

    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert doc["model"]["api_base"] == "http://gw:4000/v1"
    assert "api_key" not in doc["model"]
    assert yaml.safe_load(secrets.read_text(encoding="utf-8"))["model"]["api_key"] == "sk-live"
    assert len(res.messages) == 2  # one line per destination


async def test_set_disk_only_strips_a_secret_an_older_yaml_carries(monkeypatch, tmp_path):
    """The documented belt: 'every config save also strips any secret keys the main
    YAML might still carry', so a checkout converges to secret-free. It never fired
    on this path — a plain `config set` left a hand-seeded key inline forever."""
    import yaml

    cfg, secrets = _isolate_config_files(monkeypatch, tmp_path)
    cfg.write_text("model:\n  name: m\n  api_key: sk-hand-seeded\n", encoding="utf-8")

    await set_config({"model": {"temperature": 0.3}}, apply_settings=None)

    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "api_key" not in doc["model"]  # relocated, not left inline
    assert yaml.safe_load(secrets.read_text(encoding="utf-8"))["model"]["api_key"] == "sk-hand-seeded"


async def test_set_disk_only_blank_secret_leaves_the_stored_one(monkeypatch, tmp_path):
    """Blank means 'leave the stored secret alone' (the settings UI sends blank for an
    unchanged key). It must not clobber, and must not claim it wrote something."""
    import yaml

    from graph import config_io

    cfg, secrets = _isolate_config_files(monkeypatch, tmp_path)
    config_io.save_secrets({"auth": {"token": "keep-me"}})

    res = await set_config({"auth": {"token": ""}}, apply_settings=None)

    assert yaml.safe_load(secrets.read_text(encoding="utf-8"))["auth"]["token"] == "keep-me"
    assert "no changes" in res.messages[0]
    assert "auth" not in (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {})


async def test_get_live_config(monkeypatch):
    import graph.config_io as cio

    monkeypatch.setattr(cio, "config_to_dict", lambda cfg: {"live": True})
    ctx = OpContext(knowledge_store=None, graph_config=types.SimpleNamespace())
    assert await get_config(ctx=ctx) == {"live": True}


async def test_get_disk_config_when_no_agent(monkeypatch):
    import graph.config_io as cio

    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: {"disk": 1})
    assert await get_config(ctx=None) == {"disk": 1}


async def test_get_config_normalizes_ruamel_to_plain(monkeypatch):
    """Regression: load_yaml_doc returns a ruamel CommentedMap (a dict subclass), which
    PyYAML's safe_dump can't represent — `protoagent config get` crashed on it. get_config
    must hand back PLAIN types."""
    import io

    import pytest

    yaml_rt = pytest.importorskip("ruamel.yaml")
    import graph.config_io as cio

    cm = yaml_rt.YAML(typ="rt").load(io.StringIO("server:\n  port: 7870\nlist:\n  - a\n  - b\n"))
    assert type(cm) is not dict  # it IS a CommentedMap — the thing that broke
    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: cm)

    result = await get_config(ctx=None)
    assert type(result) is dict and result == {"server": {"port": 7870}, "list": ["a", "b"]}
    import yaml

    yaml.safe_dump(result)  # must NOT raise RepresenterError


def test_config_ops_registered_with_metadata():
    reg = registry()
    assert reg["config.set"].mutates is True  # full-profile only
    assert reg["config.get"].mutates is False  # read-only admissible
