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
# is **curated** (clean, user-facing blurbs — a different audience than CHANGELOG.md's
# detailed dev notes). It is NOT auto-derived from CHANGELOG.md (that produced verbose,
# jargon-y entries). Instead: `scaffold` drafts a *concise* entry per new release (bullet
# titles only) for a human to polish, and `missing_versions` guards against staleness.
MARKETING_JSON = Path(__file__).parent.parent / "sites" / "marketing" / "data" / "changelog.json"

_UNRELEASED_HEADING = "## [Unreleased]"


def _strip_md(s: str) -> str:
    """Markdown → plain text (the marketing page renders plain text); drop ADR refs."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)  # **bold** → bold
    s = re.sub(r"`([^`]+)`", r"\1", s)  # `code` → code
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url) → text
    s = re.sub(r"\s*\(ADR[^)]*\)", "", s)  # drop "(ADR 0026)"
    return re.sub(r"\s+", " ", s).strip()


def _section(text: str, version: str) -> tuple[str | None, str]:
    """(date, body) for ``## [version] - DATE`` — (None, "") if absent."""
    m = re.search(rf"^## \[{re.escape(version)}\] - (\S+)[ \t]*$", text, re.MULTILINE)
    if not m:
        return None, ""
    start = m.end()
    nxt = re.search(r"^## \[", text[start:], re.MULTILINE)
    return m.group(1), (text[start : start + nxt.start()] if nxt else text[start:])


def _titles(body: str) -> list[str]:
    """Concise draft summaries: each top-level bullet's **bold lead** (or first clause).

    A bullet's continuation lines are folded in before extraction, so a bold lead that
    wraps across lines (``- **A long lead\\n  that wraps.** rest``) is captured whole
    instead of leaving a stray ``**`` (the v0.47/v0.53 marketing-changelog glitch).
    """
    out: list[str] = []
    chunk: list[str] | None = None  # the current top-level bullet's lines

    def flush() -> None:
        if not chunk:
            return
        text = re.sub(r"\s+", " ", " ".join(chunk)).strip()
        bold = re.match(r"\*\*(.+?)\*\*", text)
        title = bold.group(1) if bold else re.split(r"\s+[—-]\s+|\. ", text, 1)[0]
        out.append(_strip_md(title))

    for line in body.splitlines():
        m = re.match(r"^- +(.*)$", line)  # a new TOP-LEVEL bullet (no leading indent)
        if m:
            flush()
            chunk = [m.group(1)]
        elif chunk is not None:
            s = line.strip()
            if not s or s.startswith("- ") or line.startswith("#"):
                flush()  # blank / nested bullet / heading → the bullet (and its lead) ends
                chunk = None
            else:
                chunk.append(s)  # an indented continuation line of the current bullet
    flush()
    return out


def dated_versions(text: str) -> list[str]:
    """All released versions (``X.Y.Z``) with a dated section in CHANGELOG.md."""
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\] - ", text, re.MULTILINE)


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("v").split("."))


def missing_versions() -> list[str]:
    """Released versions inside the curated range with content but no changelog.json entry.

    The marketing changelog only goes back to a curated floor (~v0.16), so we don't
    demand ancient history — we catch a *forgotten new release* (the original
    'stuck at 0.21' bug): any version at or above the oldest curated entry that's
    missing. An **empty** release (no ``[Unreleased]`` bullets → an empty section) is
    intentionally omitted from /changelog, so it's not flagged as missing.
    """
    have = {e["version"] for e in json.loads(MARKETING_JSON.read_text(encoding="utf-8"))}
    if not have:
        return []
    floor = min(_vtuple(v) for v in have)
    text = CHANGELOG.read_text(encoding="utf-8")
    return [
        f"v{v}"
        for v in dated_versions(text)
        if _vtuple(v) >= floor and f"v{v}" not in have and _titles(_section(text, v)[1])
    ]


def scaffold(version: str) -> bool:
    """Prepend a CONCISE draft entry (bullet titles) for *version* to changelog.json IF
    absent. Preserves existing curated entries (idempotent); returns True if it added one.
    A human polishes the wording — this just guarantees no version is missing."""
    entries = json.loads(MARKETING_JSON.read_text(encoding="utf-8"))
    vtag = f"v{version}"
    if any(e["version"] == vtag for e in entries):
        return False
    date, body = _section(CHANGELOG.read_text(encoding="utf-8"), version)
    if date is None:
        raise ValueError(f"no '## [{version}]' section in CHANGELOG.md")
    changes = _titles(body)
    if not changes:
        # Empty release (the PRs added no `[Unreleased]` bullets) → omit it from the
        # marketing changelog rather than emit a bare version+date line. Add a bullet
        # under `[Unreleased]` in your PR for the release to appear on /changelog.
        return False
    entries.insert(0, {"version": vtag, "date": date, "changes": changes})
    MARKETING_JSON.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def notes(version: str) -> str:
    """Return the CHANGELOG.md section body for *version* (markdown), or '' if absent/empty.

    Fed to the desktop updater-manifest job so the in-app UpdateNotice shows the curated
    CHANGELOG section instead of raw commit subjects. Empty output is the caller's signal
    to fall back (GitHub release body → placeholder) — e.g. a patch with no bullets.
    """
    _date, body = _section(CHANGELOG.read_text(encoding="utf-8"), version)
    return body.strip()


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
    p_sc = sub.add_parser(
        "scaffold",
        help="prepend a concise DRAFT changelog.json entry for a version (curated content stays)",
    )
    p_sc.add_argument("version", help="version released, e.g. 0.26.0")
    p_notes = sub.add_parser(
        "notes",
        help="print the CHANGELOG.md section body for a version (the desktop in-app updater notes)",
    )
    p_notes.add_argument("version", help="version released, e.g. 0.60.0")
    sub.add_parser("check", help="fail if any released version is missing from changelog.json")
    args = parser.parse_args()

    if args.cmd == "roll":
        text = CHANGELOG.read_text(encoding="utf-8")
        CHANGELOG.write_text(roll(text, args.version, args.date), encoding="utf-8")
        print(f"changelog: rolled Unreleased → [{args.version}] - {args.date}")
    elif args.cmd == "scaffold":
        added = scaffold(args.version)
        print(
            f"changelog: {'scaffolded draft' if added else 'already present'} v{args.version}"
            f" in {MARKETING_JSON.name} (polish the wording by hand)"
        )
    elif args.cmd == "notes":
        body = notes(args.version)
        if body:  # empty → print nothing so the caller's fallback chain kicks in
            print(body)
    elif args.cmd == "check":
        missing = missing_versions()
        if missing:
            raise SystemExit(
                f"changelog.json is missing entries for: {', '.join(missing)} "
                f"(run `scripts/changelog.py scaffold <version>` then polish)"
            )
        print("changelog: marketing changelog.json covers every released version")


if __name__ == "__main__":
    main()
