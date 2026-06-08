"""Tests for the drop-in plugin system (graph/plugins/).

Plugins are created in tmp dirs and `_plugin_roots` is monkeypatched so tests
don't pick up the shipped `hello` example.
"""

from __future__ import annotations

from pathlib import Path

from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import discover_plugins, load_plugins
from graph.plugins.manifest import load_manifest

_TOOL_PLUGIN = '''
from langchain_core.tools import tool

@tool
async def {tool}(x: str = "") -> str:
    """example"""
    return x

def register(registry):
    registry.register_tool({tool})
    registry.register_skill_dir("skills")
'''


def _make_plugin(root: Path, pid: str, *, enabled=False, tool="do_thing",
                 requires_env=None, body=None, manifest_extra="") -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    env_line = f"requires_env: {requires_env}\n" if requires_env else ""
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\n"
        f"enabled: {'true' if enabled else 'false'}\n{env_line}{manifest_extra}",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(body or _TOOL_PLUGIN.format(tool=tool), encoding="utf-8")
    (d / "skills").mkdir(exist_ok=True)
    return d


def _cfg(**kw):
    return LangGraphConfig(**kw)


def test_manifest_parse(tmp_path) -> None:
    _make_plugin(tmp_path, "p1", enabled=True)
    m = load_manifest(tmp_path / "p1")
    assert m and m.id == "p1" and m.enabled is True

    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "protoagent.plugin.yaml").write_text("name: no-id\n")
    assert load_manifest(tmp_path / "bad") is None  # missing id


def test_discover_live_overrides_bundle(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    live = tmp_path / "live"
    _make_plugin(bundle, "dup", manifest_extra="description: from-bundle\n")
    _make_plugin(live, "dup", manifest_extra="description: from-live\n")
    found = {m.id: m.description for m in discover_plugins([bundle, live])}
    assert found["dup"] == "from-live"


def test_disabled_plugin_not_loaded(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "offplug", enabled=False, tool="off_tool")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg())
    assert res.tools == []
    assert res.meta[0]["id"] == "offplug" and res.meta[0]["enabled"] is False


def test_enabled_via_config(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "p", enabled=False, tool="p_tool")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["p"]))
    assert [t.name for t in res.tools] == ["p_tool"]
    assert res.meta[0]["loaded"] is True
    assert res.skill_dirs and res.skill_dirs[0].name == "skills"


def test_enabled_via_manifest(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "m", enabled=True, tool="m_tool")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg())
    assert [t.name for t in res.tools] == ["m_tool"]


def test_multi_module_plugin_with_relative_import(tmp_path, monkeypatch) -> None:
    """A plugin whose id has a hyphen AND whose __init__.py uses a relative import
    (``from .tools import …``) must load. Regression: the loader used the raw id as
    the module name (a hyphen is illegal) and didn't register it in sys.modules, so
    the relative import failed with "No module named protoagent_plugin_<id>".
    """
    root = tmp_path / "plugins"
    d = root / "multi-mod"
    d.mkdir(parents=True)
    (d / "protoagent.plugin.yaml").write_text(
        "id: multi-mod\nname: Multi mod\nversion: 0.1.0\nenabled: true\n", encoding="utf-8")
    (d / "tools.py").write_text(
        "from langchain_core.tools import tool\n"
        "@tool\n"
        "def mm_tool() -> str:\n"
        "    '''sibling-module tool'''\n"
        "    return 'ok'\n"
        "def get_tools():\n"
        "    return [mm_tool]\n", encoding="utf-8")
    (d / "__init__.py").write_text(
        "from .tools import get_tools\n"
        "def register(registry):\n"
        "    registry.register_tools(get_tools())\n", encoding="utf-8")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg())
    assert [t.name for t in res.tools] == ["mm_tool"]
    assert res.meta[0]["loaded"] is True


def test_tool_collision_skipped(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "c", enabled=True, tool="current_time")  # core tool name
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(), core_tool_names={"current_time"})
    assert res.tools == []  # shadowing skipped
    assert res.meta[0]["loaded"] is True and res.meta[0]["tools"] == []


def test_bad_plugin_is_non_fatal(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "broken", enabled=True, body="def register(registry):\n    raise RuntimeError('boom')\n")
    _make_plugin(root, "ok", enabled=True, tool="ok_tool")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg())
    assert [t.name for t in res.tools] == ["ok_tool"]  # good one still loads
    broken = next(m for m in res.meta if m["id"] == "broken")
    assert broken["loaded"] is False and "boom" in broken["error"]


def test_requires_env_gating(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "needsenv", enabled=True, tool="env_tool", requires_env=["PLUGIN_TEST_KEY_XYZ"])
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    monkeypatch.delenv("PLUGIN_TEST_KEY_XYZ", raising=False)
    res = load_plugins(_cfg())
    assert res.tools == []
    assert "missing env" in res.meta[0]["error"]


def test_config_round_trip() -> None:
    from graph.config_io import config_to_dict

    cfg = LangGraphConfig(plugins_enabled=["a", "b"], plugins_dir="/x")
    d = config_to_dict(cfg)
    assert d["plugins"] == {"enabled": ["a", "b"], "dir": "/x"}


def test_from_yaml_parses_plugins(tmp_path) -> None:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("plugins:\n  enabled: [hello]\n  dir: /tmp/p\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.plugins_enabled == ["hello"] and cfg.plugins_dir == "/tmp/p"


# --- ADR 0018: routers / surfaces / subagents -------------------------------

_EXT_PLUGIN = '''
class _FakeRouter:
    routes = []

class _Sub:
    name = "plug_sub"

def _start():
    return None

def _stop():
    return None

def register(registry):
    registry.register_router(_FakeRouter())            # default prefix /plugins/<id>
    registry.register_router(_FakeRouter(), prefix="/x")  # explicit prefix honored
    registry.register_surface(_start, stop=_stop, name="surf")
    registry.register_subagent(_Sub())
'''


def test_plugin_contributes_router_surface_subagent(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "ext", enabled=True, body=_EXT_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg())

    # Routers: default prefix is namespaced to the plugin id; explicit honored.
    assert sorted(r["prefix"] for r in res.routers) == ["/plugins/ext", "/x"]
    assert all(r["plugin_id"] == "ext" for r in res.routers)
    # Surface + subagent collected, tagged with the plugin id.
    assert [s["name"] for s in res.surfaces] == ["surf"]
    assert all(s["plugin_id"] == "ext" for s in res.surfaces)
    assert [getattr(s, "name", None) for s in res.subagents] == ["plug_sub"]
    # Meta reports the counts.
    m = res.meta[0]
    assert m["routers"] == 2 and m["surfaces"] == 1 and m["subagents"] == ["plug_sub"]


# --- ADR 0019: config / secrets / settings ----------------------------------

_CFG_MANIFEST = (
    "config_section: cfgplug\n"
    "config: {greeting: hi, api_key: ''}\n"
    "secrets: [api_key]\n"
    "settings:\n"
    "  - {key: greeting, label: Greeting, type: string}\n"
    "  - {key: api_key, label: Key, type: secret}\n"
)


def test_plugin_declares_config_schema(tmp_path) -> None:
    from graph.plugins.pconfig import discover_plugin_config

    root = tmp_path / "plugins"
    _make_plugin(root, "cfgplug", enabled=True, manifest_extra=_CFG_MANIFEST)
    schemas = discover_plugin_config([root], {"cfgplug"})
    assert len(schemas) == 1
    s = schemas[0]
    assert s.section == "cfgplug"
    assert s.defaults == {"greeting": "hi", "api_key": ""}
    assert s.secrets == ["api_key"]
    assert [f["key"] for f in s.settings] == ["greeting", "api_key"]


def test_plugin_config_only_for_enabled(tmp_path) -> None:
    from graph.plugins.pconfig import discover_plugin_config

    root = tmp_path / "plugins"
    _make_plugin(root, "cfgplug", enabled=False, manifest_extra=_CFG_MANIFEST)
    assert discover_plugin_config([root], set()) == []          # disabled → none
    assert len(discover_plugin_config([root], {"cfgplug"})) == 1  # operator-enabled


def test_plugin_section_collision_with_builtin_ignored(tmp_path) -> None:
    from graph.plugins.pconfig import discover_plugin_config

    root = tmp_path / "plugins"
    _make_plugin(root, "evil", enabled=True,
                 manifest_extra="config_section: model\nconfig: {x: 1}\n")
    # 'model' is a reserved built-in section — the plugin can't claim it.
    assert discover_plugin_config([root], {"evil"}) == []


def test_plugins_disabled_overrides_manifest_enabled(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "onplug", enabled=True, tool="on_tool")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    # manifest enabled: true → loads
    assert [t.name for t in load_plugins(_cfg()).tools] == ["on_tool"]
    # plugins.disabled wins → not loaded
    assert load_plugins(_cfg(plugins_disabled=["onplug"])).tools == []


def test_registry_exposes_plugin_host() -> None:
    """A surface/route reaches host services (agent invoke + bus) via registry.host."""
    from pathlib import Path

    from graph.plugins.host import HOST
    from graph.plugins.registry import PluginRegistry

    r = PluginRegistry("p", Path("/tmp"))
    assert r.host is HOST                      # the process singleton the server fills
    assert hasattr(r.host, "invoke") and hasattr(r.host, "publish") and hasattr(r.host, "subscribe")


# ── console views (ADR 0026) ──────────────────────────────────────────────────


def test_manifest_parses_views() -> None:
    import tempfile
    from pathlib import Path as _P
    root = _P(tempfile.mkdtemp())
    _make_plugin(
        root, "viewy", enabled=True,
        manifest_extra=(
            "views:\n"
            "  - {id: board, label: Board, icon: LayoutDashboard, path: /plugins/viewy/board}\n"
            "  - {id: nopath, label: Bad}\n"   # missing path → dropped
        ),
    )
    m = load_manifest(root / "viewy")
    assert m is not None
    assert [v["id"] for v in m.views] == ["board"]   # the path-less one is dropped
    assert m.views[0]["icon"] == "LayoutDashboard"


def test_loader_meta_exposes_views_for_enabled_plugin(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(
        root, "viewy", enabled=True, tool="vt",
        manifest_extra="views:\n  - {id: board, label: Board, path: /plugins/viewy/board}\n",
    )
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["viewy"]))
    meta = res.meta[0]
    assert meta["id"] == "viewy" and meta["enabled"] is True
    assert [v["id"] for v in meta["views"]] == ["board"]


def test_loader_stamps_host_decided_trust_on_views(monkeypatch, tmp_path) -> None:
    # A `ui: react` plugin can NOT self-declare trust (ADR 0034 D5) — the host decides, via the
    # shipped allowlist + the operator's plugins.trusted. Default is untrusted → sandboxed iframe.
    root = tmp_path / "plugins"
    _make_plugin(
        root, "reacty", enabled=True, tool="rt",
        manifest_extra="views:\n  - {id: panel, label: Panel, path: /plugins/reacty/panel, ui: react}\n",
    )
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])

    # Not shipped-trusted, not operator-trusted → untrusted (renderer falls back to iframe).
    res = load_plugins(_cfg(plugins_enabled=["reacty"]))
    assert res.meta[0]["trusted"] is False
    assert res.meta[0]["views"][0]["trusted"] is False

    # Operator adds it to plugins.trusted → trusted (the React path is unlocked).
    res = load_plugins(_cfg(plugins_enabled=["reacty"], plugins_trusted=["reacty"]))
    assert res.meta[0]["trusted"] is True
    assert res.meta[0]["views"][0]["trusted"] is True


# ── full-bundle auto-discovery (ADR 0027) ─────────────────────────────────────


def test_plugin_autodiscovers_workflows_and_skills_dirs(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    d = _make_plugin(root, "bundle", enabled=True, tool="bt")
    (d / "workflows").mkdir()
    (d / "workflows" / "wf.yaml").write_text("name: wf\n", encoding="utf-8")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["bundle"]))
    assert any(p.name == "workflows" and "bundle" in str(p) for p in res.workflow_dirs)
    assert any(p.name == "skills" and "bundle" in str(p) for p in res.skill_dirs)


def test_register_workflow_dir(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    body = "def register(reg):\n    reg.register_workflow_dir('recipes')\n"
    d = _make_plugin(root, "wfp", enabled=True, body=body)
    (d / "recipes").mkdir()
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["wfp"]))
    assert any(p.name == "recipes" for p in res.workflow_dirs)


def test_plugin_goal_verifier_is_collected(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    body = (
        "async def _v(spec, ctx):\n"
        "    from graph.goals.types import VerifyResult\n"
        "    return VerifyResult(True, 'ok', '')\n"
        "def register(reg):\n"
        "    reg.register_goal_verifier('credits', _v)\n"
    )
    _make_plugin(root, "gverify", enabled=True, body=body)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["gverify"]))
    assert "gverify:credits" in res.goal_verifiers   # auto-namespaced (ADR 0028)


def test_plugin_goal_hook_is_collected(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    body = "def register(reg):\n    reg.register_goal_hook(on_achieved=lambda s: None)\n"
    _make_plugin(root, "ghook", enabled=True, body=body)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["ghook"]))
    assert len(res.goal_hooks) == 1 and res.goal_hooks[0]["plugin_id"] == "ghook"  # ADR 0028 D4


def test_plugin_knowledge_store_is_collected(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    body = (
        "def _factory(config):\n"
        "    return object()\n"
        "def register(reg):\n"
        "    reg.register_knowledge_store('pgvector', _factory)\n"
    )
    _make_plugin(root, "kbplugin", enabled=True, body=body)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["kbplugin"]))
    assert "pgvector" in res.knowledge_stores  # ADR 0031


def test_plugin_embedder_is_collected(monkeypatch, tmp_path) -> None:
    root = tmp_path / "plugins"
    body = (
        "def _embed(config):\n"
        "    return lambda text: [0.0, 1.0]\n"
        "def register(reg):\n"
        "    reg.register_embedder('local', _embed)\n"
    )
    _make_plugin(root, "embplugin", enabled=True, body=body)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    res = load_plugins(_cfg(plugins_enabled=["embplugin"]))
    assert "local" in res.embedders  # ADR 0031 follow-up
