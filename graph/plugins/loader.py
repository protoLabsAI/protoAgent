"""Discover and load drop-in plugins.

Two roots, mirroring config/skills: bundled (``<repo>/plugins/``, shipped
examples) and live (``<config_dir>/plugins/`` or ``plugins.dir``). A plugin is
loaded only when **enabled** — either ``enabled: true`` in its manifest (author
opt-in) or its id listed in ``plugins.enabled`` (operator opt-in). Enabled
plugins are imported and run **in-process**; everything is best-effort so one
bad plugin can't break the rest or the boot.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from graph.plugins.manifest import PluginManifest, load_manifest
from graph.plugins.registry import PluginRegistry

log = logging.getLogger("protoagent.plugins")


@dataclass
class PluginLoadResult:
    tools: list = field(default_factory=list)
    skill_dirs: list = field(default_factory=list)
    workflow_dirs: list = field(default_factory=list)  # *.yaml recipe dirs (ADR 0027)
    goal_verifiers: dict = field(default_factory=dict)  # name -> verifier fn (ADR 0028)
    goal_hooks: list = field(default_factory=list)       # {on_achieved, on_failed} (ADR 0028)
    knowledge_stores: dict = field(default_factory=dict)  # name -> backend factory (ADR 0031)
    embedders: dict = field(default_factory=dict)        # name -> embed_fn factory (ADR 0031)
    a2a_skills: list = field(default_factory=list)  # A2A card skill specs (#570)
    routers: list = field(default_factory=list)    # {plugin_id, router, prefix} (ADR 0018)
    surfaces: list = field(default_factory=list)    # {plugin_id, name, start, stop}
    subagents: list = field(default_factory=list)   # SubagentConfig
    middleware: list = field(default_factory=list)  # factories: (config) -> AgentMiddleware|None (ADR 0032)
    mcp_servers: list = field(default_factory=list)  # factories: config -> entry|None (ADR 0019)
    thread_id_resolver: object = None  # (request_metadata, session_id) -> str (#571); last plugin wins
    meta: list[dict] = field(default_factory=list)


def discover_plugins(roots: list[Path]) -> list[PluginManifest]:
    """Find plugins (dirs with a manifest) under *roots*. Live overrides bundle
    by id (later root wins)."""
    by_id: dict[str, PluginManifest] = {}
    for root in roots:
        if not (root and root.exists() and root.is_dir()):
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest = load_manifest(child)
            if manifest is not None:
                by_id[manifest.id] = manifest
    return list(by_id.values())


def _entry_file(manifest: PluginManifest) -> Path | None:
    if manifest.entrypoint:
        candidate = manifest.path / manifest.entrypoint
        return candidate if candidate.exists() else None
    for name in ("__init__.py", "plugin.py"):
        candidate = manifest.path / name
        if candidate.exists():
            return candidate
    return None


def _plugin_module_name(plugin_id: str) -> str:
    """A valid Python module name for a plugin id. Non-identifier chars (e.g. the
    hyphen in ``finance-data``) become ``_`` — a hyphen in the module name breaks
    the relative-import machinery."""
    return "protoagent_plugin_" + re.sub(r"\W", "_", plugin_id)


def _load_plugin_module(manifest: PluginManifest, entry: Path):
    """Import a plugin's entry ``__init__.py`` as a **package** so it can have
    sibling modules and use relative imports (``from .tools import …``). The
    module is registered in ``sys.modules`` BEFORE exec — relative imports resolve
    the parent package there — and the name is sanitized to a valid identifier."""
    mod_name = _plugin_module_name(manifest.id)
    spec = importlib.util.spec_from_file_location(
        mod_name, str(entry), submodule_search_locations=[str(manifest.path)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # so `from .x import y` finds the parent
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _import_register(manifest: PluginManifest):
    """Import a plugin's entry module and return its ``register`` callable."""
    entry = _entry_file(manifest)
    if entry is None:
        raise RuntimeError("no entry module (expected __init__.py or plugin.py)")
    module = _load_plugin_module(manifest, entry)
    register = getattr(module, "register", None)
    if not callable(register):
        raise RuntimeError("plugin module has no callable register(registry)")
    return register


def run_plugin_mcp_main(plugin_id: str) -> None:
    """Frozen-binary entrypoint for a plugin's managed MCP server (ADR 0019).

    Find the plugin by id across the default roots, import its entry module, and
    call its ``mcp_main()`` (the subprocess body of its managed MCP server). Used
    by the ``--mcp-plugin <id>`` shim when there's no ``python`` on PATH. Importing
    the module does NOT call ``register`` — only defines its functions — so this
    is side-effect-free apart from running the server.
    """
    from graph.config_io import _BUNDLE_CONFIG_DIR, _live_config_dir

    roots = [_BUNDLE_CONFIG_DIR.parent / "plugins", _live_config_dir() / "plugins"]
    for manifest in discover_plugins(roots):
        if manifest.id != plugin_id:
            continue
        entry = _entry_file(manifest)
        if entry is None:
            raise RuntimeError(f"plugin {plugin_id!r} has no entry module")
        module = _load_plugin_module(manifest, entry)
        mcp_main = getattr(module, "mcp_main", None)
        if not callable(mcp_main):
            raise RuntimeError(f"plugin {plugin_id!r} has no mcp_main()")
        mcp_main()
        return
    raise RuntimeError(f"plugin {plugin_id!r} not found for --mcp-plugin")


def load_plugins(config, *, core_tool_names: set[str] | None = None) -> PluginLoadResult:
    """Load enabled plugins and collect their contributions.

    ``core_tool_names`` lets the caller pass the already-registered tool names so
    plugin tools that would shadow them are skipped (the OpenClaw collision rule).
    """
    result = PluginLoadResult()
    roots = _plugin_roots(config)
    enabled_ids = set(getattr(config, "plugins_enabled", []) or [])
    disabled_ids = set(getattr(config, "plugins_disabled", []) or [])
    seen_tool_names = set(core_tool_names or set())

    for manifest in discover_plugins(roots):
        # plugins.disabled wins — turn off a bundled plugin (e.g. a first-party
        # surface) without deleting it or editing core.
        enabled = (manifest.enabled or manifest.id in enabled_ids) and manifest.id not in disabled_ids
        entry = {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "enabled": enabled,
            "loaded": False,
            "tools": [],
            "skills": 0,
            # Console surfaces (ADR 0026) — the rail reads these from
            # /api/runtime/status to render a dynamic icon + iframe per view.
            "views": list(manifest.views) if enabled else [],
        }

        if not enabled:
            result.meta.append(entry)
            continue

        missing = [v for v in manifest.requires_env if not os.environ.get(v)]
        if missing:
            entry["error"] = f"missing env: {', '.join(missing)}"
            log.warning("[plugins] %s enabled but %s — skipping", manifest.id, entry["error"])
            result.meta.append(entry)
            continue

        try:
            register = _import_register(manifest)
            # Resolved config section (ADR 0019) — defaults if not in plugin_config.
            section = manifest.config_section or manifest.id
            pconf = (getattr(config, "plugin_config", {}) or {}).get(section) or dict(manifest.config or {})
            registry = PluginRegistry(manifest.id, manifest.path, config=pconf)
            register(registry)
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not break boot
            # Clear diagnostic when an enabled plugin's declared deps aren't
            # installed (ADR 0027 D4: install fetches code; deps are explicit).
            if isinstance(exc, ModuleNotFoundError) and manifest.requires_pip:
                entry["error"] = (
                    f"declared deps not installed ({', '.join(manifest.requires_pip)}) — "
                    f"run: python -m server plugin install-deps {manifest.id}"
                )
            else:
                entry["error"] = str(exc)
            log.warning("[plugins] %s failed to load: %s — skipping", manifest.id, entry["error"])
            result.meta.append(entry)
            continue

        kept = []
        for tool in registry.tools:
            if tool.name in seen_tool_names:
                log.warning("[plugins] %s: tool %s collides with an existing tool — skipped",
                            manifest.id, tool.name)
                continue
            seen_tool_names.add(tool.name)
            kept.append(tool)

        result.tools.extend(kept)
        result.skill_dirs.extend(registry.skill_dirs)
        result.workflow_dirs.extend(registry.workflow_dirs)
        # Full-bundle auto-discovery (ADR 0027): a plugin repo can ship SKILL.md
        # skills + *.yaml workflows in conventional subdirs and they're picked up
        # without any register_* boilerplate — so installing a repo pulls in the
        # whole bundle (tools+subagents via register(), skills+workflows as data).
        conv_skills = manifest.path / "skills"
        if conv_skills.is_dir() and conv_skills not in result.skill_dirs:
            result.skill_dirs.append(conv_skills)
        conv_workflows = manifest.path / "workflows"
        if conv_workflows.is_dir() and conv_workflows not in result.workflow_dirs:
            result.workflow_dirs.append(conv_workflows)
        result.a2a_skills.extend(registry.a2a_skills)
        if registry.thread_id_resolver is not None:  # last plugin wins (#571)
            if result.thread_id_resolver is not None:
                log.warning("[plugins] %s overrides a thread_id resolver already set by "
                            "another plugin", manifest.id)
            result.thread_id_resolver = registry.thread_id_resolver
        # Surfaces / routes / subagents (ADR 0018) — tagged with the plugin id so
        # the server can namespace + report them.
        for r in registry.routers:
            result.routers.append({"plugin_id": manifest.id, **r})
        for s in registry.surfaces:
            result.surfaces.append({"plugin_id": manifest.id, **s})
        result.subagents.extend(registry.subagents)
        result.middleware.extend(registry.middleware)  # ADR 0032
        for name, fn in registry.goal_verifiers.items():  # ADR 0028
            if name in result.goal_verifiers:
                log.warning("[plugins] %s: goal verifier %s collides — skipped", manifest.id, name)
                continue
            result.goal_verifiers[name] = fn
        result.goal_hooks.extend(registry.goal_hooks)  # ADR 0028 D4
        for name, factory in registry.knowledge_stores.items():  # ADR 0031
            if name in result.knowledge_stores:
                log.warning("[plugins] %s: knowledge backend %s collides — skipped", manifest.id, name)
                continue
            result.knowledge_stores[name] = factory
        for name, factory in registry.embedders.items():  # ADR 0031 follow-up
            if name in result.embedders:
                log.warning("[plugins] %s: embedder %s collides — skipped", manifest.id, name)
                continue
            result.embedders[name] = factory
        for f in registry.mcp_servers:
            result.mcp_servers.append({"plugin_id": manifest.id, "factory": f})
        entry["loaded"] = True
        entry["tools"] = [t.name for t in kept]
        entry["skills"] = len(registry.skill_dirs)
        entry["routers"] = len(registry.routers)
        entry["surfaces"] = len(registry.surfaces)
        entry["subagents"] = [getattr(c, "name", "?") for c in registry.subagents]
        entry["mcp_servers"] = len(registry.mcp_servers)
        result.meta.append(entry)
        log.info("[plugins] loaded %s: %d tool(s), %d skill dir(s), %d route(s), "
                 "%d surface(s), %d subagent(s), %d mcp server(s)",
                 manifest.id, len(kept), len(registry.skill_dirs),
                 len(registry.routers), len(registry.surfaces),
                 len(registry.subagents), len(registry.mcp_servers))

    return result


def _plugin_roots(config) -> list[Path]:
    from graph.config_io import _BUNDLE_CONFIG_DIR, _live_config_dir

    repo_root = _BUNDLE_CONFIG_DIR.parent
    live_override = getattr(config, "plugins_dir", "") or ""
    live_root = Path(live_override).expanduser() if live_override else (_live_config_dir() / "plugins")
    return [repo_root / "plugins", live_root]  # bundle first, live overrides
