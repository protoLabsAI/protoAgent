"""cowork — the knowledge-work skill pack behind protoAgent's Cowork archetype.

Skills plus one goal/watch verifier (ADR 0083 D3: original clean-room document
skills — Anthropic's Cowork skills are all-rights-reserved and must never be
vendored here). Host-only imports stay lazy: ``graph.goals.types`` is imported
inside the verifier so this module keeps importing host-free.

Bundled into core (#3450) from the retired ``cowork-plugin`` repo, which the
manifest's ``supersedes`` names. The lazy import stays lazy even in-tree: it is
what lets ``tests/test_cowork_plugin.py`` drive ``register()`` and the verifier
against a fake registry with no host wired up.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.cowork")

# folder_changed scan bounds: enough for any sane drop folder, cheap enough to
# poll. Past the cap the count gains a "+" so evidence still moves on growth.
_SCAN_CAP = 5000
_EVIDENCE_LINES = 50


def _fenced_roots(config) -> list:
    """Resolved roots of the operator's fenced work folders, from the host config.

    ``effective_filesystem_projects`` is the real fence (explicit projects, the
    ADR 0095 registry projection, or the workspace default); fall back to the
    raw field for older hosts."""
    from pathlib import Path

    try:
        entries = config.effective_filesystem_projects()
    except Exception:  # noqa: BLE001 — older host or no config: raw field or nothing
        entries = getattr(config, "filesystem_projects", None)
    roots = []
    for e in entries or []:
        p = e.get("path") if isinstance(e, dict) else None
        if not p:
            continue
        try:
            roots.append(Path(str(p)).expanduser().resolve())
        except OSError:
            continue
    return roots


async def _folder_changed(spec: dict, ctx):
    """``{"type":"plugin","check":"cowork:folder_changed","args":{…}}`` verifier.

    args: ``path`` (required — a folder inside the fenced work folders),
    ``glob`` (default ``*``), ``recursive`` (default false).

    met = at least one matching file exists. evidence = a newest-first digest of
    (relative name, mtime, size), so an ``on_change`` watch fires on arrivals,
    edits, AND deletions. Refuses any path outside the fence: a watch must not
    widen what the operator consented to in Settings ▸ Tools.
    """
    from pathlib import Path

    from graph.goals.types import VerifyResult

    args = (spec or {}).get("args") or {}
    raw = str(args.get("path") or "").strip()
    pattern = str(args.get("glob") or "*").strip() or "*"
    recursive = bool(args.get("recursive"))
    if not raw:
        return VerifyResult(False, "args.path is required (a folder inside the fenced work folders)", "")
    if pattern.startswith(("/", "~")) or ".." in pattern:
        return VerifyResult(False, f"glob {pattern!r} must be relative to the watched folder", "")

    target = Path(raw).expanduser().resolve()
    fence = _fenced_roots(getattr(ctx, "config", None))
    if not fence:
        return VerifyResult(False, "no fenced work folders configured (Settings ▸ Tools)", "")
    if not any(target == root or root in target.parents for root in fence):
        return VerifyResult(False, f"{target} is outside the fenced work folders", "")
    if not target.is_dir():
        return VerifyResult(False, f"{target} is not a directory", "")

    globber = target.rglob if recursive else target.glob
    rows: list[tuple[str, int, int]] = []
    truncated = False
    for f in globber(pattern):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        rows.append((str(f.relative_to(target)), int(st.st_mtime), st.st_size))
        if len(rows) >= _SCAN_CAP:
            truncated = True
            break
    rows.sort(key=lambda r: (-r[1], r[0]))
    plus = "+" if truncated else ""
    lines = [f"{name} {mtime} {size}" for name, mtime, size in rows[:_EVIDENCE_LINES]]
    if truncated or len(rows) > _EVIDENCE_LINES:
        lines.append(f"… {len(rows)}{plus} total")
    reason = f"{len(rows)}{plus} file(s) match {pattern!r} in {target.name}"
    return VerifyResult(bool(rows), reason, "\n".join(lines))


def register(registry) -> None:
    try:
        registry.register_skill_dir("skills")
    except Exception:  # noqa: BLE001
        log.exception("[cowork] registering skills failed")

    try:
        registry.register_goal_verifier(
            "cowork:folder_changed",
            _folder_changed,
            "Files in a fenced work folder — drop-folder watches (evidence moves on add/edit/delete)",
        )
    except AttributeError:
        log.info("[cowork] host predates goal verifiers — folder_changed not registered")
    except Exception:  # noqa: BLE001
        log.exception("[cowork] registering folder_changed verifier failed")

    log.info("[cowork] registered")
