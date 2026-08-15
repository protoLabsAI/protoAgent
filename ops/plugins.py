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
import re
from dataclasses import dataclass, field
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
    # Bundle mcp: servers seeded into the HOST config this install (#2118) — empty for
    # single plugins, non-activated installs, and bundles that declare no servers.
    mcp_seeded: list[str] = field(default_factory=list)
    # Per-plugin import/registration failures from the post-enable reload (#2716):
    # a plugin that fails to LOAD is skipped by the loader, so the reload itself still
    # succeeds — `reloaded=True` + an id in `enabled` is NOT proof the code is live.
    load_errors: dict[str, str] = field(default_factory=dict)


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
    respect_disabled: bool = False,
    ctx: OpContext,
    apply_settings: Callable[[dict], tuple[bool, list]] | None = None,
    mcp_inputs: dict | None = None,
    bundle_secrets: list | None = None,
) -> InstallResult:
    """Clone + pin the plugin (blocking git work off the event loop), then — when
    ``activate`` and an ``apply_settings`` applier is given — add it to ``plugins.enabled``,
    seed the bundle's config defaults, seed the bundle's declared ``mcp:`` servers +
    supplied ``secrets:`` values into the HOST config (#2118, same helpers/semantics as
    the workspace-create path), and apply (hot-reload). ``mcp_inputs`` (``{key: value}``)
    fills ``${input}`` placeholders with priority over the env; ``bundle_secrets``
    (``[{key, value}]``) writes DECLARED bundle secrets via ``save_secrets`` (0600,
    merge-not-clobber). Raises ``installer.InstallError`` on a failed install (the
    adapter maps it to its surface)."""
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
    currently_disabled = list(getattr(cfg, "plugins_disabled", []) or [])
    if respect_disabled:
        # UPDATE semantics (#2718): re-installing a bundle must not undo an operator's
        # explicit disable — ids sitting in plugins.disabled stay there; only ids with
        # no recorded state (new members) get the fresh-install enable treatment.
        ids = [p for p in ids if p not in currently_disabled]
        disabled = currently_disabled
    else:
        disabled = [p for p in currently_disabled if p not in ids]
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

    # Bundle services (#2118): the workspace-create path seeds a bundle's declared
    # `mcp:` templates + supplied `secrets:` values — the HOST path silently dropped
    # both. Same helpers, host paths (live config + host plugins.lock). Gated on the
    # activate path (the CLI's fetch-only `plugin install` stays fetch-only) and run
    # BEFORE apply_settings so the hot-reload boots any seeded servers. Union is by
    # name — a server the config already has always wins, so re-installs (and earlier
    # bundles in the same lock) never clobber operator values. File I/O + template
    # resolution run off the event loop (#497).
    mcp_seeded: list[str] = []
    if "bundle" in summary:
        from graph.config_io import config_yaml_path
        from graph.workspaces.manager import apply_bundle_mcp_servers, apply_bundle_secrets

        cfg_path = config_yaml_path()
        lock = installer.lock_path()
        mcp_seeded = await asyncio.to_thread(apply_bundle_mcp_servers, cfg_path, lock, mcp_inputs or {})
        if bundle_secrets:
            await asyncio.to_thread(apply_bundle_secrets, cfg_path, lock, list(bundle_secrets))

    # Off the event loop: the applier does a full config write + graph rebuild —
    # inline it stalls every other request for the duration (2732/2735 reviews;
    # same D9 rule the devkit's reload_plugins already follows).
    ok, messages = await asyncio.to_thread(apply_settings, config_updates)
    if ok:
        # The reload "succeeding" only means the graph rebuilt — the loader SKIPS a
        # plugin whose import/registration fails and records the failure on its meta
        # entry, so read the post-reload roster for the ids we just enabled (#2716).
        from runtime.state import STATE

        load_errors = {
            str(m.get("id")): str(m.get("error"))
            for m in (STATE.plugin_meta or [])
            if m.get("id") in ids and m.get("error")
        }
        return InstallResult(
            summary=summary,
            installed_ids=installed_ids,
            enabled=ids,
            reloaded=True,
            mcp_seeded=mcp_seeded,
            load_errors=load_errors,
        )
    # The install itself succeeded (code on disk + locked); surface the enable-reload
    # failure without failing the whole op — it can be enabled manually.
    return InstallResult(
        summary=summary,
        installed_ids=installed_ids,
        enabled=[],
        reloaded=False,
        mcp_seeded=mcp_seeded,
        enable_error="; ".join(messages) or "reload failed",
    )


# ── Bundle lifecycle past install (ADR 0049 D4, #2718) ─────────────────────────


@dataclass
class BundleUpdateResult:
    install: InstallResult  # the force-reinstall half (summary/enabled/reloaded/load_errors)
    removed_members: list[str]  # members the new manifest dropped, now uninstalled
    retire_error: str | None = None  # non-fatal: dropped-member cleanup/reload failure


@op(
    name="plugins.update_bundle",
    mutates=True,
    summary="Re-resolve a bundle's ref, reinstall members at the new pins, retire dropped ones, hot-reload.",
)
async def update_bundle(
    bundle_id: str,
    *,
    ref: str | None = None,
    by: str = "ops",
    allow: list[str] | None = None,
    ctx: OpContext,
    apply_settings: Callable[..., tuple[bool, list]] | None = None,
) -> BundleUpdateResult:
    """Bundle-level update: a force re-install of the bundle repo at its recorded ref
    (or ``ref``) — member pins re-resolve exactly as a fresh install would, the lock
    row is rewritten, and (via ``install_and_activate``) the declared enable set +
    config/mcp/secrets seeding re-apply as defaults with operator values winning.
    When updating the RECORDED ref, a release-tag pin moves to the newest semver
    tag first (tags are immutable — re-installing the recorded one would be a
    no-op forever, same rule as the single-plugin update route); an explicit
    caller ``ref`` is a pin request and is used as-is, never replaced.
    Members the NEW manifest no longer names are
    RETIRED afterwards (uninstalled + module-purged + a pure reload) when they're
    still exclusively this bundle's — a member another bundle lists, or one the
    operator re-installed directly, is left alone."""
    from graph.plugins import installer
    from graph.plugins.loader import purge_plugin_modules

    entry = await asyncio.to_thread(installer.bundle_entry, bundle_id)
    if entry is None:
        raise installer.BundleNotInstalledError(f"bundle {bundle_id!r} is not installed.")
    source_url = str(entry.get("source_url") or "")
    if not source_url:
        raise installer.InstallError(f"bundle {bundle_id!r} has no source_url — cannot update.")
    before_members = [str(p) for p in entry.get("plugins") or []]

    target_ref = ref or (entry.get("requested_ref") or None)
    # Chase the newest semver tag ONLY when updating the RECORDED ref — an explicit
    # caller-passed `ref` is a pin request and must never be silently replaced
    # (the 2732 review's coderabbit major; the CLI already had this guard).
    if ref is None and target_ref and installer.is_release_tag(target_ref):
        try:
            status = await asyncio.to_thread(installer.check_plugin_update, entry)
            target_ref = status.get("latest_ref") or target_ref
        except Exception:  # noqa: BLE001 — best-effort; fall back to the recorded ref
            pass

    res = await install_and_activate(
        source_url,
        target_ref,
        force=True,
        by=by,
        allow=allow,
        activate=apply_settings is not None,
        respect_disabled=True,  # an update must not undo an operator's explicit disable
        ctx=ctx,
        apply_settings=apply_settings,
    )

    # Accumulate retire failures — reassigning per member reported only the LAST
    # one and hid the rest (2732 review).
    retire_errors: list[str] = []
    dropped = await asyncio.to_thread(installer.orphaned_bundle_members, bundle_id, before_members)
    for pid in dropped:
        try:
            await asyncio.to_thread(installer.uninstall, pid)
        except installer.InstallError as exc:
            retire_errors.append(f"{pid}: {exc}")
            continue
        purge_plugin_modules(pid)
    if dropped and apply_settings is not None:
        # The member uninstalls scrubbed the YAML's enabled refs — a pure reload
        # (config=None) picks the file state up and drops their live tools/routes.
        # Off the event loop: full graph rebuild (same rule as the activate apply).
        ok, messages = await asyncio.to_thread(apply_settings, None)
        if not ok:
            retire_errors.append("; ".join(str(m) for m in messages) or "retire reload failed")
    return BundleUpdateResult(
        install=res, removed_members=dropped, retire_error="; ".join(retire_errors) or None
    )


@op(
    name="plugins.uninstall_bundle",
    mutates=True,
    summary="Uninstall a bundle: its exclusively-owned members + the lock row, then hot-reload.",
)
async def uninstall_bundle(
    bundle_id: str,
    *,
    purge: bool = False,
    ctx: OpContext,
    apply_settings: Callable[..., tuple[bool, list]] | None = None,
) -> dict:
    """One-action bundle removal (before this, removing a stack meant hand-uninstalling
    members against a provenance row that then went stale). Members shared with another
    bundle or re-owned by a direct install are kept — ``installer.uninstall_bundle``
    decides; this op adds the live half: module purge per removed member + a pure
    reload so their tools/routes actually leave the running agent."""
    from graph.plugins import installer
    from graph.plugins.loader import purge_plugin_modules

    report = await asyncio.to_thread(installer.uninstall_bundle, bundle_id, purge=purge)
    for pid in report.get("removed_members") or []:
        purge_plugin_modules(pid)
    reloaded = False
    reload_error: str | None = None
    if apply_settings is not None and report.get("removed_members"):
        # Off the event loop — full graph rebuild (2732/2735 reviews, D9 rule).
        ok, messages = await asyncio.to_thread(apply_settings, None)
        reloaded = bool(ok)
        if not ok:
            reload_error = "; ".join(str(m) for m in messages) or "reload failed"
    return {**report, "reloaded": reloaded, "reload_error": reload_error}


# ── Bundle peek (archetype preview) ────────────────────────────────────────────
# Enumerate what an UN-installed bundle would set up — members, each member's
# manifest identity, skills, pip deps, capabilities — without installing anything.
# Read-only: fetches to a throwaway staging dir, never touches plugins.lock or
# config. Results are TTL-cached per URL so the archetype picker can call freely.

_PEEK_TTL_SECONDS = 600.0
_peek_cache: dict[str, tuple[float, dict]] = {}

# A bundle-manifest member id used in a filesystem path must be ONE safe path
# component: leading alnum, then alnum/dot/underscore/hyphen — no separators, so
# `..`/`x/../y` can't resolve outside the peek tempdir. (Deliberately a superset of
# the loader's install-time id rule; this guard is about path safety only.)
_SAFE_MEMBER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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
            # The id comes from a FETCHED manifest — untrusted data used in a path.
            # Without this check a manifest id like `x/../../dir` resolves the member
            # dir OUTSIDE the TemporaryDirectory before installer._fetch writes (the
            # 2735 review's major). One safe path component, nothing else.
            if not _SAFE_MEMBER_ID_RE.fullmatch(str(pid or "")):
                members.append(
                    {"id": pid, "builtin": bool(entry.get("builtin")), "error": "invalid member id in manifest"}
                )
                continue
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
            # The archetype: block, verbatim — so pre-install surfaces (the devkit's
            # verify_bundle, future preview UI) can sanity-check it without installing.
            "archetype": dict(bundle.get("archetype") or {}),
        }

    _peek_cache[url] = (now, result)
    return result


async def peek_bundle(url: str, ref: str | None = None) -> dict:
    """Async wrapper — blocking git/fs work runs off the event loop."""
    return await asyncio.to_thread(_peek_bundle_sync, url, ref)
