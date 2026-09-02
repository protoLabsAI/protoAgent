#!/usr/bin/env python3
"""Assert a QA-panel verdict exists for the head a PR would actually merge (ADR 0078 D3/D5).

The QA panel (`protoreview[bot]`) is **not** one of the required checks on `main`, so
nothing gates a merge on a review — and its absence is *silent*: no status is posted at
all, so nothing turns red, `mergeStateStatus` stays `CLEAN`, and the PR looks finished.
An unreviewed merge is therefore indistinguishable from a reviewed one at a glance.

Observed on `main` before this gate existed:

* **#3298** merged at head ``7721e5b9`` while the panel had only ever reviewed
  ``373d2759`` — the code that landed was never reviewed.
* **#3301** put 65 files / ~6,900 lines on `main` through an integration branch with no
  panel verdict at all.
* 9 of the 25 most recent PRs had no panel review whatsoever, and PR size does not
  predict which — so this is not ADR 0078 D2's structural trigger being selective.

ADR 0078 already forbids exactly this: **D3** ("a promotable verdict from a starved run is
how an unreviewed PR auto-merges") and **D5** (an advanced head gets a delta review). This
script makes the requirement observable instead of assumed, by posting a commit status —
always, on every open PR — that answers one question:

    Is there a QA-panel verdict for THIS head SHA?

It deliberately does **not** re-judge code. Verdict *quality* is the panel's own business
and it posts its own ``QA panel`` status for that; a ``WARN`` at head satisfies this gate
(#3297 shipped one). Only two things fail here: no verdict for the head, and an explicitly
blocking verdict. That keeps the gate safe to mark **required** without making the advisory
tier of ADR 0078 secretly mandatory.

Escape hatch: the ``skip-review-gate`` label passes the check with the reason recorded in
the status description — the same shape as ``skip-changelog`` and ``gate-exempt``. It
exists because a required check that can never go green (panel outage, a PR the panel does
not pick up) would otherwise wedge the queue with no way out but an admin merge.

Stdlib + ``gh`` only, so CI needs no dependency install. Pure decision logic lives in
``decide()`` and is covered by ``tests/test_review_at_head.py``; everything above it is I/O.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# The panel stamps every review body with a machine-readable marker, e.g.
#   <!-- protoagent-qa-review head=4fec0e53… verdict=PASS promoted=true findings=1 -->
# Parsed rather than inferred from the review's GitHub state, because a passing review is
# posted as COMMENTED and only *promoted* to APPROVED once checks are green and threads are
# resolved (ADR 0078 D2) — so an APPROVED state is a stricter thing than "was reviewed",
# and #3311 merged with a PASS that was never promoted.
_MARKER = re.compile(r"<!--\s*protoagent-qa-review\s+(?P<attrs>.*?)-->", re.DOTALL)
_ATTR = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")

REVIEWER_LOGIN = os.environ.get("REVIEWER_LOGIN", "protoreview[bot]")
STATUS_CONTEXT = os.environ.get("STATUS_CONTEXT", "Review at head")
SKIP_LABEL = "skip-review-gate"

# Verdicts that are an explicit "do not merge". PASS and WARN both satisfy the gate; WARN is
# advisory by design (ADR 0078) and #3297 shipped one. Kept as a set so a new blocking
# verdict name only has to be added here.
BLOCKING_VERDICTS = frozenset({"FAIL", "BLOCK", "REJECT"})


@dataclass(frozen=True)
class Decision:
    """A commit-status outcome: ``state`` is GitHub's, ``description`` is what a human reads."""

    state: str  # "success" | "failure"
    description: str

    @property
    def ok(self) -> bool:
        return self.state == "success"


def parse_marker(body: str | None) -> dict[str, str] | None:
    """The marker attributes in one review body, or None when it carries no marker.

    Tolerant on purpose: an unparseable or marker-less body is "not a verdict" rather than an
    error, so a human comment from the reviewer account can never be mistaken for a review.
    """
    if not body:
        return None
    found = _MARKER.search(body)
    if not found:
        return None
    return {m["key"]: m["value"] for m in _ATTR.finditer(found["attrs"])}


def verdict_for_head(reviews: list[dict], head_sha: str) -> dict[str, str] | None:
    """The LAST marker whose ``head`` is ``head_sha``, or None.

    Last, not first: the panel posts COMMENTED and may later post an APPROVED promotion for
    the same head, and the newest is the one that stands. Matching is on the full SHA — a
    prefix match would let a review of a *different* commit satisfy the gate on a collision,
    which is the whole failure this guards.
    """
    match = None
    for review in reviews:
        if (review.get("user") or {}).get("login") != REVIEWER_LOGIN:
            continue
        attrs = parse_marker(review.get("body"))
        if attrs and attrs.get("head") == head_sha:
            match = attrs
    return match


def decide(reviews: list[dict], head_sha: str, labels: list[str]) -> Decision:
    """Whether this head may merge, given the reviews on it. Pure — the tested seam."""
    if SKIP_LABEL in labels:
        return Decision("success", f"review gate waived by the {SKIP_LABEL} label")

    attrs = verdict_for_head(reviews, head_sha)
    if attrs is None:
        reviewed = sorted(
            {
                a["head"][:12]
                for r in reviews
                if (a := parse_marker(r.get("body"))) and "head" in a
            }
        )
        if reviewed:
            # The dangerous case, and the reason this gate exists: the panel DID review, so
            # the PR carries a green verdict and reads as reviewed — but not this code.
            return Decision(
                "failure",
                f"no verdict for {head_sha[:12]}; the panel reviewed {', '.join(reviewed)} — push re-review or re-run",
            )
        return Decision("failure", f"no QA panel verdict for {head_sha[:12]} — this head is unreviewed")

    verdict = attrs.get("verdict", "?").upper()
    if verdict in BLOCKING_VERDICTS:
        return Decision("failure", f"QA panel returned {verdict} for {head_sha[:12]}")
    return Decision("success", f"{verdict} at {head_sha[:12]}")


# ── I/O ───────────────────────────────────────────────────────────────────────


def _gh(*args: str) -> str:
    """`gh` with the ambient token. Raises on failure — a broken API call must not be
    mistaken for "no verdict" and silently fail a PR that was in fact reviewed."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _reviews(pr: int) -> list[dict]:
    return json.loads(_gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{pr}/reviews"))


def _open_prs() -> list[dict]:
    return json.loads(_gh("pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefOid,labels"))


def _post_status(sha: str, decision: Decision, pr: int) -> None:
    _gh(
        "api",
        "-X",
        "POST",
        f"repos/{{owner}}/{{repo}}/statuses/{sha}",
        "-f",
        f"state={decision.state}",
        "-f",
        f"context={STATUS_CONTEXT}",
        # GitHub truncates descriptions past 140 chars.
        "-f",
        f"description={decision.description[:140]}",
        "-f",
        f"target_url={os.environ.get('RUN_URL', '')}",
    )
    print(f"#{pr} {sha[:12]} -> {decision.state}: {decision.description}")


def check_pr(pr: int, head_sha: str, labels: list[str], *, dry_run: bool) -> Decision:
    decision = decide(_reviews(pr), head_sha, labels)
    if dry_run:
        print(f"[dry-run] #{pr} {head_sha[:12]} -> {decision.state}: {decision.description}")
    else:
        _post_status(head_sha, decision, pr)
    return decision


def main() -> int:
    dry_run = bool(os.environ.get("DRY_RUN"))
    pr_number = os.environ.get("PR_NUMBER")

    if pr_number:
        head = os.environ["HEAD_SHA"]
        labels = [x for x in os.environ.get("PR_LABELS", "").split(",") if x]
        check_pr(int(pr_number), head, labels, dry_run=dry_run)
        # Always exit 0: the STATUS is the signal, not this job. A non-zero exit would add a
        # second red check saying the same thing, and would make an API hiccup look like an
        # unreviewed PR.
        return 0

    # Sweep mode (scheduled backstop) — webhooks do get dropped here, and a PR whose
    # `pull_request_review` event was missed would otherwise sit with a stale red status.
    for row in _open_prs():
        labels = [lbl["name"] for lbl in row.get("labels") or []]
        try:
            check_pr(row["number"], row["headRefOid"], labels, dry_run=dry_run)
        except RuntimeError as exc:  # one unreachable PR must not abandon the rest
            print(f"#{row['number']}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
