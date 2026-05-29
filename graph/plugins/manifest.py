"""Plugin manifest (``protoagent.plugin.yaml``) parsing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("protoagent.plugins")

MANIFEST_FILENAME = "protoagent.plugin.yaml"


@dataclass
class PluginManifest:
    """Declared metadata for a plugin. ``id`` + ``name`` are required."""

    id: str
    name: str
    path: Path
    version: str = "0.0.0"
    description: str = ""
    # ``enabled: true`` in the manifest is an author opt-in (for plugins you
    # wrote/dropped in yourself). An operator can also enable by id via
    # ``plugins.enabled`` in config. Either path counts as consent.
    enabled: bool = False
    requires_env: list[str] = field(default_factory=list)
    # Declarative, for transparency in the console — not yet enforced.
    capabilities: dict = field(default_factory=dict)
    entrypoint: str = ""  # optional module filename; defaults to __init__.py / plugin.py


def load_manifest(plugin_dir: Path) -> PluginManifest | None:
    """Parse ``<plugin_dir>/protoagent.plugin.yaml`` → ``PluginManifest``.

    Returns ``None`` (logged) for a missing/invalid manifest or one without the
    required ``id``/``name`` — never raises, so one bad plugin can't break
    discovery.
    """
    manifest_path = plugin_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("[plugins] %s: unreadable manifest: %s", plugin_dir.name, exc)
        return None
    if not isinstance(data, dict):
        log.warning("[plugins] %s: manifest is not a mapping", plugin_dir.name)
        return None

    pid = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    if not pid or not name:
        log.warning("[plugins] %s: manifest missing required id/name — skipping", plugin_dir.name)
        return None

    req = data.get("requires_env")
    requires_env = [str(x) for x in req] if isinstance(req, (list, tuple)) else []
    caps = data.get("capabilities")

    return PluginManifest(
        id=pid,
        name=name,
        path=plugin_dir,
        version=str(data.get("version", "0.0.0")),
        description=str(data.get("description", "")),
        enabled=bool(data.get("enabled", False)),
        requires_env=requires_env,
        capabilities=caps if isinstance(caps, dict) else {},
        entrypoint=str(data.get("entrypoint", "")).strip(),
    )
