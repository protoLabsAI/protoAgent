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
    "model",
    "subagents",
    "middleware",
    "knowledge",
    "memory",
    "skills",
    "workflows",
    "compaction",
    "checkpoint",
    "routing",
    "goal",
    # NB: plugin sections (e.g. `discord` from the external discord plugin) are NOT
    # reserved — a plugin (bundled or external) legitimately claims its own section.
    "operator",
    "tools",
    "mcp",
    "plugins",
    "identity",
    "auth",
    "runtime",
    "telemetry",
    "instance",
    "prompt_cache",
    "enforcement",
    "ingest",
}


@dataclass
class PluginConfigSchema:
    plugin_id: str
    section: str
    defaults: dict = field(default_factory=dict)
    secrets: list = field(default_factory=list)
    settings: list = field(default_factory=list)
    test: bool = False  # has a /api/config/test-<section> check (ADR 0029)
    guide_url: str = ""  # optional setup-guide link rendered next to the group (ADR 0059)
    settings_tabs: list = field(default_factory=list)  # ordered {id, label, …} Configure tabs (#3179)


def discover_plugin_config(roots, enabled_ids, disabled_ids=None, *, strict: bool = False) -> list[PluginConfigSchema]:
    """Config schemas of **active** plugins that declare config/secrets/settings.

    ``roots`` are plugin directories (bundle + live); ``enabled_ids`` the operator's
    ``plugins.enabled`` set (a manifest ``enabled: true`` also counts);
    ``disabled_ids`` (``plugins.disabled``) turns one off regardless. A section
    colliding with a built-in (or a second plugin) is dropped (logged). Never raises
    by default — bad discovery yields no plugin config, not a broken boot. With
    ``strict=True`` a discovery ERROR propagates instead of returning ``[]``, so a
    caller (secret routing / config redaction) can tell a real failure from a
    genuinely-empty result and fail SAFE rather than open.
    """
    try:
        from graph.plugins.host import timed_lifecycle_phase
        from graph.plugins.loader import discover_plugins

        enabled = set(enabled_ids or set())
        disabled = set(disabled_ids or set())
        out: list[PluginConfigSchema] = []
        claimed: dict[str, str] = {}
        for m in discover_plugins(list(roots)):
            if m.id in disabled or not (m.enabled or m.id in enabled):
                continue
            if not (m.config or m.settings or m.secrets):
                continue
            # Config stage of the plugin lifecycle (#2675) — this plugin's schema/
            # defaults/secrets resolution, timed per plugin like the loader's
            # load/registration stages.
            with timed_lifecycle_phase(m.id, "config"):
                section = (m.config_section or m.id).strip()
                if section in _RESERVED_SECTIONS:
                    log.warning("[plugins] %s: config_section %r collides with a built-in — ignored", m.id, section)
                    continue
                if section in claimed:
                    log.warning(
                        "[plugins] config_section %r claimed by %s and %s — keeping first",
                        section,
                        claimed[section],
                        m.id,
                    )
                    continue
                claimed[section] = m.id
                out.append(
                    PluginConfigSchema(
                        m.id,
                        section,
                        dict(m.config or {}),
                        list(m.secrets or []),
                        list(m.settings or []),
                        test=bool(getattr(m, "test", False)),
                        guide_url=str(getattr(m, "guide_url", "") or ""),
                        settings_tabs=list(getattr(m, "settings_tabs", []) or []),
                    )
                )
        return out
    except Exception:  # noqa: BLE001 — discovery is best-effort
        log.exception("[plugins] config-schema discovery failed")
        if strict:
            raise
        return []


# The value last refused, so a refusal is said once per breakage rather than on every
# resolution — and said AGAIN when a value that was fixed is later broken again. (A set of
# every value ever warned about stayed silent for the life of the process instead.)
_LAST_REFUSED_PLUGIN_DIR: str | None = None


def valid_plugins_dir_override(raw: object) -> str:
    """The usable ``plugins.dir`` override, or ``""`` — the ONE validator every reader
    shares (the loader's roots, the installer's live dir, the config-schema discovery).

    A RELATIVE value is refused, not anchored: it resolves against the working directory
    of whichever process asks, so the server and an out-of-process CLI / fleet subprocess
    would read different folders — the console would list plugins the agent never loads.
    Same call the fs fence makes for a relative ``projects[].path`` (``graph/config.py``),
    and the same shape of answer: warn with the value, then fall back to the default root
    so the agent still boots on its normal plugins instead of an empty one."""
    global _LAST_REFUSED_PLUGIN_DIR
    text = str(raw or "").strip()
    if not text:
        _LAST_REFUSED_PLUGIN_DIR = None  # no override — a later bad one warns afresh
        return ""
    expanded = Path(text).expanduser()
    # Absoluteness is judged on the EXPANDED-but-unresolved path: `.resolve()` makes every
    # path absolute (against the CWD), which would swallow the input this refuses.
    if expanded.is_absolute():
        _LAST_REFUSED_PLUGIN_DIR = None  # fixed — a later breakage warns again
        return str(expanded)
    if text != _LAST_REFUSED_PLUGIN_DIR:
        _LAST_REFUSED_PLUGIN_DIR = text
        log.warning(
            "[plugins] plugins.dir %r is not absolute — ignored, using the instance's own "
            "plugins dir. A relative path resolves against the working directory of whatever "
            "process reads it (the server, a CLI, a fleet subprocess), so they would not "
            "agree on where plugins live. Set an absolute path.",
            text,
        )
    return ""


def refused_plugins_dir_message(config_value: object = None) -> str | None:
    """The operator banner for a REFUSED plugin-root override — a relative ``plugins.dir``
    (config) or ``PROTOAGENT_PLUGINS_DIR`` (env) — else ``None``.

    Unlike the fs fence skipping one project, this refusal moves the WHOLE plugin root
    to the fallback, so the operator's plugins simply stop loading; a log line alone left
    nothing on screen to explain it. Only a refusal that actually decides the root counts:
    an absolute ``plugins.dir`` wins, so a bad env var under it changes nothing and says
    nothing. The banner names the directory plugins really load from instead."""
    import os

    text = str(config_value or "").strip()
    if text:
        if Path(text).expanduser().is_absolute():
            return None  # the config override is valid and wins; the env var is irrelevant
        return (
            f"plugins.dir {text!r} is not absolute, so it is ignored and plugins load from "
            f"{_fallback_plugins_root()} instead — a relative path resolves differently in each "
            "process (server, CLI, fleet subprocess). Set an absolute path."
        )
    env = os.environ.get("PROTOAGENT_PLUGINS_DIR", "").strip()
    if env and not Path(env).expanduser().is_absolute():
        return (
            f"PROTOAGENT_PLUGINS_DIR {env!r} is not absolute, so it is ignored and plugins load from "
            f"{_fallback_plugins_root()} instead. Set an absolute path."
        )
    return None


def _fallback_plugins_root() -> str:
    """Where plugins load from when ``plugins.dir`` is refused: the instance's plugins dir,
    which itself honours an ABSOLUTE ``PROTOAGENT_PLUGINS_DIR``."""
    try:
        from infra.paths import instance_paths

        return str(instance_paths().plugins_dir)
    except Exception:  # noqa: BLE001 — a banner's wording must never break plugin loading
        return "the instance's own plugins dir"


def plugin_roots_from(plugins_root: Path, dir_override: str = "") -> list[Path]:
    """Bundle + live plugin roots (no config object).

    ``plugins_root`` is the instance's live plugins dir (e.g.
    ``instance_paths().plugins_dir``); a usable ``dir_override`` (config ``plugins.dir``,
    vetted by :func:`valid_plugins_dir_override`) wins over it. The bundle root ships
    in-tree under the app root (``app_root/plugins``)."""
    from graph.plugins.installer import bundled_plugins_dir

    override = valid_plugins_dir_override(dir_override)
    live = Path(override) if override else Path(plugins_root)
    # `bundled_plugins_dir()` IS `app_root/plugins`; going through it keeps ONE
    # expression for the bundled tree across the loader, the installer and here.
    return [bundled_plugins_dir(), live]


def live_plugin_config_schemas() -> list[PluginConfigSchema]:
    """Discover schemas from the **live** config (for config_io + settings_schema,
    which operate on the running config without a config object)."""
    try:
        from infra.paths import instance_paths

        from graph.config_io import load_yaml_doc

        data = load_yaml_doc() or {}
        plugins = data.get("plugins") or {}
        roots = plugin_roots_from(instance_paths().plugins_dir, str(plugins.get("dir") or ""))
        return discover_plugin_config(
            roots,
            set(plugins.get("enabled") or []),
            set(plugins.get("disabled") or []),
        )
    except Exception:  # noqa: BLE001
        log.exception("[plugins] live config-schema discovery failed")
        return []


def installed_plugin_config_schemas(*, strict: bool = False) -> list[PluginConfigSchema]:
    """Like ``live_plugin_config_schemas`` but for EVERY installed plugin — enabled or
    not. The SECRET-ROUTING + config-redaction paths use this so a secret saved for a
    currently-DISABLED plugin is still pulled into ``secrets.yaml`` (never left in
    plaintext in the live config) and never echoed back to the API. The settings UI
    keeps the enabled-only view (you don't configure a plugin that's off).

    ``strict=True`` propagates a discovery error (vs returning ``[]``) so the
    secret-routing / redaction callers can fail SAFE instead of treating a transient
    failure as "no secrets"."""
    try:
        from infra.paths import instance_paths

        from graph.config_io import load_yaml_doc
        from graph.plugins.loader import discover_plugins

        data = load_yaml_doc() or {}
        roots = plugin_roots_from(instance_paths().plugins_dir, str((data.get("plugins") or {}).get("dir") or ""))
        all_ids = {m.id for m in discover_plugins(roots)}
        return discover_plugin_config(roots, all_ids, set(), strict=strict)  # every installed plugin, on or off
    except Exception:  # noqa: BLE001
        log.exception("[plugins] installed config-schema discovery failed")
        if strict:
            raise
        return []
