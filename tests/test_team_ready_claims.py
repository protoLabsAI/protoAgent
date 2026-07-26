"""team-ready must reflect "already claimed by an open PR" (#2278)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "team_ready_claims", Path(__file__).resolve().parents[1] / "scripts" / "team_ready_claims.py"
)
trc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trc)


# ── which references count as a claim ─────────────────────────────────────────
def test_all_three_closing_keywords_claim():
    for kw in ("Closes", "closes", "Fixes", "fixed", "Resolves", "resolved"):
        assert trc.claimed_numbers(f"{kw} #2266") == {2266}, kw


def test_a_full_issue_url_claims_too():
    body = "Closes https://github.com/protoLabsAI/protoAgent/issues/2266"
    assert trc.claimed_numbers(body) == {2266}


def test_a_bare_mention_is_not_a_claim():
    """A PR that merely references an issue isn't doing its work — treating a mention as
    a claim would strand issues nobody has picked up."""
    assert trc.claimed_numbers("related to #2266, see also #1986") == set()


def test_multiple_claims_in_one_body():
    assert trc.claimed_numbers("Closes #1\nFixes #2\n\nresolves #3") == {1, 2, 3}


def test_empty_body_is_safe():
    assert trc.claimed_numbers("") == set()
    assert trc.claimed_numbers(None) == set()


# ── the reconciliation itself ─────────────────────────────────────────────────
def test_claimed_team_ready_issue_is_swapped():
    """The #2266 case: OPEN, team-ready, and PR #2269 already says Closes #2266."""
    issues = [{"number": 2266, "labels": ["team-ready"]}]
    prs = [{"number": 2269, "body": "Closes #2266"}]

    claim, release = trc.reconcile(issues, prs)

    assert claim == [{"number": 2266, "prs": [2269]}] and release == []


def test_unclaimed_team_ready_issue_is_left_alone():
    claim, release = trc.reconcile([{"number": 7, "labels": ["team-ready"]}], [])
    assert claim == [] and release == []


def test_claim_is_released_when_no_open_pr_claims_it_anymore():
    """An abandoned PR must not strand the issue as un-pickable — the label comes back."""
    issues = [{"number": 2266, "labels": ["claimed-by-pr"]}]

    claim, release = trc.reconcile(issues, prs=[])

    assert release == [{"number": 2266}] and claim == []


def test_a_still_claimed_issue_is_not_released():
    issues = [{"number": 2266, "labels": ["claimed-by-pr"]}]
    prs = [{"number": 2269, "body": "Fixes #2266"}]

    claim, release = trc.reconcile(issues, prs)

    assert claim == [] and release == []


def test_an_issue_that_never_had_team_ready_is_never_given_it():
    """`claimed-by-pr` is the bookkeeping marker — release restores the label only where
    this script took it, so an untriaged issue can't be promoted into intake by accident."""
    issues = [{"number": 99, "labels": ["bug"]}]

    claim, release = trc.reconcile(issues, prs=[])

    assert claim == [] and release == []


def test_several_prs_claiming_one_issue_are_all_reported():
    issues = [{"number": 5, "labels": ["team-ready"]}]
    prs = [{"number": 10, "body": "Closes #5"}, {"number": 11, "body": "Resolves #5"}]

    claim, _ = trc.reconcile(issues, prs)

    assert claim == [{"number": 5, "prs": [10, 11]}]


def test_a_pr_claiming_an_issue_that_is_not_team_ready_changes_nothing():
    issues = [{"number": 5, "labels": ["enhancement"]}]
    prs = [{"number": 10, "body": "Closes #5"}]

    claim, release = trc.reconcile(issues, prs)

    assert claim == [] and release == []
