#!/usr/bin/env python3
"""Flag marketing-roadmap refs that point at closed issues (CI staleness guard, #1945).

``sites/marketing/data/roadmap.json`` is derived from ROADMAP.md (scripts/roadmap.py) and
rendered on the public /roadmap page — and it rots silently: before the #1944 refresh,
7 of its 8 Planned/In-progress refs pointed at issues that had closed weeks earlier. This
guard queries the GitHub state of every ``#NNNN`` ref under a **Planned** or **In progress**
section and fails when one is CLOSED — i.e. the public page still advertises shipped work
as pending.

    python scripts/check_roadmap_staleness.py                 # auth via GH_TOKEN/GITHUB_TOKEN if set
    python scripts/check_roadmap_staleness.py --repo owner/x  # override the canonical repo

Never flagged (acceptance criterion: zero false positives): ``vX.Y.Z`` release refs,
ref-less items, and everything under Shipped. API failures (network, rate limit, a
deleted/private issue) **warn and exit 0** — a page-freshness guard must never brick
unrelated CI; the weekly cron retries soon enough.

Exit codes: 0 = fresh (or API unreachable, warned), 1 = stale ref(s) found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROADMAP_JSON = Path(__file__).parent.parent / "sites" / "marketing" / "data" / "roadmap.json"

# The refs semantically point at the canonical repo — roadmap.astro hardcodes its issue/release
# URLs — so a fork PR's GITHUB_REPOSITORY must NOT redirect the lookups. Forks rewrite this
# alongside roadmap.astro (or pass --repo).
DEFAULT_REPO = "protoLabsAI/protoAgent"

# Statuses whose refs must point at OPEN issues. Matched case-insensitively with hyphens
# folded to spaces ("In progress" / "In-progress" / "planned" all count); Shipped — and any
# future status — is left alone.
_ACTIVE_STATUSES = {"planned", "in progress"}

# Only pure ``#NNNN`` issue refs are checked; ``vX.Y.Z`` release refs (and anything else)
# never match, by construction.
_ISSUE_REF = re.compile(r"#(\d+)")


class ApiError(RuntimeError):
    """GitHub API unreachable/unusable (network, rate limit, 404) — soft-fail, never exit 1."""


def _is_active(status: str) -> bool:
    return status.strip().lower().replace("-", " ") in _ACTIVE_STATUSES


def active_issue_refs(sections: list[dict]) -> list[tuple[str, str, int]]:
    """roadmap.json sections → ``[(status, item title, issue number)]`` for every checkable ref.

    Release refs and ref-less items simply don't yield tuples — the no-false-positives
    guarantee lives here, not in downstream filtering.
    """
    refs: list[tuple[str, str, int]] = []
    for section in sections:
        status = str(section.get("status", ""))
        if not _is_active(status):
            continue
        for item in section.get("items", []):
            for ref in item.get("refs", []):
                m = _ISSUE_REF.fullmatch(str(ref).strip())
                if m:
                    refs.append((status, str(item.get("title", "")), int(m.group(1))))
    return refs


def fetch_issue_state(repo: str, number: int, token: str | None = None, timeout: float = 15.0) -> str:
    """GET /repos/{repo}/issues/{number} → ``"open"`` / ``"closed"``; raises ApiError otherwise.

    The /issues endpoint also resolves PR numbers (a ``#NNNN`` ref may be either), with the
    same ``state`` field. Unauthenticated works for a public repo but rate-limits at 60/hr —
    CI passes the workflow GITHUB_TOKEN.
    """
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "protoagent-roadmap-staleness",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            state = json.loads(r.read().decode("utf-8")).get("state")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:  # URLError covers HTTPError
        raise ApiError(f"#{number}: GitHub API lookup failed ({e})") from e
    if state not in ("open", "closed"):
        raise ApiError(f"#{number}: unexpected issue state {state!r}")
    return state


def run(sections: list[dict], fetch: Callable[[int], str]) -> tuple[list[str], list[str]]:
    """Check every active ref via ``fetch(number) -> state``; → (stale messages, soft warnings).

    ``fetch`` is injected so tests never touch the network.
    """
    stale: list[str] = []
    warnings: list[str] = []
    for status, title, number in active_issue_refs(sections):
        try:
            state = fetch(number)
        except ApiError as e:
            warnings.append(str(e))
            continue
        print(f"  [{status}] {title!r} → #{number}: {state}")
        if state == "closed":
            stale.append(
                f"[{status}] {title!r} refs #{number}, which is CLOSED — this shipped: rotate it "
                "into Shipped with a release ref (edit ROADMAP.md, then `python scripts/roadmap.py build`)."
            )
    return stale, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if Planned/In-progress roadmap refs point at closed issues")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/repo the refs point at (default: {DEFAULT_REPO})")
    parser.add_argument("--roadmap", default=str(ROADMAP_JSON), help="path to roadmap.json")
    args = parser.parse_args()

    sections = json.loads(Path(args.roadmap).read_text(encoding="utf-8"))
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    refs = active_issue_refs(sections)
    print(f"roadmap-staleness: {len(refs)} issue ref(s) to check under Planned/In-progress ({args.repo})")

    stale, warnings = run(sections, lambda n: fetch_issue_state(args.repo, n, token))

    for w in warnings:
        # ::warning:: renders as a yellow annotation in Actions without failing the job.
        print(f"::warning::roadmap-staleness: {w}")
    if stale:
        for s in stale:
            print(f"::error::roadmap-staleness: {s}")
        return 1
    if warnings:
        print("roadmap-staleness: API lookups incomplete — treating as pass (soft-fail by design)")
    else:
        print("roadmap-staleness: all Planned/In-progress refs point at open issues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
