"""A graph rebuild must not leak a fresh copy of every plugin (#3365).

``load_plugins`` re-executed every plugin's whole module tree on every rebuild, and
the previous generation was never released: functions handed to ``register(registry)``
carry ``__globals__`` — the old module's ``__dict__`` — and third-party registries
(pydantic model classes, SQLAlchemy annotation types) key off the classes each exec
creates. Popping the name out of ``sys.modules`` frees none of that. A process that
rebuilt on a cadence grew ~6.4 MB per rebuild, linearly and without bound, until the
host ran out of memory.

These tests pin the fix at the level that actually matters — **module object
identity**. RSS is too noisy to assert on; "how many generations of this plugin are
alive" is exact, and is the thing that was wrong.
"""

from __future__ import annotations

import gc
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins, purge_plugin_modules

_MULTIFILE_INIT = '''
from langchain_core.tools import tool
from .helper import HELPER_VALUE

@tool
async def do_thing(x: str = "") -> str:
    """example"""
    return x + HELPER_VALUE

def register(registry):
    registry.register_tool(do_thing)
'''


def _make_multifile_plugin(root: Path, pid: str, helper_value: str = "v1") -> Path:
    """A plugin with a sibling module — the shape that made the purge necessary."""
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: true\n", encoding="utf-8"
    )
    (d / "__init__.py").write_text(_MULTIFILE_INIT, encoding="utf-8")
    (d / "helper.py").write_text(f'HELPER_VALUE = "{helper_value}"\n', encoding="utf-8")
    return d


def _live_plugin_modules(pid: str) -> list[types.ModuleType]:
    """Every module object for ``pid`` still reachable — including generations that
    were purged from ``sys.modules`` but never actually freed."""
    prefix = plugin_loader._plugin_module_name(pid)
    gc.collect()
    gc.collect()
    return [
        m
        for m in gc.get_objects()
        if isinstance(m, types.ModuleType)
        and getattr(m, "__name__", "").startswith(prefix)
    ]


@pytest.fixture
def plugin_root(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    yield root
    # Module-level cache is process state — don't leak it into the next test.
    purge_plugin_modules("leaky")


def test_rebuilds_do_not_accumulate_module_generations(plugin_root) -> None:
    """Five rebuilds of unchanged code must leave exactly one generation alive.

    The results are held for the duration, which is what makes this a faithful
    reproduction rather than a vacuous one: a rebuild does not drop the previous
    generation on the floor (a turn still streaming finishes on the old graph, so
    its tools stay referenced), and a retained tool pins its module through
    ``__globals__``. Discard the results and even the unfixed loader looks clean
    here — in production the pinning came from the graph plus third-party
    registries, which no minimal fixture reproduces.
    """
    _make_multifile_plugin(plugin_root, "leaky")
    cfg = LangGraphConfig()

    results = [load_plugins(cfg) for _ in range(5)]
    assert all(r.tools for r in results)  # the reference that does the pinning

    live = _live_plugin_modules("leaky")
    dupes = Counter(m.__name__ for m in live)
    assert dupes and max(dupes.values()) == 1, f"module generations accumulated: {dupes}"

    # Nothing purged-but-alive: every survivor is the live one.
    orphans = [m for m in live if sys.modules.get(m.__name__) is not m]
    assert orphans == [], f"purged modules still reachable: {[m.__name__ for m in orphans]}"


def test_unchanged_sources_are_not_re_executed(plugin_root) -> None:
    """The mechanism: same bytes on disk → literally the same module object."""
    _make_multifile_plugin(plugin_root, "leaky")
    cfg = LangGraphConfig()

    load_plugins(cfg)
    first = sys.modules[plugin_loader._plugin_module_name("leaky")]
    load_plugins(cfg)
    second = sys.modules[plugin_loader._plugin_module_name("leaky")]

    assert first is second


def test_edited_sources_still_re_execute(plugin_root) -> None:
    """The devkit's 'edit then reload_plugins' loop must keep working — the cache is
    keyed on source content, so an edit to a SIBLING module goes live too.

    The replacement value is deliberately a DIFFERENT LENGTH from the original.
    CPython validates a cached ``.pyc`` against the source's mtime in *seconds*
    plus its size, so a same-size edit inside the same second reloads stale
    bytecode — with or without this cache (verified against ``main``). That is a
    property of the interpreter, not of the loader, but it makes a naive
    ``"v1" -> "v2"`` fixture silently test nothing.
    """
    d = _make_multifile_plugin(plugin_root, "leaky", helper_value="v1")
    cfg = LangGraphConfig()

    load_plugins(cfg)
    mod_name = plugin_loader._plugin_module_name("leaky")
    assert sys.modules[f"{mod_name}.helper"].HELPER_VALUE == "v1"
    first = sys.modules[mod_name]

    (d / "helper.py").write_text('HELPER_VALUE = "edited-second-generation"\n', encoding="utf-8")

    load_plugins(cfg)
    assert sys.modules[mod_name] is not first, "an edit must force a re-exec"
    assert sys.modules[f"{mod_name}.helper"].HELPER_VALUE == "edited-second-generation"


def test_purge_forces_a_re_execution(plugin_root) -> None:
    """A force re-install/update purges to guarantee fresh code off disk; that must
    not then be answered out of the re-exec cache."""
    _make_multifile_plugin(plugin_root, "leaky")
    cfg = LangGraphConfig()

    load_plugins(cfg)
    first = sys.modules[plugin_loader._plugin_module_name("leaky")]

    purge_plugin_modules("leaky")

    load_plugins(cfg)
    assert sys.modules[plugin_loader._plugin_module_name("leaky")] is not first
