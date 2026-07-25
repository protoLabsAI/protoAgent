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
Entries with ``status: deprecated`` or ``status: internal`` are kept in the YAML as a
record but emitted nowhere — pulling a plugin from every surface is a one-line flip.
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

_STATUSES = {"active", "deprecated", "internal"}


def load(path: Path = DIRECTORY) -> list[dict]:
    """Parse + validate the directory; returns only the ACTIVE entries."""
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
        status = e.get("status", "active")
        if status not in _STATUSES:
            raise SystemExit(f"plugin-directory: {eid}: unknown status {status!r}")
        if bool(e.get("bundled")) == bool(e.get("repo")):
            raise SystemExit(f"plugin-directory: {eid}: exactly one of bundled/repo is required")
    return [e for e in entries if e.get("status", "active") == "active"]


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
        if e.get("app", True)
    ]
    doc = {"_comment": _APP_COMMENT, "plugins": plugins}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def render_site(entries: list[dict]) -> str:
    """Active site entries → the exact marketing plugins.json overlay text.

    The overlay is keyed by id: the plugins page folds an override onto its scraped
    card via ``<repo-name minus -plugin>`` (plugins.astro, #1772), so ``site_id``
    carries that key whenever it differs from the manifest id.
    """
    out = []
    for e in entries:
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
        if bundled:
            entry["enable"] = e.get("enable") or e["id"]
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
