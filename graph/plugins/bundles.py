"""Bundle install/uninstall — a bundle is a reference manifest naming a set of plugin
repos to install together (ADR 0027 / 0042 / 0049 D4).

Split out of ``graph/plugins/installer.py`` (#3849, after #3823). ``installer``
re-exports every name here (the same objects), so ``installer._install_bundle`` /
``installer.uninstall_bundle`` / ``installer.bundle_config_overlay`` / … keep resolving
for callers and tests, and ``installer.install`` keeps calling ``load_bundle`` /
``_install_bundle`` by bare name through its own namespace.

**Seam rule.** The test suite patches collaborators on the ``installer`` module
(``install``, ``_read_lock``, ``_write_lock``, ``check_plugin_update``, ``uninstall``,
…). So this module reaches EVERY installer-namespace collaborator — including its own
moved siblings, which ``installer`` re-exports — through the installer module at call
time (``_inst().install(...)``), never via ``from graph.plugins.installer import x``:
a from-import would bind the original function and silently stop honoring those
patches. ``tests/test_plugin_bundles_seam.py`` guards this. ``installer`` is imported
lazily (it imports this module at its bottom, so a top-level import here would cycle).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


def _inst() -> ModuleType:
    """The installer module, resolved at call time (see the module docstring)."""
    from graph.plugins import installer

    return installer


BUNDLE_FILENAME = "protoagent.bundle.yaml"


def load_bundle(repo: Path) -> dict | None:
    """Parse ``<repo>/protoagent.bundle.yaml`` → a bundle dict, or ``None`` if it's
    absent/invalid. A **bundle** is a reference manifest: it names a set of plugin
    repos (``{id, url, ref}`` or ``{id, builtin: true}``) to install together, plus a
    suggested ``enabled`` list + ``config``. It carries no plugin code of its own."""
    import yaml

    f = repo / BUNDLE_FILENAME
    if not f.exists():
        return None
    try:
        doc = yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or not doc.get("id") or not isinstance(doc.get("plugins"), list):
        return None
    return doc


# The input types a bundle's `config_inputs:` may declare (#2934). Mirrors the MCP
# catalog input shape ({key, label, type, required?, default?}) plus an optional `help`
# line; the SetupWizard / NewAgentPanel set-up step picks the widget from `type`
# (string → text, path → folder picker, delegate → dropdown of configured ACP
# delegates, boolean → switch).
CONFIG_INPUT_TYPES = ("string", "path", "delegate", "boolean")

# Core sections a bundle may never prompt for through `config_inputs:` — the Configure
# step writes answers (and manifest `default`s, with no operator involvement) straight
# into the tracked config, and `required: true` is a hard gate ("type it to proceed").
# A bundle is trusted code, but a Configure prompt labelled "API key" that lands in
# `model.api_key`, or one that silently widens `network`/`egress`/`projects`, is a lying
# form. Plugin sections (`project_board.*`, `github.*`) are what the mechanism is for.
CONFIG_INPUT_RESERVED_SECTIONS = frozenset(
    {
        "model", "auth", "network", "security", "egress", "plugins", "filesystem",
        "projects", "onboarding", "delegates", "identity", "instance", "secrets", "mcp",
        "self_improvement",
    }
)

# A declared `key` is a DOTTED CONFIG PATH ("section.key[...]") the install path writes
# the operator's answer to — at least two safe segments, so a bundle can't declare a
# bare top-level key (which would replace a whole section) or smuggle YAML weirdness.
_CONFIG_INPUT_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)+$")


def normalize_config_inputs(bundle_id: str, raw: object, *, strict: bool = True) -> list[dict]:
    """Validate + normalize a bundle's ``config_inputs:`` block (#2934) into
    ``[{key, label, type, required, help?, default?}]``. ``strict`` (the install path) raises
    :class:`InstallError` on a malformed entry so a typo'd manifest fails the install
    with a reason instead of silently dropping the operator prompt; ``strict=False``
    (the read-only peek) drops bad entries so one typo can't blank the whole preview."""

    def bad(reason: str) -> None:
        if strict:
            raise _inst().InstallError(f"bundle {bundle_id!r}: invalid config_inputs entry — {reason}")

    if raw is None:
        return []
    if not isinstance(raw, list):
        bad("config_inputs must be a list")
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            bad(f"{entry!r} is not a mapping")
            continue
        key = str(entry.get("key", "")).strip()
        if not _CONFIG_INPUT_KEY_RE.fullmatch(key):
            bad(f"key {entry.get('key')!r} is not a dotted config path (e.g. section.key)")
            continue
        if key.split(".", 1)[0] in CONFIG_INPUT_RESERVED_SECTIONS:
            bad(f"key {key!r} targets a core section a bundle may not prompt for")
            continue
        label = str(entry.get("label", "")).strip()
        if not label:
            bad(f"{key!r} has no label")
            continue
        typ = str(entry.get("type") or "string").strip().lower()
        if typ not in CONFIG_INPUT_TYPES:
            bad(f"{key!r} has unknown type {entry.get('type')!r} (known: {', '.join(CONFIG_INPUT_TYPES)})")
            continue
        norm: dict = {"key": key, "label": label, "type": typ, "required": bool(entry.get("required"))}
        # `project: true` on a `path` input (#PM-first-run): the answered path is a repo
        # the agent MANAGES — the create path also registers it in the ADR 0095
        # `projects:` registry (fs fence, GitHub picker, board), records its GitHub
        # remote, and scopes `onboarding.root` to its parent. A plain flag, ignored by
        # hosts that predate it, so a bundle can declare it without a core-version gate.
        if typ == "path" and bool(entry.get("project")):
            norm["project"] = True
        # `help:` — an optional one-line explanation rendered under the field, so the
        # `label` can stay a short name ("Allow GitHub writes") instead of carrying the
        # whole explanation. Optional + additive: an older host ignores it, and a bundle
        # that folds its explanation into a long `label` still renders as before.
        help_text = str(entry.get("help") or "").strip()
        if help_text:
            norm["help"] = help_text
        if "default" in entry and entry["default"] is not None:
            default = _inst().coerce_config_input_value(typ, entry["default"])
            if default is not None:
                norm["default"] = default
        out.append(norm)
    return out


def coerce_config_input_value(typ: str, value: object) -> object | None:
    """Coerce an operator-supplied (or manifest-default) config_inputs value to its
    declared ``type``. Returns the value to write into the config YAML, or ``None``
    when it can't be interpreted (blank text, an unrecognized boolean word) — the
    caller skips the write rather than persisting junk. A quoted ``"false"`` must
    come out ``False``, so booleans parse words, never truthiness."""
    if typ == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return None
    text = str(value).strip()
    return text or None


def bundle_config_overlay(bundle_config: dict | None, current: dict | None) -> dict:
    """Reduce a bundle's recommended ``config:`` ({section: {key: val}}) to a DEFAULTS
    overlay: only the keys the operator hasn't already set in ``current`` (the live
    config sections). Per-section, per-leaf — an operator value always wins and a
    present key is left untouched, so applying this never clobbers existing settings.
    Empty sections are dropped. Shared by the console install route and the fleet
    create path so both apply bundle defaults identically (#1350)."""
    overlay: dict = {}
    cur = current if isinstance(current, dict) else {}
    for section, values in (bundle_config or {}).items():
        if not isinstance(values, dict):
            continue
        existing = cur.get(section)
        existing = existing if isinstance(existing, dict) else {}
        fill = {k: v for k, v in values.items() if k not in existing}
        if fill:
            overlay[str(section)] = fill
    return overlay


# Every archetype: key the reader (`fleet_routes._archetypes`) consumes. The block is
# otherwise cached verbatim into plugins.lock, so anything outside this set is dead
# weight the author almost certainly misspelled.
_ARCHETYPE_KEYS = {"label", "icon", "blurb", "soul", "soul_preset", "tier", "requires", "requires_tools"}


def _checked_archetype_block(bundle_id: str, arch: dict | None) -> dict:
    """The bundle's ``archetype:`` block, warning on keys no reader consumes (#2715)."""
    arch = dict(arch or {})
    unknown = sorted(set(arch) - _ARCHETYPE_KEYS)
    if unknown:
        _inst().log.warning(
            "[plugins] bundle %s archetype: block has unknown key(s) %s — known keys: %s",
            bundle_id,
            ", ".join(unknown),
            ", ".join(sorted(_ARCHETYPE_KEYS)),
        )
    return arch


def _install_bundle(
    bundle: dict,
    bundle_url: str,
    bundle_sha: str,
    ref: str | None,
    *,
    force: bool,
    by: str,
    allow: list[str] | None,
    install_runtime_deps: bool = False,
) -> dict:
    """Install every plugin a bundle names (reusing single-plugin ``install()`` for
    each — so each member is allow-checked + pinned in ``plugins.lock`` exactly as a
    direct install), then record the bundle for provenance. Enable + config are
    *suggested* in the return value, never applied (install ≠ enable ≠ trust)."""
    bid = str(bundle.get("id"))
    # Validate the declared operator prompts (#2934) BEFORE fetching any member — a
    # malformed config_inputs entry fails the install with a reason, not after N clones.
    config_inputs = _inst().normalize_config_inputs(bid, bundle.get("config_inputs"))
    installed: list[dict] = []
    skipped: list[str] = []
    superseded: list[str] = []
    bundle_warnings: list[str] = []
    for entry in bundle.get("plugins") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("builtin"):
            skipped.append(str(entry.get("id", "?")))  # ships with protoAgent — nothing to fetch
            continue
        purl = entry.get("url")
        if not purl:
            raise _inst().InstallError(f"bundle {bid!r}: plugin {entry.get('id', '?')!r} has no url")
        # A member listed by a URL a bundled plugin supersedes moved into core: treat it
        # like `builtin: true` (skip, don't fetch), so an archetype repo that still lists
        # the old URL installs unchanged on hosts old and new. Bundles carry no
        # min-version, so the repo can't be re-pointed at `builtin: true` without
        # breaking every host that predates the move.
        moved = _inst().superseding_plugin(str(purl))
        if moved is not None:
            from graph.plugins.manifest import display_source

            _inst().log.info(
                "[plugins] bundle %s: member %s ships with protoAgent now (bundled v%s supersedes %s) — skipped",
                bid,
                entry.get("id", moved.id),
                moved.version,
                display_source(str(purl)),
            )
            superseded.append(moved.id)
            # The member's pin is a floor (#2960). A pin AHEAD of what protoAgent ships
            # can't be honoured — the bundled copy is the member now — so say so instead
            # of silently running an older version than the archetype asked for.
            pin, shipped = _inst()._semver_key(str(entry.get("ref") or "")), _inst()._semver_key(moved.version)
            if pin is not None and shipped is not None and pin > shipped:
                warn = (
                    f"{moved.id}: the bundle pins {entry.get('ref')}, but this protoAgent ships "
                    f"{moved.id} v{moved.version} (it supersedes the git repo) — running the bundled "
                    f"copy; update protoAgent for the newer version."
                )
                bundle_warnings.append(warn)
                _inst().log.warning("[plugins] bundle %s: %s", bid, warn)
            continue

        member_ref = entry.get("ref")
        # Independent member semver chase (#2960): a release-tag pin in the bundle
        # manifest is a FLOOR, not the answer — an operator may have force-installed
        # the member ahead of the archetype's pin, and blindly re-pinning to the
        # manifest would DOWNGRADE it. ls-remote the member repo (via
        # check_plugin_update, the same newest-semver-tag logic the single-plugin
        # update route rides) and install the newest tag when one exists. SHA pins
        # and branch refs pass through untouched.
        #
        # BOUNDED by caret semantics (ADR 0049 amendment): the chase was unbounded, so a
        # member's first breaking release propagated to every archetype spawn automatically,
        # over a pin that looked like it was preventing exactly that. The floor now means
        # "this version or a compatible newer one", which is what a pin was always read as.
        if member_ref and _inst().is_release_tag(member_ref):
            try:
                member_lock = {
                    "id": str(entry.get("id", "")),
                    "source_url": str(purl),
                    "requested_ref": member_ref,
                    "resolved_sha": "",
                }
                status = _inst().check_plugin_update(member_lock)
                latest = status.get("latest_ref")
                if latest and _inst().is_compatible_upgrade(member_ref, latest):
                    member_ref = latest
                elif latest:
                    _inst().log.info(
                        "[plugins] bundle %s: member %s stays at %s — %s is outside the "
                        "compatible range; bump the manifest pin deliberately to adopt it",
                        bid, entry.get("id", "?"), member_ref, latest,
                    )
            except Exception:  # noqa: BLE001 — best-effort; fall back to the manifest pin
                pass

        installed.append(
            _inst().install(
                str(purl),
                member_ref,
                force=force,
                by=f"bundle:{bid}",
                allow=allow,
                install_runtime_deps=install_runtime_deps,
            )
        )

    lock = _inst()._read_lock()
    lock.setdefault("bundles", [])
    lock["bundles"] = [b for b in lock["bundles"] if b.get("id") != bid]
    lock["bundles"].append(
        {
            "id": bid,
            # Display name persisted so the console's Installed table can label member
            # rows with their bundle without re-fetching the manifest (older locks lack
            # it — consumers fall back to the id).
            "name": str(bundle.get("name") or ""),
            "source_url": bundle_url,
            "requested_ref": ref or "",
            "resolved_sha": bundle_sha,
            "plugins": [s["id"] for s in installed],
            # Members this bundle names by a URL a bundled plugin supersedes — not fetched
            # (the bundled copy is the member now), so not in `plugins`, which stays "the
            # code this bundle installed" for ownership/uninstall. Kept so the no-`enabled`
            # fallback ("turn on every member") still turns them on, as it did before the
            # plugin moved into core.
            "superseded": superseded,
            # The bundle's curated turn-on list (a subset of `plugins`). Cached here so a
            # consumer that only sees the lock — e.g. the fleet new-agent path, which
            # installs via a CLI subprocess and never sees the live install summary — can
            # auto-enable exactly what the bundle author intended. Empty = enable all members.
            "enabled": list(bundle.get("enabled") or []),
            # The bundle's recommended per-plugin config defaults ({section: {key: val}}).
            # Cached for the same lock-only consumer; applied as DEFAULTS (operator values
            # win, present keys are never clobbered — see `bundle_config_overlay`).
            "config": dict(bundle.get("config") or {}),
            # The bundle's MCP servers to seed (ADR 0083 D5, #2011): catalog-shaped
            # {template, inputs} items. Cached for the lock-only create path, which seeds
            # them into the workspace's `mcp.servers` via `_apply_bundle_mcp_servers` —
            # `config:`'s dict-leaf overlay can't merge the `mcp.servers` list.
            "mcp": list(bundle.get("mcp") or []),
            # The bundle's declared secrets ({key, label, placeholder, secret, required} —
            # same shape as an mcp-catalog input). Cached alongside `mcp` so the lock-only
            # create path can prompt for / seed these inputs without re-parsing the bundle
            # manifest (#2041, slice 1).
            "secrets": list(bundle.get("secrets") or []),
            # The plugin config keys the SetupWizard/NewAgentPanel Configure step prompts
            # for at create time (#2934) — {key: dotted config path, label, type, required,
            # default?}, normalized above. Cached so the lock-only seed path
            # (`apply_bundle_config_inputs`) can anchor operator values to DECLARED keys.
            "config_inputs": config_inputs,
            # Archetype metadata (ADR 0042) cached here so the new-agent picker can offer
            # this bundle as a starter type without re-reading its manifest. Unknown keys
            # are cached but never read — warn (not fail) so a typo'd field (`souls:`,
            # `require_tools:`) surfaces at install instead of vanishing silently (#2715).
            "archetype": _inst()._checked_archetype_block(bid, bundle.get("archetype")),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "by": by,
        }
    )
    _inst()._write_lock(lock)
    _inst()._audit(
        "install-bundle",
        {"url": bundle_url, "sha": bundle_sha, "id": bid},
        f"installed bundle {bid} ({len(installed)} plugin(s))",
    )
    _inst().log.info("[plugins] installed bundle %s@%s (%d plugins) from %s", bid, bundle_sha[:10], len(installed), bundle_url)
    summary = {
        "bundle": bid,
        "name": bundle.get("name", ""),
        "description": bundle.get("description", ""),
        "resolved_sha": bundle_sha,
        "installed": installed,
        "skipped_builtin": skipped,
        # Members listed by a URL a bundled plugin supersedes — skipped like builtins.
        "skipped_superseded": superseded,
        "enabled": list(bundle.get("enabled") or []),
        "config": bundle.get("config") or {},
        "config_inputs": config_inputs,
    }
    if bundle_warnings:
        summary["warnings"] = bundle_warnings
    return summary


# ── Bundle-level lifecycle (ADR 0049 D4, #2718) ────────────────────────────────
# A bundle was first-class at install and never again: `check_updates`/`sync` read
# lock["plugins"] only, and uninstall had no bundle notion — so a published archetype repo's
# pin never moved on an installed host, and removing one meant hand-uninstalling
# members against a provenance row that slowly went stale.


def bundle_entry(bundle_id: str) -> dict | None:
    """The ``lock["bundles"]`` row for ``bundle_id`` (None when not installed)."""
    return next((b for b in _inst()._read_lock().get("bundles") or [] if b.get("id") == bundle_id), None)


def _bundle_ownership(bundle_id: str) -> tuple[dict | None, set[str], dict[str, str]]:
    """The shared ownership scan (extracted per the 2732 review): the bundle's lock
    row, the member ids every OTHER bundle row lists, and each installed plugin's
    ``by`` provenance. The rule everywhere: a member is exclusively this bundle's
    iff it still carries ``by == bundle:<id>`` and no other row lists it."""
    lock = _inst()._read_lock()
    row = next((b for b in lock.get("bundles") or [] if b.get("id") == bundle_id), None)
    listed_elsewhere: set[str] = set()
    for b in lock.get("bundles") or []:
        if b.get("id") != bundle_id:
            listed_elsewhere.update(str(p) for p in b.get("plugins") or [])
    by_of = {str(e.get("id")): e.get("by", "") for e in lock.get("plugins") or []}
    return row, listed_elsewhere, by_of


def _exclusively_owned(pid: str, bundle_id: str, listed_elsewhere: set[str], by_of: dict[str, str]) -> bool:
    return pid not in listed_elsewhere and by_of.get(pid, "") == f"bundle:{bundle_id}"


def orphaned_bundle_members(bundle_id: str, before_members: list[str]) -> list[str]:
    """After a bundle update rewrote its lock row: the members the NEW manifest
    dropped that are still exclusively this bundle's (by-provenance, not listed by
    any other bundle). The update path uninstalls these so a manifest that removed a
    member doesn't leave it installed forever with dangling provenance."""
    row, listed_elsewhere, by_of = _inst()._bundle_ownership(bundle_id)
    current = {str(p) for p in (row.get("plugins") or [])} if row else set()
    return [
        str(pid)
        for pid in before_members
        if pid not in current and _inst()._exclusively_owned(str(pid), bundle_id, listed_elsewhere, by_of)
    ]


def uninstall_bundle(bundle_id: str, *, purge: bool = False) -> dict:
    """Remove a bundle: uninstall its exclusively-owned members and drop the lock
    row. Shared members (listed by another bundle) and members whose ``by``
    provenance moved (re-installed directly) are kept; a member already gone from
    disk+lock (uninstalled individually — the provenance row still listed it) is
    skipped, not an error. ``purge`` forwards to each member uninstall (config +
    secrets removal)."""
    row, listed_elsewhere, by_of = _inst()._bundle_ownership(bundle_id)
    if row is None:
        raise _inst().BundleNotInstalledError(f"bundle {bundle_id!r} is not installed.")
    # Bucket every row member honestly (the 2732 review's bucketing finding: a member
    # uninstalled individually earlier — the stale-provenance case this function
    # exists for — landed in "kept" as if it were still installed and shared):
    #   no lock entry at all  → already gone         → skipped_missing
    #   exclusively ours      → uninstall            → removed_members (or failed{pid: why})
    #   anything else         → shared / re-owned    → kept
    removed_members: list[str] = []
    superseded: list[str] = []
    superseded_was_loaded: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    kept: list[str] = []
    for raw in row.get("plugins") or []:
        pid = str(raw)
        if pid not in by_of:
            skipped.append(pid)  # provenance row outlived the member — nothing to remove
        elif _inst()._exclusively_owned(pid, bundle_id, listed_elsewhere, by_of):
            try:
                report = _inst().uninstall(pid, purge=purge)
                if isinstance(report, dict) and report.get("superseded_by_bundled"):
                    # Only the member's IGNORED copy went — the plugin now ships with
                    # protoAgent and its bundled copy keeps running (still enabled). Not a
                    # removed member: callers must not unload it or report it gone.
                    superseded.append(pid)
                    if report.get("was_loaded"):
                        superseded_was_loaded.append(pid)
                else:
                    removed_members.append(pid)
            except _inst().InstallError as exc:
                # An uninstall that RAISED is not "already gone" — reporting it in
                # skipped_missing mislabeled a real failure (2740 review nit).
                failed[pid] = str(exc)
        else:
            kept.append(pid)
    # Re-read — each member uninstall rewrote the lock — then drop the row itself.
    lock = _inst()._read_lock()
    lock["bundles"] = [b for b in lock.get("bundles") or [] if b.get("id") != bundle_id]
    _inst()._write_lock(lock)
    _inst()._audit(
        "uninstall-bundle",
        {"id": bundle_id, "purge": purge},
        f"uninstalled bundle {bundle_id} ({len(removed_members)} member(s), {len(kept)} kept)",
    )
    _inst().log.info(
        "[plugins] uninstalled bundle %s — removed: %s; kept (shared/re-owned): %s; superseded copies removed "
        "(bundled copy keeps running): %s",
        bundle_id,
        ", ".join(removed_members) or "none",
        ", ".join(kept) or "none",
        ", ".join(superseded) or "none",
    )
    return {
        "id": bundle_id,
        "removed_members": removed_members,
        "skipped_missing": skipped,
        # Members whose uninstall RAISED (a race, a refusal) — distinct from
        # skipped_missing so callers never label a failure "already gone".
        "failed": failed,
        "kept": kept,
        # Members that moved into core: their ignored git copy was removed, the bundled
        # copy keeps running. `superseded_was_loaded` is the subset this process was still
        # running from the removed copy (upgraded under a live server) — unload those.
        "superseded": superseded,
        "superseded_was_loaded": superseded_was_loaded,
        "purged": purge,
    }
