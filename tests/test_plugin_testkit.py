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
