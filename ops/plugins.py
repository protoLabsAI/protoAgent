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
from pathlib import Path
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


# ── Bundle peek (archetype preview) ────────────────────────────────────────────
# Enumerate what an UN-installed bundle would set up — members, each member's
# manifest identity, skills, pip deps, capabilities — without installing anything.
# Read-only: fetches to a throwaway staging dir, never touches plugins.lock or
# config. Results are TTL-cached per URL so the archetype picker can call freely.

_PEEK_TTL_SECONDS = 600.0
_peek_cache: dict[str, tuple[float, dict]] = {}


def _peek_skills(root: Path) -> list[dict]:
    """Name + description of every SKILL.md a plugin checkout ships."""
    from graph.skills.loader import parse_skill_md

    out = []
    for skill_md in sorted(root.glob("skills/*/SKILL.md")):
        art = parse_skill_md(skill_md)
        if art is not None:
            out.append({"name": art.name, "description": art.description})
    return out


def _peek_member_from(root: Path, entry: dict) -> dict:
    """Describe one bundle member from a checkout (builtin dir or fetched repo)."""
    from graph.plugins.manifest import load_manifest

    manifest = load_manifest(root)
    detail = {
        "id": entry.get("id"),
        "builtin": bool(entry.get("builtin")),
        "ref": entry.get("ref"),
        "url": entry.get("url"),
    }
    if manifest is None:
        detail["error"] = "manifest unreadable"
        return detail
    detail.update(
        {
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "requires_pip": list(manifest.requires_pip or []),
            "capabilities": dict(manifest.capabilities or {}),
            "views": [v.get("label") or v.get("id") for v in (manifest.views or [])],
            "skills": _peek_skills(root),
        }
    )
    return detail


def _peek_bundle_sync(url: str, ref: str | None = None) -> dict:
    import shutil
    import tempfile
    import time

    from graph.plugins import installer
    from infra.paths import instance_paths

    now = time.monotonic()
    hit = _peek_cache.get(url)
    if hit and now - hit[0] < _PEEK_TTL_SECONDS:
        return hit[1]

    builtin_root = instance_paths().app_root / "plugins"
    with tempfile.TemporaryDirectory(prefix="pa-bundle-peek-") as tmp:
        staging = Path(tmp) / "bundle"
        installer._fetch(url, ref, staging)
        bundle = installer.load_bundle(staging)
        if bundle is None:
            # Not a bundle — a single-plugin repo; preview it as one member. A plain
            # plugin declares no bundle-level mcp/secrets, so both are empty (the
            # dialog can read these keys uniformly across bundle + plugin previews).
            result = {
                "kind": "plugin",
                "members": [_peek_member_from(staging, {"id": None, "url": url, "ref": ref})],
                "mcp": [],
                "secrets": [],
            }
            _peek_cache[url] = (now, result)
            return result

        members = []
        for entry in bundle.get("plugins", []):
            pid = entry.get("id")
            try:
                if entry.get("builtin"):
                    members.append(_peek_member_from(builtin_root / pid, entry))
                else:
                    member_dir = Path(tmp) / f"member-{pid}"
                    installer._fetch(entry["url"], entry.get("ref"), member_dir)
                    members.append(_peek_member_from(member_dir, entry))
                    shutil.rmtree(member_dir, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 — one unreachable member ≠ no preview
                members.append({"id": pid, "builtin": bool(entry.get("builtin")), "error": str(exc)})

        result = {
            "kind": "bundle",
            "id": bundle.get("id"),
            "name": bundle.get("name"),
            "description": bundle.get("description"),
            "verified_against": bundle.get("verified_against"),
            "enabled": list(bundle.get("enabled") or []),
            "members": members,
            # Catalog-shaped MCP servers ({template, inputs: [{key, label, …}]}) and the
            # secrets ({key, label, placeholder, secret, required}) this bundle will ask
            # the operator to fill — surfaced so ArchetypePreviewDialog can show the inputs
            # up front, WITHOUT installing (this is a read-only peek). Slice 1 of #2041.
            "mcp": list(bundle.get("mcp") or []),
            "secrets": list(bundle.get("secrets") or []),
        }

    _peek_cache[url] = (now, result)
    return result


async def peek_bundle(url: str, ref: str | None = None) -> dict:
    """Async wrapper — blocking git/fs work runs off the event loop."""
    return await asyncio.to_thread(_peek_bundle_sync, url, ref)
