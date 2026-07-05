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
