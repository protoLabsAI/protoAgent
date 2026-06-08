#!/usr/bin/env python3
"""Maintain CHANGELOG.md (Keep a Changelog format).

The release-prep workflow (.github/workflows/prepare-release.yml) calls:

    python scripts/changelog.py roll 0.4.0            # date = today (UTC)
    python scripts/changelog.py roll 0.4.0 --date 2026-06-01

which moves everything under ``## [Unreleased]`` into a new dated
``## [0.4.0] - YYYY-MM-DD`` section and leaves a fresh, empty Unreleased
block at the top. The rolled file is committed as part of the
``chore: release vX.Y.Z`` PR, so the changelog goes through the same
branch ruleset (PR + checks) as any other change — nothing pushes to
``main`` directly.

Contributors add their entries under ``## [Unreleased]`` in their feature
PRs (``### Added`` / ``### Changed`` / ``### Fixed`` / ``### Docs`` / …).
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

CHANGELOG = Path(__file__).parent.parent / "CHANGELOG.md"
# The marketing site's /changelog page reads this — DERIVED from CHANGELOG.md so it
# can't drift (it used to be hand-maintained and went stale at v0.21).
MARKETING_JSON = (
    Path(__file__).parent.parent / "sites" / "marketing" / "data" / "changelog.json"
)

_UNRELEASED_HEADING = "## [Unreleased]"


def _strip_md(s: str) -> str:
    """Bullet text → plain text (the marketing page renders it as plain text)."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)            # **bold** → bold
    s = re.sub(r"`([^`]+)`", r"\1", s)                # `code` → code
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)    # [text](url) → text
    return re.sub(r"\s+", " ", s).strip()


def to_entries(text: str) -> list[dict]:
    """Parse dated ``## [X.Y.Z] - DATE`` sections into marketing-changelog entries
    (newest-first, matching CHANGELOG.md order). Skips ``[Unreleased]``."""
    entries: list[dict] = []
    for m in re.finditer(r"^## \[(\d+\.\d+\.\d+)\] - (\S+)[ \t]*$", text, re.MULTILINE):
        version, date = m.group(1), m.group(2)
        start = m.end()
        nxt = re.search(r"^## \[", text[start:], re.MULTILINE)
        body = text[start: start + nxt.start()] if nxt else text[start:]
        changes: list[str] = []
        cur: str | None = None
        for line in body.splitlines():
            if re.match(r"^\s*-\s+", line):
                if cur:
                    changes.append(_strip_md(cur))
                cur = re.sub(r"^\s*-\s+", "", line)
            elif line.lstrip().startswith("#"):
                if cur:
                    changes.append(_strip_md(cur))
                cur = None
            elif cur is not None and line.strip():
                cur += " " + line.strip()  # continuation of the current bullet
        if cur:
            changes.append(_strip_md(cur))
        entries.append({"version": f"v{version}", "date": date, "changes": changes})
    return entries


def roll(text: str, version: str, date: str) -> str:
    """Return *text* with the Unreleased section promoted to ``[version] - date``.

    Raises ``ValueError`` if there's no Unreleased heading.
    """
    m = re.search(r"^## \[Unreleased\][ \t]*\n", text, re.MULTILINE)
    if not m:
        raise ValueError("no '## [Unreleased]' section in CHANGELOG.md")

    start = m.end()
    # The Unreleased body runs until the next version heading (or EOF / footer).
    nxt = re.search(r"^## \[", text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end].strip("\n")

    section = f"## [{version}] - {date}\n"
    if body:
        section += f"\n{body}\n"

    before = text[: m.start()]
    after = text[end:]
    rebuilt = f"{before}{_UNRELEASED_HEADING}\n\n{section}\n{after}"
    # Normalize runs of blank lines introduced by the splice.
    return re.sub(r"\n{3,}", "\n\n", rebuilt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain CHANGELOG.md")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_roll = sub.add_parser("roll", help="promote Unreleased to a dated version section")
    p_roll.add_argument("version", help="version being released, e.g. 0.4.0")
    p_roll.add_argument(
        "--date",
        default=datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        help="release date (YYYY-MM-DD); defaults to today (UTC)",
    )
    sub.add_parser("json", help="regenerate the marketing changelog.json from CHANGELOG.md")
    args = parser.parse_args()

    if args.cmd == "roll":
        text = CHANGELOG.read_text(encoding="utf-8")
        CHANGELOG.write_text(roll(text, args.version, args.date), encoding="utf-8")
        print(f"changelog: rolled Unreleased → [{args.version}] - {args.date}")
    elif args.cmd == "json":
        entries = to_entries(CHANGELOG.read_text(encoding="utf-8"))
        MARKETING_JSON.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"changelog: wrote {len(entries)} entries → {MARKETING_JSON}")


if __name__ == "__main__":
    main()
