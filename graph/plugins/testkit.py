"""Host-free plugin test harness — load a plugin and exercise its REAL modules
(relative imports + host APIs) without a running protoAgent.

Why this exists: a plugin is a package whose modules use relative imports
(``from . import client``) and may touch host-only modules (``graph.*``,
``knowledge.store``) that protoAgent provides at runtime but aren't pip deps. So a
plain ``import client`` from the repo root fails, and a plugin's engine logic
(``fleet.autopilot`` role assignment, a trade-route ranker, …) goes untested unless
every bit is hand-extracted into dependency-free modules. This harness removes that
tax: it loads the plugin the way the host does, so its sibling modules are importable
and its host imports resolve to stubs.

Two capabilities:
  * ``load_plugin(root)``     — import the plugin dir as a package so ``from . import x``
    resolves and ``import <pkg>.fleet`` works → unit-test deep engine modules directly.
  * ``install_host_stubs()``  — register stub ``graph.*`` / ``knowledge.*`` modules in
    ``sys.modules`` so the plugin's host imports load (and are monkeypatchable) with no
    host. Plus ``FakeRegistry`` to capture what ``register()`` contributes.

Self-contained on purpose: **stdlib only, zero protoAgent-internal imports**, so it works
both in-repo (``from graph.plugins.testkit import load_plugin``) AND vendored verbatim into
a standalone plugin's CI (the scaffolder copies this file to ``tests/_plugin_testkit.py``).
The package naming mirrors ``graph/plugins/loader.py`` so tests exercise the SAME import
paths the runtime uses — keep the two in sync.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

__all__ = ["plugin_module_name", "load_plugin", "install_host_stubs", "FakeRegistry"]


def plugin_module_name(plugin_id: str) -> str:
    """The synthetic package name a plugin loads under — mirrors
    ``graph.plugins.loader._plugin_module_name`` (a hyphen in the module name breaks the
    relative-import machinery, so non-identifier chars become ``_``)."""
    return "protoagent_plugin_" + re.sub(r"\W", "_", plugin_id)


def load_plugin(root, plugin_id: str | None = None, *, entry: str = "__init__.py"):
    """Import a plugin directory as a PACKAGE and return the package module.

    After this, the plugin's own relative imports resolve and you can reach its sibling
    modules — ``import <pkg>.fleet`` / ``getattr(pkg, "fleet")`` — to unit-test engine
    logic directly, exactly as the host loads it (under ``protoagent_plugin_<id>`` with the
    plugin dir on the package search path). Idempotent + reload-safe: re-loading purges the
    package AND its cached submodules so an edited sibling re-execs (mirrors the loader).

    Args:
        root: the plugin directory (where ``__init__.py`` lives).
        plugin_id: the plugin id; defaults to the directory name.
        entry: the entry module filename (``__init__.py`` or ``plugin.py``).
    """
    root = Path(root).resolve()
    name = plugin_module_name(plugin_id or root.name)
    for cached in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        sys.modules.pop(cached, None)
    spec = importlib.util.spec_from_file_location(
        name, str(root / entry), submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create an import spec for {root / entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # register BEFORE exec so `from .x import y` finds the parent
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


# ── host stubs ───────────────────────────────────────────────────────────────────────
# protoAgent provides these modules at runtime but they aren't pip deps, so a plugin's
# `from graph.sdk import complete` / `from knowledge.store import KnowledgeStore` raise
# ModuleNotFoundError under bare pytest. We register lightweight stand-ins. Each undefined
# attribute resolves to a stub that's safe to IMPORT and to monkeypatch, but RAISES if
# actually called unpatched — so a test that exercises a host seam without patching it
# fails loudly rather than silently passing against a fake.


def _raise_unpatched(dotted: str):
    def _stub(*_a, **_k):
        raise RuntimeError(
            f"{dotted} is a stubbed host seam — monkeypatch it in your test "
            f"(install_host_stubs registered a placeholder so the import resolves).")
    return _stub


class _StubModule(types.ModuleType):
    """A stub host module: declared attributes are returned as-is; any other attribute
    resolves to a raise-when-called placeholder (so imports succeed and seams are
    patchable). Marked as a package (``__path__``) so submodules resolve."""

    def __init__(self, name: str, attrs: dict | None = None):
        super().__init__(name)
        self.__path__ = []  # type: ignore[attr-defined]
        for k, v in (attrs or {}).items():
            setattr(self, k, v)

    def __getattr__(self, item: str):
        if item.startswith("__"):
            raise AttributeError(item)
        return _raise_unpatched(f"{self.__name__}.{item}")


# Default host surface, derived from what real plugins import (spacetraders, project_board,
# notes, …). `extra` lets a plugin add its own; anything already importable is left alone.
def _default_stubs() -> dict:
    return {
        "graph": {},
        "graph.sdk": {},                       # run_subagent / subagent_types / config / complete
        "graph.config": {"LangGraphConfig": type("LangGraphConfig", (), {})},
        "graph.config_io": {"SECRETS_YAML_PATH": Path("config/secrets.yaml")},
        "graph.goals": {},
        "graph.goals.types": {"VerifyResult": type("VerifyResult", (), {
            "__init__": lambda self, **kw: self.__dict__.update(kw)})},
        "knowledge": {},
        "knowledge.store": {"KnowledgeStore": type("KnowledgeStore", (), {})},
    }


def install_host_stubs(extra: dict | None = None) -> list[str]:
    """Register stub host modules in ``sys.modules`` so a plugin's ``graph.*`` /
    ``knowledge.*`` imports resolve with no protoAgent present. Call BEFORE ``load_plugin``
    (or before importing any plugin module that imports the host).

    Idempotent and non-clobbering: a module that's already importable (a real one, or a
    stub from a previous call) is left untouched. ``extra`` is ``{module_name: {attr: val}}``
    to add or override host modules your plugin needs. Returns the names newly installed.
    """
    specs = _default_stubs()
    for name, attrs in (extra or {}).items():
        specs.setdefault(name, {}).update(attrs)
    installed: list[str] = []
    for name in sorted(specs, key=lambda n: n.count(".")):  # parents before children
        if name in sys.modules:
            continue                                        # already present (real or stubbed) — leave it
        try:
            __import__(name)                                # a real host module is installed → use it
            continue
        except Exception:
            pass
        module = _StubModule(name, specs[name])             # attrs set only when we CREATE the stub,
        sys.modules[name] = module                          # so a real host module is never clobbered
        installed.append(name)
        if "." in name:                                     # attach to the parent package
            parent, _, child = name.rpartition(".")
            if parent in sys.modules:
                setattr(sys.modules[parent], child, module)
    return installed


# ── fake registry ────────────────────────────────────────────────────────────────────
class FakeRegistry:
    """Records what ``register(registry)`` contributes, with no host — mirrors the real
    ``graph.plugins.registry.PluginRegistry`` surface so a plugin's ``register()`` runs
    unchanged. Assert against the captured lists/dicts.

    e.g. ``reg = FakeRegistry(); plugin.register(reg); assert reg.tools and reg.verifiers``.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.tools: list = []
        self.routers: list = []
        self.surfaces: list = []
        self.subagents: list = []
        self.middlewares: list = []
        self.mcp_servers: list = []
        self.a2a_skills: list = []
        self.skill_dirs: list = []
        self.workflow_dirs: list = []
        self.verifiers: dict = {}
        self.goal_hooks: list = []
        self.knowledge_stores: dict = {}
        self.embedders: dict = {}
        self.handlers: dict = {}          # topic -> [handlers]
        self.emitted: list = []           # (topic, data)
        self.navigations: list = []
        self.thread_id_resolver = None

    # contributions
    def register_tool(self, tool) -> None:
        self.tools.append(tool)

    def register_tools(self, tools) -> None:
        self.tools.extend(tools)

    def register_router(self, router, prefix: str | None = None) -> None:
        self.routers.append((prefix, router))

    def register_surface(self, start, stop=None, name: str | None = None, reload=None) -> None:
        self.surfaces.append(name)

    def register_subagent(self, config) -> None:
        self.subagents.append(config)

    def register_middleware(self, factory) -> None:
        self.middlewares.append(factory)

    def register_mcp_server(self, factory) -> None:
        self.mcp_servers.append(factory)

    def register_a2a_skill(self, spec: dict) -> None:
        self.a2a_skills.append(spec)

    def register_skill_dir(self, path) -> None:
        self.skill_dirs.append(str(path))

    def register_workflow_dir(self, path) -> None:
        self.workflow_dirs.append(str(path))

    def register_goal_verifier(self, name: str, fn) -> None:
        self.verifiers[name] = fn

    def register_goal_hook(self, *, on_achieved=None, on_failed=None) -> None:
        self.goal_hooks.append((on_achieved, on_failed))

    def register_knowledge_store(self, name: str, factory) -> None:
        self.knowledge_stores[name] = factory

    def register_embedder(self, name: str, factory) -> None:
        self.embedders[name] = factory

    def register_thread_id_resolver(self, fn) -> None:
        self.thread_id_resolver = fn

    # bus / nav (no-op capture)
    def emit(self, topic: str, data: dict | None = None) -> None:
        self.emitted.append((topic, data))

    def on(self, topic: str, handler) -> None:
        self.handlers.setdefault(topic, []).append(handler)

    def navigate(self, view: str = "") -> None:
        self.navigations.append(view)
