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
    # Plugin config (ADR 0019) — declared as data so it's known at config-load /
    # secret-strip / settings-schema time, before register() ever imports.
    #   config_section: the top-level YAML section the plugin claims (default: id)
    #   config:    defaults for that section (key → default value)
    #   secrets:   keys in the section routed to the secrets.yaml overlay
    #   settings:  Settings-schema field specs ({key, label, type, ...})
    config_section: str = ""
    config: dict = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    # Test action (ADR 0029) — when true, the plugin serves a credential check at
    # `POST /api/config/test-<config_section>` (e.g. the chat_surface wirer mounts
    # one), and the console renders a generic "Test connection" button for the
    # group. No console edit needed per plugin.
    test: bool = False
    # Console surfaces (ADR 0026) — each entry adds a left-rail icon opening a
    # full view (an iframe of a page the plugin serves at `path`). Declared as
    # data so it's known without importing the plugin, and surfaced to the
    # frontend via /api/runtime/status. Each: {id, label, icon, path, tabs?}.
    views: list[dict] = field(default_factory=list)
    # Event contract (ADR 0039) — the topics this plugin broadcasts / listens for.
    # Declarative for discoverability (surfaced in /api/runtime/status): a plugin
    # "ships" its events as its public API so others subscribe by topic without
    # importing it. Not enforced — publish is auto-namespaced + guarded at runtime;
    # subscribing to any topic is allowed.
    emits: list[str] = field(default_factory=list)
    subscribes: list[str] = field(default_factory=list)
    # Distribution (ADR 0027) — for plugins installed from a git URL.
    #   requires_pip: declared pip deps. NOT auto-installed (install ≠ code exec);
    #     the operator installs them explicitly. Missing → clear error on enable.
    #   repository/homepage: provenance, shown in the install review.
    #   min_protoagent_version: compat guard (warn/refuse on an older host).
    requires_pip: list[str] = field(default_factory=list)
    repository: str = ""
    homepage: str = ""
    min_protoagent_version: str = ""


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

    cfg = data.get("config")
    secrets = data.get("secrets")
    settings = data.get("settings")
    views = data.get("views")
    emits = data.get("emits")
    subscribes = data.get("subscribes")
    requires_pip = data.get("requires_pip")
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
        config_section=str(data.get("config_section", "")).strip() or pid,
        config=cfg if isinstance(cfg, dict) else {},
        secrets=[str(s) for s in secrets] if isinstance(secrets, (list, tuple)) else [],
        settings=[s for s in settings if isinstance(s, dict)] if isinstance(settings, (list, tuple)) else [],
        test=bool(data.get("test", False)),
        views=[v for v in views if isinstance(v, dict) and v.get("id") and v.get("path")]
        if isinstance(views, (list, tuple)) else [],
        emits=[str(x) for x in emits] if isinstance(emits, (list, tuple)) else [],
        subscribes=[str(x) for x in subscribes] if isinstance(subscribes, (list, tuple)) else [],
        requires_pip=[str(x) for x in requires_pip] if isinstance(requires_pip, (list, tuple)) else [],
        repository=str(data.get("repository", "")).strip(),
        homepage=str(data.get("homepage", "")).strip(),
        min_protoagent_version=str(data.get("min_protoagent_version", "")).strip(),
    )
