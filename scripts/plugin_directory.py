#!/usr/bin/env python3
"""Derive every curated plugin-listing surface from config/plugin-directory.yaml.

The directory YAML is the human-owned source of truth: one entry per curated plugin.
Two files are derived from it (the same source→derived pattern as ROADMAP.md →
roadmap.json, see scripts/roadmap.py):

  config/plugin-catalog.json          — the in-app Discover catalog (GET /api/plugins/catalog,
                                        ADR 0059); schema unchanged: id/name/category/official/
                                        repo/tagline per entry
  sites/marketing/data/plugins.json   — the marketing plugins page's editorial overlay
                                        (sites/marketing/src/pages/plugins.astro merges it over
                                        the auto-discovered bundled + topic-scraped cards)

    python scripts/plugin_directory.py build     # directory YAML → both JSON files
    python scripts/plugin_directory.py check     # fail if either derived file is stale (CI guard)

Both outputs are faithful projections — ``build`` fully rewrites them and ``check``
(also enforced by tests/test_plugin_directory.py in the main suite) fails on drift.

The YAML is the full census of the org's plugins (#2910), and ``status`` decides where
each one is listed:

  active      Discover + the marketing page
  incubating  the marketing page only, badged — real and installable, not finished
  personal    listed nowhere — built for one operator's setup
  archived    listed nowhere — kept as a record
  deprecated / internal   listed nowhere (the older values, still accepted)

"Listed nowhere" has to cover the marketing page's auto-discovery too: it renders every
repo tagged ``protoagent-plugin``, so each unlisted entry emits a ``hidden`` marker into
the overlay and the page drops that repo's card.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
DIRECTORY = ROOT / "config" / "plugin-directory.yaml"
APP_CATALOG = ROOT / "config" / "plugin-catalog.json"
MARKETING_JSON = ROOT / "sites" / "marketing" / "data" / "plugins.json"

ORG = "https://github.com/protoLabsAI"
TREE = f"{ORG}/protoAgent/tree/main/plugins"

_APP_COMMENT = (
    "GENERATED from config/plugin-directory.yaml by scripts/plugin_directory.py — do not "
    "edit by hand; run `python scripts/plugin_directory.py build`. Curated official-plugin "
    "directory served by GET /api/plugins/catalog and rendered in the Plugins ▸ Discover "
    "section (ADR 0059). A fork can override it by placing its own plugin-catalog.json in "
    "the live config dir. `repo` is the install URL (one-click install runs `plugin install "
    "<repo>`, ADR 0058 — works on every surface incl. the frozen desktop app)."
)

_STATUSES = {"active", "incubating", "personal", "archived", "deprecated", "internal"}
# Where each status is listed. Discover installs with one click, so it carries finished
# plugins only; the marketing page may also show an incubating one, badged.
_APP_STATUSES = frozenset({"active"})
_SITE_STATUSES = frozenset({"active", "incubating"})


def _status(e: dict) -> str:
    return e.get("status", "active")


def load(path: Path = DIRECTORY) -> list[dict]:
    """Parse + validate the directory; returns EVERY entry — the census. Which surface
    lists which status is the renderers' call (``_APP_STATUSES`` / ``_SITE_STATUSES``)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("plugins") or []
    seen: set[str] = set()
    for e in entries:
        eid = e.get("id")
        if not eid or not e.get("name") or not e.get("category") or not e.get("tagline"):
            raise SystemExit(f"plugin-directory: entry {eid!r} is missing id/name/category/tagline")
        if eid in seen:
            raise SystemExit(f"plugin-directory: duplicate id {eid!r}")
        seen.add(eid)
        status = _status(e)
        if status not in _STATUSES:
            raise SystemExit(f"plugin-directory: {eid}: unknown status {status!r}")
        if bool(e.get("bundled")) == bool(e.get("repo")):
            raise SystemExit(f"plugin-directory: {eid}: exactly one of bundled/repo is required")
    return entries


def _source_url(e: dict) -> str:
    return f"{TREE}/{e['id']}" if e.get("bundled") else e["repo"]


def render_app(entries: list[dict]) -> str:
    """Active app entries → the exact plugin-catalog.json text (schema unchanged)."""
    plugins = [
        {
            "id": e["id"],
            "name": e["name"],
            "category": e["category"],
            "official": bool(e.get("official", True)),
            "repo": _source_url(e),
            "tagline": e["tagline"],
        }
        for e in entries
        if e.get("app", True) and _status(e) in _APP_STATUSES
    ]
    doc = {"_comment": _APP_COMMENT, "plugins": plugins}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def render_site(entries: list[dict]) -> str:
    """Site entries → the exact marketing plugins.json overlay text.

    The overlay is keyed by id: the plugins page folds an override onto its scraped
    card via ``<repo-name minus -plugin>`` (plugins.astro, #1772), so ``site_id``
    carries that key whenever it differs from the manifest id. That key is also what
    a ``hidden`` marker must match to drop the card.
    """
    out = []
    for e in entries:
        status = _status(e)
        if status not in _SITE_STATUSES:
            # Unlisted — but the page still auto-discovers every topic-tagged repo, so an
            # archived or personal one that keeps the topic would get a card anyway.
            out.append({"id": e.get("site_id") or e["id"], "status": status, "hidden": True})
            continue
        if not e.get("site", True):
            continue
        bundled = bool(e.get("bundled"))
        entry: dict = {
            "id": e.get("site_id") or e["id"],
            "name": e["name"],
            "category": e["category"],
            "official": bool(e.get("official", True)),
            "tagline": e["tagline"],
            "adds": list(e.get("adds") or []),
            "bundled": bundled,
        }
        if status != "active":
            entry["status"] = status  # the page badges it
        if bundled:
            # app:false rows are libraries/always-on builtins — an "enable X in
            # plugins.enabled" CTA is wrong or a no-op there (#2897 review). Explicit
            # null so the site merge overrides the auto-discovered CTA too.
            entry["enable"] = (e.get("enable") or e["id"]) if e.get("app", True) else None
        else:
            entry["install"] = e["repo"]
            if e.get("enable"):
                entry["enable"] = e["enable"]
        entry["links"] = {
            "source": _source_url(e),
            "docs": e.get("docs") or ("/docs/guides/plugins" if bundled else f"{e['repo']}#readme"),
        }
        out.append(entry)
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive the plugin catalogs from plugin-directory.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="directory YAML → plugin-catalog.json + marketing plugins.json")
    sub.add_parser("check", help="fail if either derived file is out of date (CI guard)")
    args = parser.parse_args()

    entries = load()
    outputs = {APP_CATALOG: render_app(entries), MARKETING_JSON: render_site(entries)}

    if args.cmd == "build":
        for path, text in outputs.items():
            path.write_text(text, encoding="utf-8")
            print(f"plugin-directory: wrote {path.relative_to(ROOT)}")
    elif args.cmd == "check":
        stale = [
            str(path.relative_to(ROOT))
            for path, text in outputs.items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != text
        ]
        if stale:
            raise SystemExit(
                f"stale derived catalogs: {', '.join(stale)} — run `python scripts/plugin_directory.py build`"
            )
        print("plugin-directory: derived catalogs are in sync")


if __name__ == "__main__":
    main()
