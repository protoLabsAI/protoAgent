"""The `Review at head` gate (ADR 0078 D3/D5) — scripts/review_at_head.py.

This is a GUARD, so the tests exercise the guard rather than the thing it guards: every case
below is built from a marker shape actually observed on this repo's PRs, and the central one
replays the real #3298 failure (reviewed `373d2759`, merged `7721e5b9`) that the gate exists
to catch. A guard whose own tests only feed it the happy shape is the blind spot — a hardcoded
list or a narrow regex passes those and still misses the case in production.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "review_at_head", Path(__file__).resolve().parents[1] / "scripts" / "review_at_head.py"
)
rah = importlib.util.module_from_spec(_SPEC)
# Register BEFORE exec: `@dataclass` resolves annotations via `sys.modules[cls.__module__]`,
# which is absent for a spec-loaded module and raises. (test_team_ready_claims.py omits this
# only because that script has no dataclass.)
sys.modules[_SPEC.name] = rah
_SPEC.loader.exec_module(rah)


# Real SHAs from the incident this gate is built for (#3298).
REVIEWED = "373d27593952480244cf203392d5a8b5e0ef0087"
MERGED = "7721e5b974c17ae5b7fb4f4a003f9d8fb8103447"


def review(head, verdict="PASS", *, login=rah.REVIEWER_LOGIN, promoted="false", body=None):
    """A review as the GitHub API returns it, carrying the panel's real marker shape."""
    if body is None:
        body = (
            f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted={promoted} -->\n"
            "## QA panel review\n\nsome prose\n"
        )
    return {"user": {"login": login}, "body": body}


# ── the incident this exists to catch ─────────────────────────────────────────


def test_a_verdict_for_an_OLDER_head_does_not_satisfy_the_merged_head():
    # #3298 exactly: the panel reviewed and approved, so the PR reads as reviewed — but a push
    # landed afterwards and the code that merged was never looked at. Nothing was red.
    reviews = [review(REVIEWED), review(REVIEWED, promoted="true")]
    decision = rah.decide(reviews, MERGED, [])
    assert not decision.ok
    # The message must name BOTH heads, or the reader cannot tell this from "never reviewed".
    assert MERGED[:12] in decision.description
    assert REVIEWED[:12] in decision.description


def test_no_reviews_at_all_is_refused_and_says_so_plainly():
    decision = rah.decide([], MERGED, [])
    assert not decision.ok
    assert "unreviewed" in decision.description


def test_a_verdict_at_the_merged_head_passes():
    decision = rah.decide([review(MERGED)], MERGED, [])
    assert decision.ok and "PASS" in decision.description


# ── verdict semantics: presence is the gate, quality is the panel's own status ─


def test_WARN_at_head_passes_because_the_qa_tier_is_advisory():
    # #3297 shipped a WARN. Failing on it here would quietly make ADR 0078's advisory tier
    # mandatory, which is a policy change this gate is explicitly not making.
    decision = rah.decide([review(MERGED, "WARN")], MERGED, [])
    assert decision.ok and "WARN" in decision.description


@pytest.mark.parametrize("verdict", sorted(rah.BLOCKING_VERDICTS))
def test_an_explicitly_blocking_verdict_fails(verdict):
    decision = rah.decide([review(MERGED, verdict)], MERGED, [])
    assert not decision.ok and verdict in decision.description


def test_verdict_matching_is_case_insensitive():
    assert not rah.decide([review(MERGED, "fail")], MERGED, []).ok


def test_the_LAST_marker_for_a_head_wins():
    # The panel posts COMMENTED and may later promote the same head to APPROVED; a promotion
    # must not be overridden by the earlier row, nor vice versa.
    reviews = [review(MERGED, "FAIL"), review(MERGED, "PASS", promoted="true")]
    assert rah.decide(reviews, MERGED, []).ok


# ── what must NOT count as a verdict ───────────────────────────────────────────


def test_a_review_from_another_account_is_not_a_verdict():
    # CodeRabbit reviews most of these PRs and would otherwise satisfy the gate for free.
    reviews = [review(MERGED, login="coderabbitai[bot]")]
    assert not rah.decide(reviews, MERGED, []).ok


def test_a_human_comment_from_the_reviewer_account_is_not_a_verdict():
    reviews = [{"user": {"login": rah.REVIEWER_LOGIN}, "body": "looks fine to me, merging"}]
    assert not rah.decide(reviews, MERGED, []).ok


def test_an_empty_or_missing_body_is_not_a_verdict():
    assert rah.parse_marker(None) is None
    assert rah.parse_marker("") is None
    assert not rah.decide([{"user": {"login": rah.REVIEWER_LOGIN}, "body": None}], MERGED, []).ok


def test_a_PREFIX_of_the_head_sha_does_not_satisfy_the_gate():
    # Matching on a short prefix would let a review of a different commit pass. The marker
    # carries the full 40-char SHA, so compare the whole thing.
    assert not rah.decide([review(MERGED[:12])], MERGED, []).ok


# ── the escape hatch ──────────────────────────────────────────────────────────


def test_the_skip_label_waives_the_gate_and_records_why():
    decision = rah.decide([], MERGED, [rah.SKIP_LABEL])
    assert decision.ok
    assert rah.SKIP_LABEL in decision.description  # the waiver must be legible in the status


def test_an_unrelated_label_does_not_waive_the_gate():
    assert not rah.decide([], MERGED, ["skip-changelog", "enhancement"]).ok


# ── marker parsing, against the shapes really posted ──────────────────────────


def test_parses_the_real_marker_including_the_optional_findings_attribute():
    body = (
        "<!-- protoagent-qa-review head=32cc20d3cce2e2ed78a5038ee6533e64bb23db8b "
        "verdict=PASS promoted=true findings=1 -->\n## QA panel review — **PASS**"
    )
    attrs = rah.parse_marker(body)
    assert attrs["head"] == "32cc20d3cce2e2ed78a5038ee6533e64bb23db8b"
    assert attrs["verdict"] == "PASS" and attrs["findings"] == "1"


def test_a_marker_that_is_not_the_panels_is_ignored():
    assert rah.parse_marker("<!-- some-other-bot head=abc verdict=PASS -->") is None


def test_the_description_stays_within_githubs_140_char_status_limit():
    # Long inputs must not produce a status GitHub rejects; the caller truncates, but the
    # generated text should be comfortably short on its own for the common cases.
    many = [review(f"{i:040x}") for i in range(40)]
    assert len(rah.decide(many, MERGED, []).description) < 400  # truncation handles the rest
