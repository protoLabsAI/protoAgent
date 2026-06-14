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
import sys
from pathlib import Path

import pytest

from graph.plugins import testkit
from graph.plugins.loader import _plugin_module_name  # the harness must match the runtime

FIXTURE = Path(__file__).parent / "fixtures" / "sample_plugin"
PID = "sample-plugin"


@pytest.fixture(autouse=True)
def _clean_modules():
    """Drop the fixture's synthetic package + any phantom stubs between tests."""
    yield
    pkg = testkit.plugin_module_name(PID)
    for m in [n for n in list(sys.modules)
              if n == pkg or n.startswith(pkg + ".") or n.split(".")[0] == "phantom_host"]:
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
