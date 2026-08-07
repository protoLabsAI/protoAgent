"""plugin-devkit — the featured full-bundle reference + scaffolder (ADR 0027, ADR 0096)."""

from __future__ import annotations

import asyncio
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
    spec = importlib.util.spec_from_file_location("pdk_test", str(REPO / "plugins" / "plugin-devkit" / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def test_devkit_loads_as_a_full_bundle(monkeypatch, tmp_path):
    root = tmp_path / "plugins"
    shutil.copytree(REPO / "plugins" / "plugin-devkit", root / "plugin-devkit")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["plugin-devkit"]))
    meta = next(m for m in res.meta if m["id"] == "plugin-devkit")
    assert meta["loaded"], meta.get("error")
    for name in ("scaffold_plugin", "plugin_list_files", "plugin_read_file", "plugin_write_file", "test_plugin"):
        assert name in meta["tools"]
    assert any(s.name == "plugin-architect" for s in res.subagents)
    assert any(p.name == "skills" and "plugin-devkit" in str(p) for p in res.skill_dirs)
    assert any(p.name == "workflows" and "plugin-devkit" in str(p) for p in res.workflow_dirs)
    assert meta["routers"] >= 1  # the /guide view


def test_scaffold_produces_a_loadable_plugin(monkeypatch, tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = _run(
        scaffold.ainvoke(
            {
                "name": "My Cool Plugin",
                "summary": "demo",
                "with_view": True,
                "with_skill": True,
                "with_workflow": True,
                "enable": False,
            }
        )
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
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "dup", "enable": False}))
    assert "already exists" in _run(scaffold.ainvoke({"name": "dup", "enable": False}))


def test_scaffold_communication_plugin(monkeypatch, tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = _run(scaffold.ainvoke({"name": "My Chat", "summary": "demo", "with_comms": True}))
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
    out_root = tmp_path / "out"
    out_root.mkdir()

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
    # _live_enable now confirms the plugin actually LOADED (not just that the config
    # reload ran) — the loader publishes that on STATE.plugin_meta.
    monkeypatch.setattr(STATE, "plugin_meta", [{"id": "live-one", "enabled": True, "loaded": True}], raising=False)

    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = _run(scaffold.ainvoke({"name": "Live One"}))  # enable defaults True
    assert "enabled + loaded live" in msg
    assert captured["config"]["plugins"]["enabled"] == ["existing", "live-one"]


def test_enable_plugin_tool_noop_without_graph(monkeypatch):
    """enable_plugin / reload_plugins degrade gracefully when there's no live agent."""
    mod = _load_devkit_module(None)
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph", None, raising=False)
    assert "not running" in _run(mod.enable_plugin.ainvoke({"plugin_id": "whatever"}))
    assert "no live agent" in _run(mod.reload_plugins.ainvoke({}))


def test_live_enable_reports_a_plugin_that_failed_to_load(monkeypatch):
    """The make-test-live reliability fix: a plugin whose register() raises is *skipped*
    (best-effort load), so the config reload 'succeeds' — but the agent must be told it
    FAILED to load (with the error AND the traceback, ADR 0096 D4) rather than 'live',
    so it fixes-and-reloads instead of testing a no-op."""
    mod = _load_devkit_module(None)
    import server.agent_init as agent_init
    from runtime.state import STATE

    class _Cfg:
        plugins_enabled: list = []
        plugins_disabled: list = []

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "graph_config", _Cfg(), raising=False)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **k: (True, []))
    monkeypatch.setattr(
        STATE,
        "plugin_meta",
        [
            {
                "id": "broken",
                "enabled": True,
                "loaded": False,
                "error": "boom: bad import",
                "traceback": "Traceback (most recent call last):\n  …\nValueError: boom",
            }
        ],
        raising=False,
    )
    out = _run(mod.enable_plugin.ainvoke({"plugin_id": "broken"}))
    assert "FAILED to load" in out and "boom" in out
    assert "ValueError" in out  # the traceback rides along (D4)
    # reload_plugins surfaces the same failure (so the iterate loop is honest)
    assert "FAILED to load" in _run(mod.reload_plugins.ainvoke({}))


def test_reload_ignores_disabled_plugins(monkeypatch):
    """A DISABLED plugin never loads by design — reload_plugins must not report it as a
    failure (previously ~a dozen 'unknown error' lines buried any real breakage)."""
    mod = _load_devkit_module(None)
    import server.agent_init as agent_init
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **k: (True, []))
    monkeypatch.setattr(
        STATE,
        "plugin_meta",
        [
            {"id": "off-one", "enabled": False, "loaded": False},
            {"id": "off-two", "enabled": False, "loaded": False},
            {"id": "really-broken", "enabled": True, "loaded": False, "error": "kapow"},
        ],
        raising=False,
    )
    out = _run(mod.reload_plugins.ainvoke({}))
    assert "really-broken" in out and "kapow" in out
    assert "off-one" not in out and "off-two" not in out


def test_reload_tools_are_async(tmp_path):
    """ADR 0096 D9: the tools that trigger the heavy graph recompile must be async
    (offloading via asyncio.to_thread) — a sync body only stays off the event loop by
    accident of LangChain's executor plumbing."""
    mod = _load_devkit_module(tmp_path)
    assert mod.enable_plugin.coroutine is not None
    assert mod.reload_plugins.coroutine is not None
    assert mod._build_scaffold_tool(None).coroutine is not None
    assert mod._build_test_tool(None).coroutine is not None


def test_file_tools_roundtrip_and_fence(tmp_path):
    """plugin_list_files / plugin_read_file / plugin_write_file — the edit half of the
    loop (ADR 0096 D2), fenced to the plugins dir."""
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Edit Me", "enable": False}))

    tools = {t.name: t for t in mod._build_file_tools({"target_dir": str(out_root)})}
    listing = tools["plugin_list_files"].invoke({"plugin_id": "edit-me"})
    assert "__init__.py" in listing and "protoagent.plugin.yaml" in listing

    body = tools["plugin_read_file"].invoke({"plugin_id": "edit-me", "path": "__init__.py"})
    assert "def register" in body

    out = tools["plugin_write_file"].invoke({"plugin_id": "edit-me", "path": "notes.md", "content": "hello"})
    assert out.startswith("✓") and (out_root / "edit-me" / "notes.md").read_text() == "hello"

    # the fence: relative-only, no .., no absolute, ids are slugs, unknown ids refused
    assert "✗" in tools["plugin_read_file"].invoke({"plugin_id": "edit-me", "path": "../outside.txt"})
    assert "✗" in tools["plugin_write_file"].invoke({"plugin_id": "edit-me", "path": "/tmp/x", "content": "x"})
    assert "✗" in tools["plugin_read_file"].invoke({"plugin_id": "../edit-me", "path": "__init__.py"})
    assert "✗" in tools["plugin_list_files"].invoke({"plugin_id": "no-such-plugin"})


def test_test_plugin_runs_the_scaffolded_suite(tmp_path):
    """test_plugin (ADR 0096 D3) actually subprocess-runs the with_tests suite of a
    freshly scaffolded plugin — the loop's verify step, green from birth."""
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Green Born", "with_tests": True, "enable": False}))

    tp = mod._build_test_tool({"target_dir": str(out_root)})
    out = _run(tp.ainvoke({"plugin_id": "green-born"}))
    assert out.startswith("✓"), out


def test_test_plugin_reports_no_tests(tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Bare One", "enable": False}))  # no with_tests
    tp = mod._build_test_tool({"target_dir": str(out_root)})
    out = _run(tp.ainvoke({"plugin_id": "bare-one"}))
    assert "no tests collected" in out


def test_loader_records_traceback(monkeypatch, tmp_path):
    """A register() that raises leaves a bounded traceback on the meta entry (ADR 0096
    D4) — str(exc) alone gives a NameError with no location."""
    root = tmp_path / "plugins"
    (root / "boomer").mkdir(parents=True)
    (root / "boomer" / "protoagent.plugin.yaml").write_text("id: boomer\nname: Boomer\nversion: 0.0.1\nenabled: true\n")
    (root / "boomer" / "__init__.py").write_text("def register(registry):\n    raise ValueError('kapow')\n")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["boomer"]))
    meta = next(m for m in res.meta if m["id"] == "boomer")
    assert not meta["loaded"] and "kapow" in meta["error"]
    tb = meta.get("traceback", "")
    assert "ValueError" in tb and "kapow" in tb and len(tb) <= 2000
