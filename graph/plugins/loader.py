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
from dataclasses import dataclass, field
from pathlib import Path

from graph.plugins.manifest import PluginManifest, load_manifest
from graph.plugins.registry import PluginRegistry

log = logging.getLogger("protoagent.plugins")


@dataclass
class PluginLoadResult:
    tools: list = field(default_factory=list)
    skill_dirs: list = field(default_factory=list)
    routers: list = field(default_factory=list)    # {plugin_id, router, prefix} (ADR 0018)
    surfaces: list = field(default_factory=list)    # {plugin_id, name, start, stop}
    subagents: list = field(default_factory=list)   # SubagentConfig
    mcp_servers: list = field(default_factory=list)  # factories: config -> entry|None (ADR 0019)
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


def _import_register(manifest: PluginManifest):
    """Import a plugin's entry module and return its ``register`` callable."""
    entry = _entry_file(manifest)
    if entry is None:
        raise RuntimeError("no entry module (expected __init__.py or plugin.py)")
    mod_name = f"protoagent_plugin_{manifest.id}"
    spec = importlib.util.spec_from_file_location(
        mod_name, str(entry), submodule_search_locations=[str(manifest.path)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {entry}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
        mod_name = f"protoagent_plugin_{manifest.id}"
        spec = importlib.util.spec_from_file_location(
            mod_name, str(entry), submodule_search_locations=[str(manifest.path)]
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not import plugin {plugin_id!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
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
            entry["error"] = str(exc)
            log.warning("[plugins] %s failed to load: %s — skipping", manifest.id, exc)
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
        # Surfaces / routes / subagents (ADR 0018) — tagged with the plugin id so
        # the server can namespace + report them.
        for r in registry.routers:
            result.routers.append({"plugin_id": manifest.id, **r})
        for s in registry.surfaces:
            result.surfaces.append({"plugin_id": manifest.id, **s})
        result.subagents.extend(registry.subagents)
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
