#!/usr/bin/env python3
"""PR gate: a changelog entry must not land under an ALREADY-DATED heading.

    changelog_placement.py <base-ref>      # e.g. origin/main

A PR branch cut before a release roll still carries `## [Unreleased]` as its diff
context. When the roll renames that heading to `## [X.Y.Z] - date` and the PR merges
afterwards, git's 3-way merge places the entry inside the now-CLOSED section. The
result is silent and wrong in two directions at once: the entry is attributed to a
release that shipped without it, and it never appears in any release's notes —
neither the one it's filed under (already published) nor the next one (it isn't
under [Unreleased]).

Two confirmed occurrences before this guard existed:

  d689b509  feat(settings): agent_runtime in the Model section  -> filed under
            [0.103.0], merged SIX DAYS after that tag.
  6ba6c4d0  feat(workflows): opt-in per-step timeout            -> filed under
            [0.115.0], merged after that tag; caught by hand while reading the
            v0.116.0 roll, and moved to [0.116.0].

Both were features. Neither was caught by the existing changelog gate, which only
asks whether CHANGELOG.md was touched at all — not WHERE.

**Counts, not line matching.** The check compares the number of entries under each
dated heading between base and head. Rewording an existing entry (a typo fix, a
clarification) moves lines but not counts, so it passes; only a net-new entry under
a dated heading fails. That keeps historical corrections — which this repo does
make — from tripping it.

**Release branches are exempt.** Rolling [Unreleased] into a dated section is
precisely what `prepare-release` does, so `release/*` and `prepare-release/*` head
refs skip the check (matching the sibling gate's escape hatch).

Pure git + python3 stdlib, no install — same posture as changelog_gate.sh.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# `## [1.2.3] - 2026-01-01` (dated/closed) vs `## [Unreleased]` (open). The VERSION is
# captured because it — not the whole heading — is the section's identity: correcting a
# heading's date would otherwise make the section look brand-new to the base/head
# comparison, and a misfiled entry added in the same commit would slip through.
_DATED = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
_HEADING = re.compile(r"^## \[")
_ENTRY = re.compile(r"^- \*\*")


def entries_by_dated_section(text: str) -> dict[str, int]:
    """{release version: number of `- **` entries beneath it}. Open sections skipped."""
    counts: dict[str, int] = {}
    current: str | None = None
    for line in text.splitlines():
        if _HEADING.match(line):
            m = _DATED.match(line)
            current = m.group(1) if m else None
            if current is not None:
                counts.setdefault(current, 0)
        elif current is not None and _ENTRY.match(line):
            counts[current] += 1
    return counts


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: changelog_placement.py <base-ref>", file=sys.stderr)
        return 2
    base = sys.argv[1]

    head_ref = os.environ.get("PR_HEAD_REF", "")
    if head_ref.startswith("release/") or head_ref.startswith("prepare-release/"):
        print(f"skip: release branch {head_ref!r} rolls [Unreleased] into a dated section by design")
        return 0

    # Resolve the base FIRST, and fail loudly if it doesn't. Folding "the ref is
    # unresolvable" into "there's no CHANGELOG there" would make this gate pass silently
    # whenever it couldn't actually run — a shallow clone without the base, a typo'd ref —
    # which is the same fail-open shape it exists to prevent. A guard that can't check
    # must say so, not wave the PR through.
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
                      capture_output=True).returncode != 0:
        print(
            f"::error::base ref {base!r} does not resolve — cannot compare changelog "
            "placement. (A shallow checkout? This job needs fetch-depth: 0.)"
        )
        return 1

    probe = subprocess.run(["git", "cat-file", "-e", f"{base}:CHANGELOG.md"], capture_output=True)
    if probe.returncode != 0:
        # The ref resolves, it just has no changelog — a genuinely new file. Nothing to
        # compare against, and no dated section can have grown.
        print(f"skip: no CHANGELOG.md at {base} — nothing to compare")
        return 0
    before = subprocess.run(
        ["git", "show", f"{base}:CHANGELOG.md"], capture_output=True, text=True, check=True
    ).stdout
    try:
        with open("CHANGELOG.md", encoding="utf-8") as fh:
            after = fh.read()
    except OSError as exc:
        print(f"::error::cannot read CHANGELOG.md: {exc}")
        return 1

    old, new = entries_by_dated_section(before), entries_by_dated_section(after)

    # Only sections that existed BEFORE can be "already dated" from this PR's point of
    # view. A dated heading that appears only in `new` was created by this PR — that's a
    # release roll, which the head-ref check above already exempts for real releases.
    grew = [(h, old[h], new[h]) for h in old if h in new and new[h] > old[h]]
    if not grew:
        print("ok: no new entries under an already-dated changelog heading")
        return 0

    for version, was, now in grew:
        print(
            f"::error::[{version}] gained {now - was} changelog entr"
            f"{'y' if now - was == 1 else 'ies'} ({was} -> {now}). That release has already "
            "shipped, so the entry would be credited to it AND missing from the next "
            "release's notes. Move it under '## [Unreleased]'. (Your branch was probably "
            "cut before a release roll renamed that heading.)"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
