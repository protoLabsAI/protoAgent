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
import re
from pathlib import Path

CHANGELOG = Path(__file__).parent.parent / "CHANGELOG.md"

_UNRELEASED_HEADING = "## [Unreleased]"


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
    args = parser.parse_args()

    if args.cmd == "roll":
        text = CHANGELOG.read_text(encoding="utf-8")
        CHANGELOG.write_text(roll(text, args.version, args.date), encoding="utf-8")
        print(f"changelog: rolled Unreleased → [{args.version}] - {args.date}")


if __name__ == "__main__":
    main()
