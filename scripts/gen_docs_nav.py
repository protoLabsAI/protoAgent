#!/usr/bin/env python3
"""Generate ``plugins/docs/nav.json`` — the Diátaxis→domain doc tree the Docs plugin's
reader shows — from the VitePress sidebar (``docs/.vitepress/config.mts``).

The domain grouping lives ONLY in the sidebar config; the markdown files carry no domain
metadata. We extract it at build/sync time into a committed JSON the plugin reads at runtime
(the frozen desktop app has no ``.vitepress/``). ``tests/test_docs_plugin.py`` asserts the
committed file stays in sync, so a sidebar edit that isn't regenerated fails CI.

    python scripts/gen_docs_nav.py            # write plugins/docs/nav.json
    python scripts/gen_docs_nav.py --check    # exit 1 if the committed file is stale

The parser is deliberately narrow (the sidebar is a regular object literal: double-quoted
values, only the bare keys text/link/items/collapsed, no inline comments) and never runs
the TS — it brace-matches the ``sidebar`` object, quotes those keys, drops trailing commas,
and ``json.loads`` it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "docs" / ".vitepress" / "config.mts"
OUT = REPO / "plugins" / "docs" / "nav.json"

# Sidebar route key → corpus section dir. ADRs aren't enumerated in the sidebar (just a
# link to /adr/), so they're listed from the filesystem below.
SECTION_ROUTE = {
    "/tutorials/": "tutorials",
    "/guides/": "guides",
    "/reference/": "reference",
    "/explanation/": "explanation",
}


def _load_corpus():
    """Load the plugin's corpus module standalone (it imports only stdlib)."""
    spec = importlib.util.spec_from_file_location("docs_corpus_gen", REPO / "plugins" / "docs" / "corpus.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _sidebar_object(src: str) -> str:
    """Brace-match the object literal after ``sidebar:`` (respecting string contents)."""
    j = src.index("{", src.index("sidebar:"))
    depth = 0
    in_str = esc = False
    for k in range(j, len(src)):
        c = src[k]
        if in_str:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[j : k + 1]
    raise ValueError("unbalanced sidebar object in config.mts")


def _parse_sidebar() -> dict:
    obj = _sidebar_object(CONFIG.read_text(encoding="utf-8"))
    obj = re.sub(r"\b(text|link|items|collapsed)\s*:", r'"\1":', obj)  # quote bare keys
    obj = re.sub(r",(\s*[}\]])", r"\1", obj)  # drop trailing commas
    return json.loads(obj)


def build_nav() -> dict:
    corpus = _load_corpus()
    valid = corpus.valid_paths()
    titles = {rel: corpus.doc_title(p) for rel, p in corpus.iter_docs()}
    sidebar = _parse_sidebar()

    nav: dict[str, list] = {}
    for route, section in SECTION_ROUTE.items():
        groups = []
        for grp in sidebar.get(route, []):
            items = []
            for it in grp.get("items", []):
                link = (it.get("link") or "").strip()
                # Only same-section doc links (skips Overview "/guides/" + cross-section refs).
                if not link.startswith("/" + section + "/") or link.rstrip("/") == "/" + section:
                    continue
                path = link.lstrip("/") + ".md"
                if path in valid:
                    items.append({"path": path, "title": it.get("text") or titles.get(path, path)})
            if items:
                groups.append({"label": grp.get("text", ""), "items": items})
        nav[section] = groups

    adrs = sorted(p for p in valid if p.startswith("adr/"))
    nav["adr"] = [{"label": "ADRs", "items": [{"path": p, "title": titles[p]} for p in adrs]}] if adrs else []
    return nav


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if plugins/docs/nav.json is stale")
    args = ap.parse_args()

    payload = json.dumps(build_nav(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != payload:
            sys.exit("plugins/docs/nav.json is stale — run `python scripts/gen_docs_nav.py`")
        print("nav.json is in sync with the sidebar")
        return
    OUT.write_text(payload, encoding="utf-8")
    n = sum(len(g["items"]) for groups in build_nav().values() for g in groups)
    print(f"wrote {OUT.relative_to(REPO)} ({n} docs)")


if __name__ == "__main__":
    main()
