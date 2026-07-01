#!/usr/bin/env python3
"""Derive the marketing site's /roadmap data from ROADMAP.md.

ROADMAP.md at the repo root is the human-owned source of truth: status sections
(``## Planned`` / ``## In progress`` / ``## Shipped``) each holding a bullet list of
items — a **bold title**, an em-dash one-line detail, and an optional ``(#issue)`` or
``(vX.Y.Z)`` reference. This mirrors the CHANGELOG.md → changelog.json pipeline
(see scripts/changelog.py): the markdown is the thing you edit; the JSON is derived so
the Astro page stays a dumb renderer.

    python scripts/roadmap.py build     # ROADMAP.md → sites/marketing/data/roadmap.json
    python scripts/roadmap.py check     # fail if roadmap.json is stale (CI guard)

Unlike changelog.json (a *curated* subset), roadmap.json is a faithful projection of
ROADMAP.md — so ``build`` fully rewrites it and ``check`` fails when it drifts.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROADMAP = Path(__file__).parent.parent / "ROADMAP.md"
MARKETING_JSON = Path(__file__).parent.parent / "sites" / "marketing" / "data" / "roadmap.json"

# Issue / release references carried in a trailing ``(...)`` — e.g. ``(#1520)`` or
# ``(v0.78.0)``. The Astro page turns ``#N`` into an issue link and ``vX.Y.Z`` into a
# release-tag link.
_REF = r"#\d+|v\d+\.\d+\.\d+"


def _strip_md(s: str) -> str:
    """Markdown → plain text (the roadmap page renders plain text); drop ADR refs."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)  # **bold** → bold
    s = re.sub(r"`([^`]+)`", r"\1", s)  # `code` → code
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url) → text
    s = re.sub(r"\s*\(ADR[^)]*\)", "", s)  # drop "(ADR 0026)"
    return re.sub(r"\s+", " ", s).strip()


def _item(raw: str) -> dict[str, object]:
    """Parse one folded bullet into ``{title, detail, refs}``.

    ``- **Title** — detail sentence. (#1520)`` → title/detail split on the bold lead
    (falling back to the first em-dash clause), with the trailing reference parenthetical
    peeled off into ``refs``.
    """
    refs = re.findall(_REF, raw)
    # Peel the trailing "(…#1520…)" / "(…v0.78.0…)" reference group off the detail.
    body = re.sub(rf"\s*\(([^)]*(?:{_REF})[^)]*)\)\s*$", "", raw).strip()

    bold = re.match(r"\*\*(.+?)\*\*\s*", body)
    if bold:
        title, detail = bold.group(1), body[bold.end() :]
    else:  # no bold lead → split on the first em-dash / hyphen separator
        parts = re.split(r"\s+[—–-]\s+", body, maxsplit=1)
        title, detail = parts[0], (parts[1] if len(parts) > 1 else "")

    detail = re.sub(r"^[—–-]\s*", "", detail).strip()
    return {"title": _strip_md(title), "detail": _strip_md(detail), "refs": refs}


def parse(text: str) -> list[dict[str, object]]:
    """ROADMAP.md → ``[{status, items: [{title, detail, refs}]}]`` in document order.

    Only ``## `` (level-2) headings open a status group; a ``# `` title and any intro
    prose above the first group are ignored. Empty groups are dropped.
    """
    groups: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    chunk: list[str] | None = None  # the current bullet's lines (lead + continuations)

    def flush() -> None:
        nonlocal chunk
        if current is not None and chunk:
            text = re.sub(r"\s+", " ", " ".join(chunk)).strip()
            current["items"].append(_item(text))  # type: ignore[attr-defined]
        chunk = None

    for line in text.splitlines():
        heading = re.match(r"^##\s+(.*\S)\s*$", line)
        if heading:
            flush()
            current = {"status": heading.group(1).strip(), "items": []}
            groups.append(current)
            continue
        bullet = re.match(r"^-\s+(.*)$", line)
        if bullet:
            flush()
            chunk = [bullet.group(1)]
        elif chunk is not None:
            s = line.strip()
            if not s or s.startswith("- ") or line.startswith("#"):
                flush()  # blank / next bullet / heading ends the current bullet
            else:
                chunk.append(s)  # an indented continuation line
    flush()
    return [g for g in groups if g["items"]]


def render(text: str) -> str:
    """ROADMAP.md text → the exact JSON string written to roadmap.json."""
    return json.dumps(parse(text), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive roadmap.json from ROADMAP.md")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="parse ROADMAP.md → sites/marketing/data/roadmap.json")
    sub.add_parser("check", help="fail if roadmap.json is out of date vs ROADMAP.md")
    args = parser.parse_args()

    out = render(ROADMAP.read_text(encoding="utf-8"))

    if args.cmd == "build":
        MARKETING_JSON.write_text(out, encoding="utf-8")
        n = sum(len(g["items"]) for g in parse(ROADMAP.read_text(encoding="utf-8")))
        print(f"roadmap: wrote {n} items to {MARKETING_JSON.name}")
    elif args.cmd == "check":
        current = MARKETING_JSON.read_text(encoding="utf-8") if MARKETING_JSON.exists() else ""
        if current != out:
            raise SystemExit(
                f"{MARKETING_JSON.name} is out of date — run `python scripts/roadmap.py build`"
            )
        print("roadmap: roadmap.json is in sync with ROADMAP.md")


if __name__ == "__main__":
    main()
