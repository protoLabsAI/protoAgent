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
         "with_skill": True, "with_workflow": True}
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
    scaffold.invoke({"name": "dup"})
    assert "already exists" in scaffold.invoke({"name": "dup"})
