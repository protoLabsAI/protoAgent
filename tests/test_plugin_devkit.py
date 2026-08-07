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
    # The conventional skills/ dir counts in the meta (it always LOADED; the count
    # said 0, which read as "no skill shipped" in the boot log and runtime status).
    assert meta["skills"] >= 1
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
    monkeypatch.setattr(
        STATE,
        "plugin_meta",
        [{"id": "live-one", "enabled": True, "loaded": True, "tools": ["live_one_hello"]}],
        raising=False,
    )

    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = _run(scaffold.ainvoke({"name": "Live One"}))  # enable defaults True
    assert "enabled + loaded live" in msg and "live_one_hello" in msg
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


def test_scaffold_git_init_makes_a_repo_from_birth(tmp_path):
    """git_init=True (ADR 0096 D6): the scaffold is a git repo with an initial commit."""
    import subprocess

    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    msg = _run(scaffold.ainvoke({"name": "Repo Born", "git_init": True, "enable": False}))
    assert ".git (initial commit)" in msg
    pdir = out_root / "repo-born"
    assert (pdir / ".git").is_dir()
    log = subprocess.run(["git", "log", "--oneline"], cwd=pdir, capture_output=True, text=True, check=True).stdout
    assert "scaffold: initial skeleton" in log


def _acp_raw(name, tmp_path, **extra):
    return {"name": name, "type": "acp", "command": "true", "workdir": str(tmp_path), **extra}


def test_resolve_coder_degrades_honestly(monkeypatch, tmp_path):
    """develop_plugin's delegate resolution (ADR 0096 D5): no roster / wrong name /
    wrong type / ambiguity each name the fix instead of guessing."""
    import plugins.delegates as delegates_mod

    mod = _load_devkit_module(tmp_path)

    monkeypatch.setattr(delegates_mod, "_load_delegates_config", lambda: [])
    d, err = mod._resolve_coder(None)
    assert d is None and "no `acp` coding delegate configured" in err

    monkeypatch.setattr(delegates_mod, "_load_delegates_config", lambda: [_acp_raw("proto", tmp_path)])
    d, err = mod._resolve_coder(None)
    assert err is None and d.name == "proto"

    d, err = mod._resolve_coder({"coder": "nope"})
    assert d is None and "no delegate named 'nope'" in err

    monkeypatch.setattr(
        delegates_mod,
        "_load_delegates_config",
        lambda: [_acp_raw("proto", tmp_path), _acp_raw("opus", tmp_path)],
    )
    d, err = mod._resolve_coder(None)
    assert d is None and "multiple acp delegates" in err
    d, err = mod._resolve_coder({"coder": "opus"})
    assert err is None and d.name == "opus"


class _FakeAcpAdapter:
    """Stands in for AcpAdapter in ADAPTERS — but the DelegateRegistry parses raw
    entries through the adapter too, so delegate parsing stays the real thing."""

    type = "acp"

    def __init__(self):
        from plugins.delegates.adapters import AcpAdapter

        self._real = AcpAdapter()
        self.calls: dict = {}

    def parse(self, raw):
        return self._real.parse(raw)

    async def forget_session(self, d):
        self.calls["forgot"] = True

    async def dispatch(self, d, prompt, timeout=None):
        self.calls["delegate"] = d
        self.calls["prompt"] = prompt
        return "done: implemented the feature"

    async def teardown(self, d):
        self.calls["torn"] = True


def test_develop_plugin_dispatches_scoped_and_rejoins_the_spine(monkeypatch, tmp_path):
    """develop_plugin (ADR 0096 D5): the coder is dispatched with a per-call scoped
    copy (workdir = the plugin dir, manage_git off, fresh session, teardown), then
    the host re-joins the spine at test (+ reload when live)."""
    import plugins.delegates as delegates_mod
    from plugins.delegates import adapters as adapters_mod
    from runtime.state import STATE

    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Coded Up", "enable": False}))
    pdir = out_root / "coded-up"

    monkeypatch.setattr(delegates_mod, "_load_delegates_config", lambda: [_acp_raw("proto", tmp_path)])
    fake = _FakeAcpAdapter()
    monkeypatch.setitem(adapters_mod.ADAPTERS, "acp", fake)
    monkeypatch.setattr(STATE, "graph", None, raising=False)

    dev = mod._build_develop_tool({"target_dir": str(out_root)})
    out = _run(dev.ainvoke({"plugin_id": "coded-up", "instructions": "add a frobnicate tool"}))

    scoped = fake.calls["delegate"]
    assert scoped.workdir == str(pdir)  # per-call override, registry untouched
    assert getattr(scoped, "manage_git", False) is False  # devkit owns the lifecycle
    assert fake.calls["forgot"] and fake.calls["torn"]  # fresh session + reaped subprocess
    assert "add a frobnicate tool" in fake.calls["prompt"]
    assert "edit ONLY files inside it" in fake.calls["prompt"]
    assert "done: implemented the feature" in out
    assert "— test_plugin —" in out  # re-joined the spine at *test*
    assert "reload skipped (no live agent)" in out


def test_develop_plugin_without_delegate_names_the_fix(monkeypatch, tmp_path):
    import plugins.delegates as delegates_mod

    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Lonely", "enable": False}))
    monkeypatch.setattr(delegates_mod, "_load_delegates_config", lambda: [])
    dev = mod._build_develop_tool({"target_dir": str(out_root)})
    out = _run(dev.ainvoke({"plugin_id": "lonely", "instructions": "do a thing"}))
    assert out.startswith("✗") and "no `acp` coding delegate configured" in out


def test_register_plugin_project_appends_scoped_entry(monkeypatch, tmp_path):
    """register_plugin_project (ADR 0096 D6): appends a {name, path, write} entry to
    the ADR 0095 registry (preserving existing entries), carries github when given,
    and reads default_branch off a git_init scaffold."""
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Grad Uate", "git_init": True, "enable": False}))
    pdir = out_root / "grad-uate"

    import server.agent_init as agent_init
    from runtime.state import STATE

    class _Cfg:
        projects = [{"name": "existing", "path": "/elsewhere"}]

    captured: dict = {}

    def _fake_apply(config=None, soul=None, layer="agent"):
        captured["config"] = config
        return (True, [])

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "graph_config", _Cfg(), raising=False)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", _fake_apply)

    reg = mod._build_register_project_tool({"target_dir": str(out_root)})
    out = _run(reg.ainvoke({"plugin_id": "grad-uate", "github": "protoLabsAI/grad-uate"}))
    assert out.startswith("✓ registered")
    projects = captured["config"]["projects"]
    assert projects[0] == {"name": "existing", "path": "/elsewhere"}  # preserved
    entry = projects[1]
    assert entry["name"] == "grad-uate" and entry["path"] == str(pdir) and entry["write"] is True
    assert entry["github"] == "protoLabsAI/grad-uate"
    assert entry.get("default_branch")  # a git_init scaffold has a branch to read

    # idempotent: registering again is a no-op, not a duplicate
    registered = projects

    class _Cfg2:
        projects = registered

    monkeypatch.setattr(STATE, "graph_config", _Cfg2(), raising=False)
    assert "already registered" in _run(reg.ainvoke({"plugin_id": "grad-uate"}))


def test_register_plugin_project_refuses_bad_input(monkeypatch, tmp_path):
    mod = _load_devkit_module(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    scaffold = mod._build_scaffold_tool({"target_dir": str(out_root)})
    _run(scaffold.ainvoke({"name": "Fenced", "enable": False}))

    from runtime.state import STATE

    reg = mod._build_register_project_tool({"target_dir": str(out_root)})
    # unknown id → fence refusal (only real plugin dirs are addressable)
    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    assert "✗" in _run(reg.ainvoke({"plugin_id": "not-a-plugin"}))
    # malformed github
    assert "owner/repo" in _run(reg.ainvoke({"plugin_id": "fenced", "github": "not a repo!"}))
    # no live agent
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    assert "not running" in _run(reg.ainvoke({"plugin_id": "fenced"}))


def test_projects_config_write_validates_end_to_end(monkeypatch, tmp_path):
    """The one integration risk: `_apply_settings_changes(config={"projects": [...]})`
    must pass config validation and land the list in the YAML leaf — the registry's
    first runtime write path (ADR 0095 had no POST)."""
    import graph.config_io as cio
    import server.agent_init as ai

    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("model:\n  name: m\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))
    monkeypatch.setattr(ai, "_sync_autostart_with_config", lambda *_a, **_k: None)

    entry = {"name": "grad-uate", "path": str(tmp_path), "write": True, "github": "o/r"}
    ok, msgs = ai._apply_settings_changes(config={"projects": [entry]})
    assert ok, msgs

    import yaml as _yaml

    doc = _yaml.safe_load(leaf.read_text())
    assert doc["projects"] == [entry]


def test_live_enable_flags_a_silent_noop_plugin(monkeypatch):
    """Live QA 2026-08-07: a model-authored register() that RETURNED tools instead of
    calling registry.register_tool loads cleanly with ZERO contributions — and every
    message called it live, so the agent couldn't self-correct. 'Loaded' alone is not
    success: the message must say it registered nothing and name the contract."""
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
        [{"id": "noop-one", "enabled": True, "loaded": True, "tools": [], "skills": 0}],
        raising=False,
    )
    out = _run(mod.enable_plugin.ainvoke({"plugin_id": "noop-one"}))
    assert "registered NOTHING" in out and "registry.register_tool" in out


def test_reload_flags_silent_noop_only_under_devkit_roots(monkeypatch, tmp_path):
    """The zero-contribution sweep is scoped to plugins in the devkit's dirs — a
    bundled plugin contributing only meta-invisible surface (middleware, late-tool
    factories, e.g. execute_code) must not trip it."""
    from graph.plugins import scaffold as scaffold_mod

    mod = _load_devkit_module(tmp_path)
    root = tmp_path / "live-plugins"
    (root / "quiet-one").mkdir(parents=True)
    (root / "quiet-one" / "protoagent.plugin.yaml").write_text("id: quiet-one\nname: Q\nversion: 0.0.1\n")
    monkeypatch.setattr(scaffold_mod, "live_plugins_dir", lambda: root)

    import server.agent_init as agent_init
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **k: (True, []))
    monkeypatch.setattr(
        STATE,
        "plugin_meta",
        [
            {"id": "quiet-one", "enabled": True, "loaded": True, "tools": [], "skills": 0},
            # loaded, zero visible contributions, but NOT under the devkit roots (a
            # bundled late-tools plugin) — must stay unflagged.
            {"id": "execute_code", "enabled": True, "loaded": True, "tools": [], "skills": 0},
        ],
        raising=False,
    )
    out = _run(mod.reload_plugins.ainvoke({}))
    assert "quiet-one" in out and "registered NOTHING" in out
    assert "execute_code" not in out
