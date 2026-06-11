"""plugin-devkit — the featured full-bundle reference + scaffolder (ADR 0027)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins

REPO = Path(__file__).resolve().parent.parent


def _cfg(**kw):
    return LangGraphConfig(**kw)


def _load_devkit_module(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "pdk_test", str(REPO / "plugins" / "plugin-devkit" / "__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_devkit_loads_as_a_full_bundle(monkeypatch, tmp_path):
    root = tmp_path / "plugins"
    shutil.copytree(REPO / "plugins" / "plugin-devkit", root / "plugin-devkit")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["plugin-devkit"]))
    meta = next(m for m in res.meta if m["id"] == "plugin-devkit")
    assert meta["loaded"], meta.get("error")
    assert "scaffold_plugin" in meta["tools"]
    assert any(s.name == "plugin-architect" for s in res.subagents)
    assert any(p.name == "skills" and "plugin-devkit" in str(p) for p in res.skill_dirs)
    assert any(p.name == "workflows" and "plugin-devkit" in str(p) for p in res.workflow_dirs)
    assert meta["routers"] >= 1  # the /guide view


def test_scaffold_produces_a_loadable_plugin(monkeypatch, tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = scaffold.invoke(
        {"name": "My Cool Plugin", "summary": "demo", "with_view": True,
         "with_skill": True, "with_workflow": True, "enable": False}
    )
    assert "scaffolded" in msg
    pdir = out_root / "my-cool-plugin"
    assert (pdir / "protoagent.plugin.yaml").exists()
    assert (pdir / "__init__.py").exists()
    assert (pdir / "skills").is_dir() and (pdir / "workflows").is_dir()

    # the scaffolded skeleton must itself LOAD (enable it + run the loader)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [out_root])
    res = load_plugins(_cfg(plugins_enabled=["my-cool-plugin"]))
    meta = next(m for m in res.meta if m["id"] == "my-cool-plugin")
    assert meta["loaded"], meta.get("error")
    assert "my_cool_plugin_hello" in meta["tools"]


def test_scaffold_refuses_overwrite(tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"; out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    scaffold.invoke({"name": "dup", "enable": False})
    assert "already exists" in scaffold.invoke({"name": "dup", "enable": False})


def test_scaffold_communication_plugin(monkeypatch, tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"; out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = scaffold.invoke({"name": "My Chat", "summary": "demo", "with_comms": True})
    assert "communication plugin" in msg
    pdir = out_root / "my-chat"
    manifest = (pdir / "protoagent.plugin.yaml").read_text()
    init = (pdir / "__init__.py").read_text()
    assert "config_section: my-chat" in manifest and "bot_token" in manifest
    assert "register_chat_surface" in init and "class MyChatAdapter" in init

    # the scaffolded comms skeleton must itself LOAD (registers a surface)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [out_root])
    res = load_plugins(_cfg(plugins_enabled=["my-chat"]))
    meta = next(m for m in res.meta if m["id"] == "my-chat")
    assert meta["loaded"], meta.get("error")


def test_scaffold_enable_hot_reloads_when_live(monkeypatch, tmp_path):
    """enable=True (the default) drives the live hot-reload path so a freshly
    scaffolded plugin loads without a restart — it adds the new id to
    plugins.enabled (preserving the rest) and reloads via _apply_settings_changes."""
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"; out_root.mkdir()

    import server.agent_init as agent_init
    from runtime.state import STATE

    class _Cfg:
        plugins_enabled = ["existing"]
        plugins_disabled = []

    captured: dict = {}

    def _fake_apply(config=None, soul=None, layer="agent"):
        captured["config"] = config
        return (True, [])

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "graph_config", _Cfg(), raising=False)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", _fake_apply)

    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = scaffold.invoke({"name": "Live One"})  # enable defaults True
    assert "enabled + loaded live" in msg
    assert captured["config"]["plugins"]["enabled"] == ["existing", "live-one"]


def test_enable_plugin_tool_noop_without_graph(monkeypatch):
    """enable_plugin / reload_plugins degrade gracefully when there's no live agent."""
    mod = _load_devkit_module(None)
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph", None, raising=False)
    assert "not running" in mod.enable_plugin.invoke({"plugin_id": "whatever"})
    assert "no live agent" in mod.reload_plugins.invoke({})
