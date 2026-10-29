"""Shared archetype catalog lookup (ADR 0042) — the graph-layer resolver.

The new-agent picker reads ``/api/archetypes`` (``operator_api/fleet_routes._archetypes``):
it turns the ``archetype-catalog.json`` entries + every installed bundle's ``archetype:``
block into the cards an operator picks, and the console then POSTs the chosen card's
``bundle`` / ``soul`` / ``requires_tools`` back to ``POST /api/fleet``.

A non-HTTP caller — the demo-agent core (``graph.fleet.demo``) — needs to resolve an
archetype the SAME way, from inside the ``graph`` layer, which can never import
``operator_api`` (the import-layering invariant). This module is that graph-importable twin:
:func:`resolve_archetype` takes an archetype id and returns the ``{bundle, ref, soul,
requires_tools}`` a member is created from, reading the catalog exactly as the picker does
(live config dir overrides the bundled seed) and falling back to the two code-free personas
so a broken/forked config still resolves ``basic``.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("protoagent.fleet.archetypes")


# The two code-free personas — the last-resort set if ``archetype-catalog.json`` is missing
# or unreadable, so ``basic`` always resolves even on a broken/forked config. Mirrors
# ``operator_api/fleet_routes._FALLBACK_ARCHETYPES`` (the console projection of the same data).
FALLBACK_ARCHETYPES: list[dict] = [
    {
        "id": "basic",
        "label": "Basic",
        "icon": "Sparkles",
        "bundle": None,
        "blurb": "A blank-slate agent — the core loop + built-in tools, no plugins.",
        "soul_preset": "base",
    },
    {
        "id": "custom",
        "label": "Custom",
        "icon": "PenLine",
        "bundle": None,
        "blurb": "Write your own — start from a SOUL template and fill it in.",
        "soul_preset": "blank",
    },
]


def _norm_url(u: str) -> str:
    """Normalize a bundle URL for comparison (drop ``.git`` / trailing slash, lowercase) —
    the same shape the plugin catalog matches install state by."""
    return re.sub(r"\.git$", "", (u or "").strip().rstrip("/")).lower()


def _read_catalog_doc() -> dict | None:
    """The winning ``archetype-catalog.json`` parsed — the live config dir overrides the
    bundled seed (a fork adds/removes archetypes with no code change), same lookup order as
    the console's ``_read_archetype_catalog_doc``. ``None`` when absent or malformed; the live
    dir wins even when broken (never silently falls through to the seed)."""
    import json

    from infra.paths import instance_paths

    ip = instance_paths()
    for base in (ip.config_dir, ip.bundle_dir):
        f = base / "archetype-catalog.json"
        if f.exists():
            try:
                doc = json.loads(f.read_text(encoding="utf-8")) or {}
                return doc if isinstance(doc, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                log.warning("[fleet] archetype-catalog.json unreadable at %s", f)
            return None
    return None


def load_catalog() -> list[dict]:
    """Built-in archetype entries from ``archetype-catalog.json``, or the code-free fallback
    when the file is absent/malformed, so resolution never comes up empty-handed."""
    entries = (_read_catalog_doc() or {}).get("archetypes")
    if isinstance(entries, list) and entries:
        return [e for e in entries if isinstance(e, dict)]
    return FALLBACK_ARCHETYPES


def _catalog_record(entry: dict, aid: str) -> dict:
    """A catalog entry shaped as a resolution record: the persona (inline ``soul`` or a named
    ``soul_preset`` resolved here) plus the bundle + capability contract a member is built
    from. Matches the fields ``create`` cares about."""
    from graph.config_io import read_soul_preset

    soul = entry.get("soul") or (read_soul_preset(str(entry["soul_preset"])) if entry.get("soul_preset") else "")
    return {
        "id": aid,
        "label": entry.get("label", aid),
        "bundle": entry.get("bundle") or None,
        # Catalog entries don't pin a git ref (the picker installs a bundle at its default
        # branch); kept here so a bundle entry that does carry one flows through.
        "ref": (str(entry.get("ref")).strip() or None) if entry.get("ref") else None,
        "soul": soul or "",
        "requires_tools": [str(t) for t in (entry.get("requires_tools") or []) if str(t).strip()],
    }


def _bundle_record(bid: str, url: str, arch: dict) -> dict:
    """An installed bundle's ``archetype:`` block shaped as a resolution record — the same
    persona pair (inline ``soul`` or a host ``soul_preset``) the catalog supports."""
    from graph.config_io import read_soul_preset

    soul = str(arch.get("soul") or "")
    if not soul and arch.get("soul_preset"):
        soul = read_soul_preset(str(arch["soul_preset"]))
    return {
        "id": bid,
        "label": arch.get("label") or bid,
        "bundle": url or None,
        "ref": (str(arch.get("ref")).strip() or None) if arch.get("ref") else None,
        "soul": soul or "",
        "requires_tools": [str(t) for t in (arch.get("requires_tools") or []) if str(t).strip()],
    }


def resolve_archetype(archetype_id: str) -> dict | None:
    """Resolve an archetype id to the ``{id, label, bundle, ref, soul, requires_tools}`` a
    member is created from — the catalog (+ code-free fallback) first, then every installed
    bundle's self-registered ``archetype:`` metadata. ``None`` for an unknown id.

    This is the server-side twin of what the console does by reading ``/api/archetypes`` and
    POSTing the picked card's fields: a graph-layer caller resolves the same record without
    an HTTP round-trip (and without importing ``operator_api``)."""
    aid = (archetype_id or "").strip()
    if not aid:
        return None
    for entry in load_catalog():
        if str(entry.get("id") or "").strip() == aid:
            return _catalog_record(entry, aid)
    # Installed bundles that declare `archetype:` metadata self-register as starter types
    # (same source the picker merges in); best-effort — a missing/malformed lock resolves none.
    try:
        from graph.plugins.installer import _read_lock

        for b in _read_lock().get("bundles") or []:
            if str(b.get("id") or "").strip() != aid:
                continue
            arch = b.get("archetype") or {}
            if arch.get("label"):
                return _bundle_record(aid, str(b.get("source_url") or ""), arch)
    except Exception:  # noqa: BLE001 — archetype discovery is best-effort, never fatal
        log.warning("[fleet] installed-bundle archetype discovery failed", exc_info=True)
    return None
