"""The host-free plugin test harness (graph.plugins.testkit).

Proves a plugin's REAL modules — sibling modules with relative imports, a tool module
with a module-level ``@tool``, a ``register()`` with lazy host imports — load and run with
no protoAgent host, which is exactly what the standalone plugins (spacetraders, …) couldn't
do before. The fixture plugin lives in ``tests/fixtures/sample_plugin``.

Note: this suite runs INSIDE protoAgent, where the real ``graph.*`` host IS importable — so
``install_host_stubs`` correctly no-ops for those (it only stubs absent modules). The
stub MACHINERY is exercised against a ``phantom_host.*`` namespace that's guaranteed absent,
so the test never clobbers the real host.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest

from graph.plugins import testkit
from graph.plugins.loader import _plugin_module_name  # the harness must match the runtime
from graph.plugins.registry import PluginRegistry  # the surface FakeRegistry must mirror

FIXTURE = Path(__file__).parent / "fixtures" / "sample_plugin"
PID = "sample-plugin"


@pytest.fixture(autouse=True)
def _clean_modules():
    """Drop the fixture's synthetic package + any phantom stubs between tests."""
    yield
    pkg = testkit.plugin_module_name(PID)
    for m in [n for n in list(sys.modules) if n == pkg or n.startswith(pkg + ".") or n.split(".")[0] == "phantom_host"]:
        sys.modules.pop(m, None)


def test_package_name_matches_the_runtime_loader():
    # The whole point: tests must exercise the SAME import paths the host uses.
    assert testkit.plugin_module_name(PID) == _plugin_module_name(PID)
    assert testkit.plugin_module_name("sample-plugin") == "protoagent_plugin_sample_plugin"


def test_load_plugin_makes_sibling_modules_importable():
    pkg = testkit.load_plugin(FIXTURE, PID)
    # The deep engine module is reachable through the loaded package — the capability the
    # old "test register() only" scaffold lacked.
    engine = importlib.import_module(f"{testkit.plugin_module_name(PID)}.engine")
    out = engine.classify([{"name": "a", "size": 12}, {"name": "b", "size": 3}])
    assert out == {"big": ["a"], "small": ["b"]}
    assert hasattr(pkg, "register")


def test_tool_module_with_relative_import_and_decorator_loads():
    # tools.py has a MODULE-LEVEL `from . import engine` + `@tool` — unloadable bare.
    testkit.load_plugin(FIXTURE, PID)
    tools = importlib.import_module(f"{testkit.plugin_module_name(PID)}.tools")
    # The @tool-wrapped callable is invokable and routes through the engine.
    assert tools.summarize.invoke({"items_json": '[{"name":"x","size":50}]'}) == "1 big, 0 small"


def test_reload_is_clean_no_stale_submodules():
    testkit.load_plugin(FIXTURE, PID)
    name = testkit.plugin_module_name(PID)
    importlib.import_module(f"{name}.engine")
    testkit.load_plugin(FIXTURE, PID)  # re-load must purge cached submodules
    assert f"{name}.engine" not in sys.modules  # stale submodule dropped on reload


def test_register_runs_host_free_and_fake_registry_captures():
    testkit.install_host_stubs()  # no-op for real graph in-repo; stubs it standalone
    pkg = testkit.load_plugin(FIXTURE, PID)
    reg = testkit.FakeRegistry()
    pkg.register(reg)  # must not raise: the lazy `from graph.goals.types import VerifyResult` resolves
    assert len(reg.tools) == 1
    assert "sample:done" in reg.verifiers
    assert reg.skill_dirs == ["skills"]
    assert "sample-cmd" in reg.chat_commands  # "Sample Cmd" slugified — the #1637 hole, now captured
    assert len(reg.late_tool_factories) == 1


def test_fake_registry_captures_surface_lifecycle_callables():
    # #1729: register_surface used to keep only the NAME, so a plugin's surface
    # lifecycle wiring (arming watches in `start`, etc.) couldn't be exercised from
    # a register-smoke test. The start/stop/reload callables are now captured and the
    # smoke can actually CALL them and assert the side effect.
    reg = testkit.FakeRegistry(plugin_id="demo")
    armed = []

    def _start():
        armed.append("armed")

    def _stop():
        return None

    def _reload(cfg):
        return None

    reg.register_surface(_start, stop=_stop, name="fleet", reload=_reload)

    assert reg.surfaces == ["fleet"]  # names still captured for existing assertions
    start, stop, reload = reg.surface_specs["fleet"]
    assert (start, stop, reload) == (_start, _stop, _reload)
    start()  # exercisable: mutation-kills a plugin that forgets to wire `start`
    assert armed == ["armed"]

    # An unnamed surface is keyed by the plugin id (the effective name), so a plugin
    # that relies on the default name is still reachable.
    reg2 = testkit.FakeRegistry(plugin_id="solo")
    reg2.register_surface(_start)
    assert "solo" in reg2.surface_specs


# ── registry parity — the drift guard (#1637) ────────────────────────────────────────
# FakeRegistry drifted from PluginRegistry (register_chat_command was missing), which made
# that seam silently untestable: plugins hasattr-guard the call, so the smoke test passed
# while asserting nothing. These tests make the NEXT new seam fail here instead of drifting.


def _public_methods(cls) -> dict:
    return {n: fn for n, fn in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}


def test_fake_registry_mirrors_the_full_plugin_registry_surface():
    real = _public_methods(PluginRegistry)
    fake = _public_methods(testkit.FakeRegistry)
    assert "register_chat_command" in real  # sanity: introspection sees the surface
    for name, fn in real.items():
        fake_fn = fake.get(name)
        assert fake_fn is not None, (
            f"FakeRegistry is missing PluginRegistry.{name} — every public registry method "
            f"must be mirrored in graph/plugins/testkit.py, or the seam is silently "
            f"untestable in plugin smoke tests (plugins hasattr-guard these calls)."
        )
        real_params = [(p.name, p.kind) for p in inspect.signature(fn).parameters.values()]
        fake_params = [(p.name, p.kind) for p in inspect.signature(fake_fn).parameters.values()]
        assert fake_params == real_params, (
            f"FakeRegistry.{name} signature drifted from PluginRegistry.{name}: "
            f"{fake_params} != {real_params}"
        )


def test_testkit_slugify_matches_the_host_slugifier():
    # testkit is host-free by contract (vendored verbatim into standalone plugin CI), so it
    # duplicates slugify_slash instead of importing it — this keeps the copies in sync.
    from graph.slash_commands import slugify_slash

    for raw in ("Issue", "My Cmd", "foo_bar", "  /Weird--Token!!  ", "GOAL", "", "---", "héllo", "a1-b2"):
        assert testkit._slugify_slash(raw) == slugify_slash(raw), f"slug drift for {raw!r}"


def test_fake_registry_captures_chat_commands_slugified():
    reg = testkit.FakeRegistry()

    async def h(rest, session_id):
        return "ok"

    reg.register_chat_command("Issue", h)  # mixed case — stored under the live token
    assert reg.chat_commands == {"issue": h}


def test_fake_registry_chat_command_rejects_what_the_host_rejects():
    # The real registry warns-and-skips these (degrade-safe live); the fake raises so a
    # broken registration fails the test instead of shipping green.
    reg = testkit.FakeRegistry()

    async def h(rest, session_id):
        return "ok"

    with pytest.raises(ValueError):  # reserved core token
        reg.register_chat_command("goal", h)
    with pytest.raises(ValueError):  # slugifies to the reserved token too
        reg.register_chat_command("/GOAL", h)
    with pytest.raises(ValueError):  # lifecycle is reserved too (ADR 0074) — mirrors the host
        reg.register_chat_command("lifecycle", h)
    with pytest.raises(ValueError):  # …and its slugified variant
        reg.register_chat_command("/Lifecycle", h)
    with pytest.raises(ValueError):  # empty after slugify
        reg.register_chat_command("!!!", h)
    with pytest.raises(ValueError):  # non-callable handler
        reg.register_chat_command("fine-name", "not-callable")
    reg.register_chat_command("Issue", h)
    with pytest.raises(ValueError):  # duplicate token (live: first wins + warning)
        reg.register_chat_command("issue", h)
    assert reg.chat_commands == {"issue": h}  # nothing rejected leaked into the capture


def test_install_host_stubs_creates_patchable_seam_for_absent_host():
    # phantom_host.* is guaranteed absent, so the stub machinery (not the real graph) is tested.
    installed = testkit.install_host_stubs(extra={"phantom_host": {}, "phantom_host.sdk": {}})
    assert "phantom_host.sdk" in installed
    sdk = importlib.import_module("phantom_host.sdk")
    # An undeclared seam imports fine but raises if called unpatched (no silent fake-pass)…
    with pytest.raises(RuntimeError):
        sdk.complete("hi")
    # …and is monkeypatchable like the real seam.
    sdk.complete = lambda *_a, **_k: "patched"
    assert sdk.complete("hi") == "patched"


def test_install_host_stubs_leaves_the_real_host_untouched():
    before = sys.modules.get("graph")
    testkit.install_host_stubs()
    # The real graph package is still the same object — never stubbed or mutated.
    assert sys.modules.get("graph") is before
    assert importlib.import_module("graph.plugins.loader")  # real host still importable


def test_install_host_stubs_is_idempotent():
    first = testkit.install_host_stubs(extra={"phantom_host": {}, "phantom_host.sdk": {}})
    second = testkit.install_host_stubs(extra={"phantom_host": {}, "phantom_host.sdk": {}})
    assert "phantom_host.sdk" in first and second == []  # nothing re-installed the 2nd time


# ── subagent-config + knobs stubs (#1764) ────────────────────────────────────────────
# A plugin's register() doesn't just IMPORT host seams — it CONSTRUCTS a SubagentConfig and
# builds knob tools inline. Before #1764 `graph.subagents.config` was unstubbed
# (ModuleNotFoundError) and `graph.sdk` handed back a raise-when-called Knobs, so a scaffolded
# plugin that registered a subagent or used Knobs failed its OWN host-free smoke test.


def test_default_stubs_expose_a_permissive_subagent_config():
    # #1764: graph.subagents.config.SubagentConfig must be a record-only stand-in that stores
    # its kwargs, so register_subagent(SubagentConfig(...)) runs host-free and stays assertable.
    specs = testkit._default_stubs()
    assert "graph.subagents.config" in specs and "graph.subagents" in specs  # parent pkg too
    SubagentConfig = specs["graph.subagents.config"]["SubagentConfig"]
    sc = SubagentConfig(name="architect", description="d", system_prompt="p", tools=["read_file"])
    assert sc.name == "architect"
    assert sc.tools == ["read_file"]


def test_default_stubs_expose_chainable_knobs_and_a_tool_list():
    # #1764: graph.sdk Knobs/make_knob_tools are called at register() time, so they must be
    # real stand-ins (chainable no-op + list), not the raise-when-called placeholder.
    specs = testkit._default_stubs()
    Knobs = specs["graph.sdk"]["Knobs"]
    make_knob_tools = specs["graph.sdk"]["make_knob_tools"]
    knobs = Knobs()
    assert knobs.define("depth", 3, lo=1, hi=5).preset("deep", {"depth": 5}) is knobs  # chainable
    assert knobs.get("depth") == 3  # recorded default reads back
    tools = make_knob_tools(knobs, prefix="demo")
    assert isinstance(tools, list) and [t.name for t in tools] == ["demo_knobs", "demo_tune", "demo_preset"]


def test_register_with_subagent_and_knobs_runs_against_the_stubs(monkeypatch):
    # The end-to-end shape #1764 fixes: a register() that registers a subagent (SubagentConfig)
    # AND wires knob tools (Knobs/make_knob_tools). In-repo the real graph host is importable, so
    # mask just the two leaf modules the register() imports with the STUBS — exercising exactly
    # the standalone code path a scaffolded plugin runs. monkeypatch restores sys.modules after.
    specs = testkit._default_stubs()
    for name in ("graph.sdk", "graph.subagents.config"):
        monkeypatch.setitem(sys.modules, name, testkit._StubModule(name, specs[name]))

    def register(registry):
        from graph.sdk import Knobs, make_knob_tools
        from graph.subagents.config import SubagentConfig

        knobs = Knobs().define("depth", 3, lo=1, hi=5).preset("deep", {"depth": 5})
        registry.register_tools(make_knob_tools(knobs, prefix="demo"))
        registry.register_subagent(
            SubagentConfig(
                name="architect",
                description="Designs the plugin.",
                system_prompt="You are the architect.",
                tools=["read_file"],
            )
        )

    reg = testkit.FakeRegistry()
    register(reg)  # must not raise — the whole point of #1764

    assert len(reg.subagents) == 1  # the subagent contribution is recorded + assertable
    assert reg.subagents[0].name == "architect"
    assert reg.subagents[0].tools == ["read_file"]
    assert [t.name for t in reg.tools] == ["demo_knobs", "demo_tune", "demo_preset"]  # knob tools wired
