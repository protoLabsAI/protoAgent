"""Plugin config schema discovery (ADR 0019).

A plugin declares its config section in the manifest (pure data), so config-load,
secret-stripping, and the settings schema can know about it **without importing
the plugin** — that happens later, at ``register()`` time. This module reads
those declared schemas from manifests under the plugin roots.

Used by:
- ``graph/config.py::from_yaml`` — to read each plugin section into ``plugin_config``.
- ``graph/config_io.py`` — to extend ``SECRET_PATHS`` + ``config_to_dict``.
- ``graph/settings_schema.py`` — to append each plugin's Settings fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("protoagent.plugins")

# Built-in top-level config sections a plugin may NOT claim (collision → ignored,
# built-in wins). Keep roughly in step with the YAML the template ships.
_RESERVED_SECTIONS = {
    "model", "subagents", "middleware", "knowledge", "memory", "skills",
    "workflows", "compaction", "checkpoint", "routing", "goal", "execute_code",
    "operator", "tools", "discord", "google", "mcp", "plugins", "identity",
    "auth", "runtime", "telemetry", "instance", "prompt_cache", "enforcement",
    "ingest",
}


@dataclass
class PluginConfigSchema:
    plugin_id: str
    section: str
    defaults: dict = field(default_factory=dict)
    secrets: list = field(default_factory=list)
    settings: list = field(default_factory=list)


def discover_plugin_config(roots, enabled_ids) -> list[PluginConfigSchema]:
    """Config schemas of **enabled** plugins that declare config/secrets/settings.

    ``roots`` are plugin directories (bundle + live); ``enabled_ids`` the
    operator's ``plugins.enabled`` set (a manifest ``enabled: true`` also counts).
    A section colliding with a built-in (or a second plugin) is dropped (logged).
    Never raises — bad discovery yields no plugin config, not a broken boot.
    """
    try:
        from graph.plugins.loader import discover_plugins

        enabled = set(enabled_ids or set())
        out: list[PluginConfigSchema] = []
        claimed: dict[str, str] = {}
        for m in discover_plugins(list(roots)):
            if not (m.enabled or m.id in enabled):
                continue
            if not (m.config or m.settings or m.secrets):
                continue
            section = (m.config_section or m.id).strip()
            if section in _RESERVED_SECTIONS:
                log.warning("[plugins] %s: config_section %r collides with a built-in — ignored",
                            m.id, section)
                continue
            if section in claimed:
                log.warning("[plugins] config_section %r claimed by %s and %s — keeping first",
                            section, claimed[section], m.id)
                continue
            claimed[section] = m.id
            out.append(PluginConfigSchema(
                m.id, section, dict(m.config or {}), list(m.secrets or []), list(m.settings or []),
            ))
        return out
    except Exception:  # noqa: BLE001 — discovery is best-effort
        log.exception("[plugins] config-schema discovery failed")
        return []


def plugin_roots_from(config_dir: Path, dir_override: str = "") -> list[Path]:
    """Bundle + live plugin roots, computed from a config dir (no config object)."""
    from graph.config_io import _BUNDLE_CONFIG_DIR

    live = Path(dir_override).expanduser() if dir_override else (config_dir / "plugins")
    return [_BUNDLE_CONFIG_DIR.parent / "plugins", live]


def live_plugin_config_schemas() -> list[PluginConfigSchema]:
    """Discover schemas from the **live** config (for config_io + settings_schema,
    which operate on the running config without a config object)."""
    try:
        from graph.config_io import _live_config_dir, load_yaml_doc

        data = load_yaml_doc() or {}
        plugins = data.get("plugins") or {}
        roots = plugin_roots_from(_live_config_dir(), str(plugins.get("dir") or ""))
        return discover_plugin_config(roots, set(plugins.get("enabled") or []))
    except Exception:  # noqa: BLE001
        log.exception("[plugins] live config-schema discovery failed")
        return []
