#!/usr/bin/env python3
"""Reconcile the `team-ready` label against open PRs that already claim the issue (#2278).

`team-ready` means "triaged, ready for an agent to pick up" — it is the only intake gate
the board pipeline accepts. It knows nothing about pull requests, so an issue whose work
is already in flight keeps advertising itself as available. An agent told to "pick up the
next team-ready issue" boards it, dispatches a coder, and burns a full run on work that is
already done or in review. Both observed cases were exactly this: protoAgent#2266 (open PR
#2269 said `Closes #2266`) and protoAgent#1986 (shipped in merged PR #2116, issue never
closed, label never removed).

The rule: an open PR carrying `Closes #N` / `Fixes #N` / `Resolves #N` **claims** issue N.
A claimed issue swaps `team-ready` for `claimed-by-pr`; when the claim goes away (the PR
merged and the issue is closed, or the PR was abandoned / the reference edited out) the
swap is reversed.

`claimed-by-pr` doubles as the bookkeeping marker: it is added *only* where this script
removed `team-ready`, so releasing a claim restores exactly the issues it took the label
from and never invents `team-ready` on an issue that never had it.

Read-only unless it has something to change. `DRY_RUN=1` prints the plan and writes
nothing. Stdlib + `gh` only — no dependency install in CI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

READY = "team-ready"
CLAIMED = "claimed-by-pr"

# GitHub's own closing keywords, in the two forms a PR body actually uses: `#123` and a
# full issue URL. Deliberately NOT matching bare "#123" without a keyword — a PR that
# merely *mentions* an issue ("related to #123", "see #123") is not claiming it, and
# treating a mention as a claim would silently strand issues nobody is working on.
_CLOSES = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+"
    r"(?:#(?P<num>\d+)|https?://github\.com/[^/\s]+/[^/\s]+/issues/(?P<url_num>\d+))"
)


def claimed_numbers(body: str) -> set[int]:
    """Issue numbers a PR body claims to close."""
    out: set[int] = set()
    for m in _CLOSES.finditer(body or ""):
        num = m.group("num") or m.group("url_num")
        if num:
            out.add(int(num))
    return out


def reconcile(issues: list[dict], prs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pure planner: ``(to_claim, to_release)``.

    ``issues`` — open issues, each ``{"number", "labels": [name, …]}``.
    ``prs``    — open PRs, each ``{"number", "body"}``.
    """
    claims: dict[int, list[int]] = {}
    for pr in prs:
        for n in claimed_numbers(pr.get("body") or ""):
            claims.setdefault(n, []).append(int(pr["number"]))

    to_claim, to_release = [], []
    for issue in issues:
        num = int(issue["number"])
        labels = set(issue.get("labels") or [])
        by = sorted(claims.get(num, []))
        if by and READY in labels:
            to_claim.append({"number": num, "prs": by})
        elif not by and CLAIMED in labels:
            # The claim is gone (PR closed, or the reference edited out) and the issue is
            # still open — hand it back to intake rather than leaving it un-pickable.
            to_release.append({"number": num})
    return to_claim, to_release


# ── gh plumbing ───────────────────────────────────────────────────────────────
def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fetch_issues() -> list[dict]:
    """Open issues carrying either label. Both are needed: `team-ready` to find new
    claims, `claimed-by-pr` to find claims to release."""
    seen: dict[int, dict] = {}
    for label in (READY, CLAIMED):
        raw = _gh("issue", "list", "--state", "open", "--limit", "500", "--label", label, "--json", "number,labels")
        for it in json.loads(raw or "[]"):
            seen[int(it["number"])] = {
                "number": int(it["number"]),
                "labels": [lbl["name"] for lbl in it.get("labels", [])],
            }
    return list(seen.values())


def fetch_prs() -> list[dict]:
    raw = _gh("pr", "list", "--state", "open", "--limit", "500", "--json", "number,body")
    return json.loads(raw or "[]")


def ensure_label() -> None:
    """Create `claimed-by-pr` if the repo doesn't have it yet (idempotent)."""
    subprocess.run(
        ["gh", "label", "create", CLAIMED, "--color", "FBCA04",
         "--description", "Work is already claimed by an open PR — not available for intake"],
        capture_output=True, text=True,
    )


def main() -> int:
    dry = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    to_claim, to_release = reconcile(fetch_issues(), fetch_prs())

    if not to_claim and not to_release:
        print("team-ready is consistent with open PR claims — nothing to do.")
        return 0
    if to_claim and not dry:
        ensure_label()

    for c in to_claim:
        prs = ", ".join(f"#{p}" for p in c["prs"])
        print(f"claim   #{c['number']}: {READY} -> {CLAIMED} (claimed by {prs})")
        if not dry:
            _gh("issue", "edit", str(c["number"]), "--remove-label", READY, "--add-label", CLAIMED)
    for r in to_release:
        print(f"release #{r['number']}: {CLAIMED} -> {READY} (no open PR claims it)")
        if not dry:
            _gh("issue", "edit", str(r["number"]), "--remove-label", CLAIMED, "--add-label", READY)

    print(f"\n{len(to_claim)} claimed, {len(to_release)} released{' (DRY RUN — nothing written)' if dry else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
