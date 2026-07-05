"""Plugin ops (ADR 0075 D2) — install a plugin AND activate it, as one op.

`install_and_activate` is the ADR's flagship: it wraps `installer.install` **plus** the
auto-enable + hot-reload dance that a CLI/MCP install couldn't do before (add to
`plugins.enabled`, seed the bundle's config defaults, rebuild the running agent). One op,
shared by the console route, a future `protoagent plugin install`, and the operator MCP.

The terminal step — apply the config change + rebuild the live agent — is a **host
capability** (`server.agent_init._apply_settings_changes`) that lives above this layer, so
it's **injected** as `apply_settings`, keeping `ops` free of any `server` import. A live
surface (REST) passes the real applier; a disk-only caller passes `None` (install to disk,
no reload). Stale-router detection (does re-installing over a live mount need a restart?) is
about the live HTTP app, so it stays in the REST adapter — computed there from the
`installed_ids` this op returns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from ops import OpContext, op


@dataclass
class InstallResult:
    summary: dict  # installer.install's summary (single plugin or bundle)
    installed_ids: list[str]  # plugin ids whose CODE landed on disk this install
    enabled: list[str]  # ids added to plugins.enabled + reloaded (empty when not activated)
    reloaded: bool
    enable_error: str | None = None


def _enabled_ids_from_summary(summary: dict) -> list[str]:
    """Plugin id(s) to enable: a single plugin → its id; a bundle → its declared ``enabled``
    set (else every installed member)."""
    if "bundle" in summary:
        suggested = [str(x) for x in (summary.get("enabled") or [])]
        members = [str(s["id"]) for s in (summary.get("installed") or []) if s.get("id")]
        return suggested or members
    pid = summary.get("id")
    return [str(pid)] if pid else []


def _installed_ids_from_summary(summary: dict) -> list[str]:
    """Plugin id(s) whose CODE this install just placed on disk — for a bundle, its fetched
    members only (``builtin`` members aren't fetched, so can't have been replaced live)."""
    if "bundle" in summary:
        return [str(s["id"]) for s in (summary.get("installed") or []) if s.get("id")]
    pid = summary.get("id")
    return [str(pid)] if pid else []


@op(
    name="plugins.install_and_activate",
    mutates=True,
    summary="Install a plugin from git and (by default) enable + hot-reload it.",
)
async def install_and_activate(
    url: str,
    ref: str | None = None,
    *,
    force: bool = False,
    by: str = "ops",
    allow: list[str] | None = None,
    activate: bool = True,
    ctx: OpContext,
    apply_settings: Callable[[dict], tuple[bool, list]] | None = None,
) -> InstallResult:
    """Clone + pin the plugin (blocking git work off the event loop), then — when
    ``activate`` and an ``apply_settings`` applier is given — add it to ``plugins.enabled``,
    seed the bundle's config defaults, and apply (hot-reload). Raises
    ``installer.InstallError`` on a failed install (the adapter maps it to its surface)."""
    from graph.plugins import installer
    from graph.plugins.loader import purge_plugin_modules

    summary = await asyncio.to_thread(installer.install, url, ref, force=force, by=by, allow=allow)

    installed_ids = _installed_ids_from_summary(summary)
    # Drop each re-installed plugin's module subtree so the reload re-execs from the fresh
    # checkout (a first install is a no-op here; matters on force re-install / update).
    for pid in installed_ids:
        purge_plugin_modules(pid)

    ids = _enabled_ids_from_summary(summary)
    if not (activate and ids and apply_settings):
        return InstallResult(summary=summary, installed_ids=installed_ids, enabled=[], reloaded=False)

    cfg = ctx.graph_config
    enabled = list(getattr(cfg, "plugins_enabled", []) or [])
    disabled = [p for p in (getattr(cfg, "plugins_disabled", []) or []) if p not in ids]
    for pid in ids:
        if pid not in enabled:
            enabled.append(pid)
    config_updates: dict = {"plugins": {"enabled": enabled, "disabled": disabled}}

    # Seed the bundle's recommended per-plugin config defaults (#1350), same trust gate as
    # auto-enable — defaults only, reduced against the live YAML so an operator value is
    # never clobbered.
    bundle_config = summary.get("config") if "bundle" in summary else None
    if bundle_config:
        from graph.config_io import config_yaml_path, load_yaml_doc
        from graph.plugins.installer import bundle_config_overlay

        current = load_yaml_doc(config_yaml_path())
        overlay = bundle_config_overlay(bundle_config, current if isinstance(current, dict) else {})
        config_updates.update(overlay)

    ok, messages = apply_settings(config_updates)
    if ok:
        return InstallResult(summary=summary, installed_ids=installed_ids, enabled=ids, reloaded=True)
    # The install itself succeeded (code on disk + locked); surface the enable-reload
    # failure without failing the whole op — it can be enabled manually.
    return InstallResult(
        summary=summary,
        installed_ids=installed_ids,
        enabled=[],
        reloaded=False,
        enable_error="; ".join(messages) or "reload failed",
    )
