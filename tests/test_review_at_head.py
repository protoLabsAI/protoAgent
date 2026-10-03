"""The `Review at head` gate (ADR 0078 D3/D5) — scripts/review_at_head.py.

This is a GUARD, so the tests exercise the guard rather than the thing it guards: every case
below is built from a marker shape actually observed on this repo's PRs, and the central one
replays the real #3298 failure (reviewed `373d2759`, merged `7721e5b9`) that the gate exists
to catch. A guard whose own tests only feed it the happy shape is the blind spot — a hardcoded
list or a narrow regex passes those and still misses the case in production.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import re
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


def review(
    head,
    verdict="PASS",
    *,
    login=rah.REVIEWER_LOGIN,
    promoted="false",
    coverage=None,
    standing_block=None,
    body=None,
):
    """A review as the GitHub API returns it, carrying the panel's real marker shape.

    ``coverage`` / ``standing_block`` are the #3334 contract attributes: omitted (``None``)
    they are not stamped at all, reproducing today's legacy marker exactly, so the pre-rollout
    cases and the older tests share one builder.
    """
    if body is None:
        attrs = f"head={head} verdict={verdict} promoted={promoted}"
        if coverage is not None:
            attrs += f" coverage={coverage}"
        if standing_block is not None:
            attrs += f" standing_block={standing_block}"
        body = (
            f"<!-- protoagent-qa-review {attrs} -->\n"
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


def test_a_promotion_speaks_only_for_a_head_with_no_round():
    # The panel posts COMMENTED and may later promote the same head to APPROVED. A promotion
    # is not a round (pr-reviewer-plugin#234): it counts only when it is all there is, and it
    # never outvotes a round on the same head.
    assert rah.decide([review(MERGED, "PASS", promoted="true")], MERGED, []).ok
    assert rah.decide([review(MERGED, "PASS"), review(MERGED, "PASS", promoted="true")], MERGED, []).ok
    reviews = [review(MERGED, "FAIL"), review(MERGED, "PASS", promoted="true")]
    assert not rah.decide(reviews, MERGED, []).ok


def test_the_STRICTEST_round_for_a_head_wins_not_the_latest():
    # pr-reviewer-plugin#239: after a FAIL, a re-review PASS on the same head used to turn this
    # check green while the plugin's `QA panel` (strictest per head) stayed red. Each round is
    # tagged so the test proves WHICH round spoke, not only that something said FAIL
    # (pr-reviewer-plugin#254).
    def tagged(verdict, n):
        marker = f"<!-- protoagent-qa-review head={MERGED} verdict={verdict} promoted=false round={n} -->"
        return review(MERGED, body=f"{marker}\n## QA panel review\n")

    reviews = [tagged("FAIL", 1), tagged("PASS", 2)]
    assert rah.parse_marker(reviews[-1]["body"])["verdict"] == "PASS"  # what "latest" read
    pick = rah.verdict_for_head(reviews, MERGED)
    assert (pick["verdict"], pick["round"]) == ("FAIL", "1")  # the EARLIER round speaks
    decision = rah.decide(reviews, MERGED, [])
    assert not decision.ok and decision.description == f"QA panel returned FAIL for {MERGED[:12]}"
    # Among equally strict rounds the newest speaks; a stricter earlier one beats a later one.
    assert rah.verdict_for_head([tagged("FAIL", 1), tagged("PASS", 2), tagged("FAIL", 3)], MERGED)["round"] == "3"
    pick = rah.verdict_for_head([tagged("WARN", 1), tagged("PASS", 2)], MERGED)
    assert (pick["verdict"], pick["round"]) == ("WARN", "1")


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


# ── the coverage / standing-block contract (#3334) ─────────────────────────────
#
# Two INDEPENDENT producer-owned facts: `coverage` completeness and whether a `standing_block`
# remains. A head merges only when coverage is explicitly complete AND no standing block is
# retained. Missing/unknown attributes fail closed — but only once the producer rollout is on
# (`require_contract=True`); an attribute that IS present is honoured either way.


def test_complete_coverage_and_no_standing_block_passes_as_a_reviewed_head():
    # r1: PASS with an explicit clean contract merges exactly as a valid reviewed head.
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="complete", standing_block="false")],
        MERGED,
        [],
        require_contract=True,
    )
    assert decision.ok and "PASS" in decision.description


def test_WARN_with_a_clean_contract_still_passes():
    # WARN stays advisory (ADR 0078) even under the contract, provided the contract is clean.
    decision = rah.decide(
        [review(MERGED, "WARN", coverage="complete", standing_block="false")],
        MERGED,
        [],
        require_contract=True,
    )
    assert decision.ok and "WARN" in decision.description


def test_a_clean_contract_is_honoured_case_insensitively():
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="Complete", standing_block="FALSE")],
        MERGED,
        [],
        require_contract=True,
    )
    assert decision.ok


def test_incomplete_coverage_fails_and_names_coverage_as_the_reason():
    # r2: incomplete coverage is a non-success that identifies coverage.
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="incomplete", standing_block="false")],
        MERGED,
        [],
        require_contract=True,
    )
    assert not decision.ok
    assert "coverage" in decision.description.lower()


def test_incomplete_coverage_is_not_rescued_by_a_confident_PASS_prose_body():
    # r2 + r6: the verdict is PASS and the prose insists everything is done, but the machine
    # attribute says coverage is incomplete. The gate reads the attribute, never the prose.
    body = (
        f"<!-- protoagent-qa-review head={MERGED} verdict=PASS coverage=incomplete standing_block=false -->\n"
        "## QA panel review — **PASS**\n\nAll lanes complete, no blocking findings, ship it. ✅\n"
    )
    decision = rah.decide([review(MERGED, body=body)], MERGED, [], require_contract=True)
    assert not decision.ok
    assert "coverage" in decision.description.lower()


def test_a_standing_block_fails_even_with_complete_coverage_and_a_PASS_verdict():
    # r3: the standing block holds even when coverage is complete and the verdict is PASS.
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="complete", standing_block="true")],
        MERGED,
        [],
        require_contract=True,
    )
    assert not decision.ok
    assert "standing" in decision.description.lower() or "block" in decision.description.lower()


def test_coverage_and_standing_block_are_independent_reasons():
    # A block is reported as a block, incomplete coverage as coverage — never conflated.
    blocked = rah.decide(
        [review(MERGED, "PASS", coverage="complete", standing_block="true")], MERGED, [], require_contract=True
    )
    uncovered = rah.decide(
        [review(MERGED, "PASS", coverage="incomplete", standing_block="false")], MERGED, [], require_contract=True
    )
    assert "standing" in blocked.description.lower() and "coverage" not in blocked.description.lower()
    assert "coverage" in uncovered.description.lower()


# ── fail-closed, gated on the producer rollout ─────────────────────────────────


def test_a_legacy_marker_still_passes_BEFORE_the_rollout():
    # The load-bearing rollout guard: before the emitter ships the attributes every marker is
    # legacy (no coverage/standing_block). Failing those closed now would red every open PR, so
    # a legacy PASS must still pass while `require_contract` is off.
    decision = rah.decide([review(MERGED, "PASS")], MERGED, [], require_contract=False)
    assert decision.ok and "PASS" in decision.description


def test_a_legacy_marker_fails_closed_AFTER_the_rollout():
    # r4: once the producer contract is required, a marker missing the attributes is non-success.
    decision = rah.decide([review(MERGED, "PASS")], MERGED, [], require_contract=True)
    assert not decision.ok


def test_an_unknown_coverage_value_fails_closed():
    # r4: a malformed/unknown value is non-satisfying, not silently accepted.
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="mostly", standing_block="false")],
        MERGED,
        [],
        require_contract=True,
    )
    assert not decision.ok and "coverage" in decision.description.lower()


def test_an_unknown_standing_block_value_fails_closed():
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="complete", standing_block="maybe")],
        MERGED,
        [],
        require_contract=True,
    )
    assert not decision.ok


def test_a_half_contract_fails_closed_when_only_coverage_is_present():
    # A marker that carries one attribute but not the other is malformed → non-success, because
    # its mere presence enables enforcement even with the flag off.
    decision = rah.decide(
        [review(MERGED, "PASS", coverage="complete")], MERGED, [], require_contract=False
    )
    assert not decision.ok


def test_an_explicit_attribute_is_honoured_even_BEFORE_the_rollout():
    # An explicit producer fact is authoritative regardless of the flag: a standing block on
    # the marker blocks even with `require_contract` off, and incomplete coverage likewise.
    blocked = rah.decide(
        [review(MERGED, "PASS", coverage="complete", standing_block="true")], MERGED, [], require_contract=False
    )
    uncovered = rah.decide(
        [review(MERGED, "PASS", coverage="incomplete", standing_block="false")], MERGED, [], require_contract=False
    )
    assert not blocked.ok and not uncovered.ok


def test_a_blocking_verdict_still_fails_before_the_contract_is_even_consulted():
    # The verdict enum is unchanged: FAIL is refused as before, whatever the contract says.
    decision = rah.decide(
        [review(MERGED, "FAIL", coverage="complete", standing_block="false")],
        MERGED,
        [],
        require_contract=True,
    )
    assert not decision.ok and "FAIL" in decision.description


# ── v0.158.0 fixtures: the real shapes the contract must survive (r5) ──────────


def test_v0158_structural_lane_skipped_after_gateway_exit_4_is_incomplete_coverage():
    # A structural review lane bailed on gateway exit 4, so the panel never covered it: the
    # verdict may read PASS, but coverage is not complete.
    marker = review(MERGED, "PASS", coverage="incomplete", standing_block="false")
    decision = rah.decide([marker], MERGED, [], require_contract=True)
    assert not decision.ok and "coverage" in decision.description.lower()


def test_v0158_unreadable_panel_brief_is_unavailable_coverage():
    # The panel brief could not be read, so coverage could not be established.
    marker = review(MERGED, "WARN", coverage="unavailable", standing_block="false")
    decision = rah.decide([marker], MERGED, [], require_contract=True)
    assert not decision.ok and "coverage" in decision.description.lower()


def test_v0158_pass_that_explicitly_retains_a_standing_block_is_blocked():
    marker = review(MERGED, "PASS", coverage="complete", standing_block="true")
    decision = rah.decide([marker], MERGED, [], require_contract=True)
    assert not decision.ok
    assert "standing" in decision.description.lower() or "block" in decision.description.lower()


def test_v0158_a_genuinely_clean_pass_succeeds():
    marker = review(MERGED, "PASS", coverage="complete", standing_block="false")
    decision = rah.decide([marker], MERGED, [], require_contract=True)
    assert decision.ok and "PASS" in decision.description


# ── the gate's own robustness: it must not fail open ─────────────────────────────────
#
# Every case below is a way this job used to pass a head it should not have: an API
# hiccup read as a clean run, a silent off-switch, a marker with no verdict in it.


def test_a_gh_failure_in_single_pr_mode_still_exits_zero(monkeypatch, capsys):
    """The job's promise is that the STATUS is the signal, not the job's exit code.

    Sweep mode has always caught `RuntimeError` ("one unreachable PR must not abandon the
    rest"); single-PR mode called `check_pr` bare. So a transient `gh` failure — a dropped
    call, a rate limit — escaped `main()`, `raise SystemExit(main())` never ran, and the job
    exited non-zero: a second red check saying the same thing as the status, and an API
    hiccup made to look like an unreviewed PR. Exactly what the comment there forbids.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("gh api repos/o/r/pulls/123/reviews failed: 502 Bad Gateway")

    monkeypatch.setattr(rah, "_gh", _boom)
    monkeypatch.setenv("PR_NUMBER", "123")
    monkeypatch.setenv("HEAD_SHA", "a" * 40)
    monkeypatch.setenv("PR_LABELS", "")
    monkeypatch.delenv("DRY_RUN", raising=False)

    assert rah.main() == 0
    assert "502 Bad Gateway" in capsys.readouterr().err

def test_dry_run_is_off_unless_explicitly_truthy(monkeypatch):
    """`DRY_RUN=false` used to ENABLE dry-run.

    `bool("false")` is True, so any non-empty value turned the gate into a no-op: no status
    posted, nothing red, and the merge gate silently ineffective. It is parsed like
    REQUIRE_COVERAGE_CONTRACT now — a silent off-switch on a merge gate is the worst kind.
    """
    seen: dict[str, bool] = {}
    monkeypatch.setattr(rah, "check_pr", lambda pr, head, labels, *, dry_run: seen.update(dry_run=dry_run))
    monkeypatch.setenv("PR_NUMBER", "123")
    monkeypatch.setenv("HEAD_SHA", MERGED)
    monkeypatch.setenv("PR_LABELS", "")

    for value in ("false", "0", "no", "off", ""):
        seen.clear()
        monkeypatch.setenv("DRY_RUN", value)
        assert rah.main() == 0
        assert seen["dry_run"] is False, f"DRY_RUN={value!r} must not enable dry-run"

    for value in ("1", "true", "yes", "on", "TRUE"):
        seen.clear()
        monkeypatch.setenv("DRY_RUN", value)
        assert rah.main() == 0
        assert seen["dry_run"] is True, f"DRY_RUN={value!r} must enable dry-run"


def test_a_marker_with_no_verdict_attribute_fails_closed():
    """A marker is not a verdict.

    This gate answers one question — is there a QA-panel verdict for THIS head — and the module
    fails closed on every other missing critical attribute. A marker matching the head but
    carrying no `verdict` used to read as "?", miss BLOCKING_VERDICTS, and return success.
    """
    body = f"<!-- protoagent-qa-review head={MERGED} -->\n## QA panel review\n\nsome prose\n"
    decision = rah.decide([review(MERGED, body=body)], MERGED, [])
    assert not decision.ok
    assert "carries no verdict" in decision.description


# ── a FAIL a later round supersedes (pr-reviewer-plugin#234, vendored) ─────────
#
# The strictest round wins, except a FAIL that a later COMPLETE, VERIFIED round on the same
# head superseded: it dispositioned that FAIL (its `disp=` record names it) and refuted EVERY
# blocking finding with evidence, the refutation honoured. The rule is vendored from the
# plugin's rounds.py, so this check and the plugin's `QA panel` give the same answer.

FILE = "src/engagement.rs"
MAJOR = {
    "file": FILE,
    "line": 42,
    "severity": "major",
    "claim": "Unconditional Engaged exclusions silently alter legacy behaviour.",
    "evidence": "if state == State::Engaged { return; }",
    "verdict": "confirmed",
}
REFUTED = {"a": f"{FILE}:42", "d": "refuted", "e": True, "h": True}


def token(record):
    """The panel's `disp=` disposition record: unpadded base64url of compact JSON."""
    raw = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def panel_round(verdict, findings=(), *, id, record=None, complete=True, verified=True):
    """A panel round as the plugin posts it: the marker, then the findings record."""
    attrs = f"head={MERGED} verdict={verdict} promoted=false"
    attrs += "" if complete else " complete=false"
    attrs += "" if verified else " verified=false"
    attrs += f" disp={token(record)}" if record else ""
    body = (
        f"<!-- protoagent-qa-review {attrs} -->\n## QA panel review — **{verdict}**\n\n"
        "<details>\n<summary>findings JSON (machine-readable)</summary>\n\n"
        f"```json\n{json.dumps(list(findings), indent=2)}\n```\n</details>"
    )
    return {"user": {"login": rah.REVIEWER_LOGIN}, "body": body, "id": id}


def refuting(of=102, **row):
    return {"of": of, "rows": [{**REFUTED, **row}]}


R1 = panel_round("PASS", id=101)
R2 = panel_round("FAIL", [MAJOR], id=102)


def test_a_round_that_refutes_every_blocking_finding_with_evidence_supersedes_the_fail():
    # mythxengine-sdk#409: r1 PASS, r2 FAIL with one major, r3 on the SAME head refutes it.
    decision = rah.decide([R1, R2, panel_round("PASS", id=103, record=refuting())], MERGED, [])
    assert decision.ok and "refuted with evidence" in decision.description


@pytest.mark.parametrize(
    "newer",
    [
        panel_round("PASS", id=103, record=refuting(d="open")),
        panel_round("PASS", id=103, record=refuting(d="fixed")),
        panel_round("PASS", id=103, record={"of": 102, "rows": []}),
        panel_round("PASS", id=103, record=refuting(e=False)),
        panel_round("PASS", id=103, record=refuting(h=False)),
        panel_round("PASS", id=103, record=refuting(), complete=False),
        panel_round("PASS", id=103, record=refuting(), verified=False),
        panel_round("PASS", id=103),
        panel_round("PASS", [{**MAJOR, "carried": True}], id=103, record=refuting()),
    ],
    ids=[
        "open",
        "fixed-on-an-unchanged-head",
        "unaccounted",
        "no-evidence",
        "not-honoured",
        "incomplete",
        "unverified",
        "no-record",
        "still-carried",
    ],
)
def test_the_fail_stands_unless_every_supersede_condition_holds(newer):
    decision = rah.decide([R1, R2, newer], MERGED, [])
    assert not decision.ok and "FAIL" in decision.description


def test_two_racing_rounds_on_one_head_still_settle_strictest():
    # pr-reviewer-plugin#89: both started from r1, so neither names the other.
    racer = panel_round("PASS", id=103, record=refuting(of=101))
    for reviews in ([R1, R2, racer], [R1, racer, R2]):
        assert not rah.decide(reviews, MERGED, []).ok


def test_a_supersede_rule_that_raises_falls_back_to_strictest(monkeypatch, capsys):
    def boom(_rounds):
        raise RuntimeError("rule broke")

    monkeypatch.setattr(rah, "_v_superseded_fails", boom)
    assert not rah.decide([R1, R2, panel_round("PASS", id=103, record=refuting())], MERGED, []).ok
    assert "strictest verdict wins" in capsys.readouterr().err


def _canonical(node):
    """Mirror of pr-reviewer-plugin `scripts/vendor_supersede_rule.py::canonical`."""
    if isinstance(node, list):
        kept = [
            _canonical(n)
            for n in node
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
        ]
        return "[" + ",".join(kept) + "]"
    if isinstance(node, ast.AST):
        fields = [
            f"{name}={_canonical(getattr(node, name, None))}"
            for name in node._fields
            if getattr(node, name, None) not in (None, [])
        ]
        return f"{type(node).__name__}(" + ",".join(fields) + ")"
    return repr(node)


def test_the_vendored_supersede_block_is_unedited():
    # Edit the rule in pr-reviewer-plugin and re-sync (`scripts/vendor_supersede_rule.py
    # --sync`), never here: this copy must keep answering exactly as `QA panel` does.
    text = Path(rah.__file__).read_text(encoding="utf-8")
    begin = text.index("# ── BEGIN VENDORED SUPERSEDE RULE")
    end = text.index("\n", text.index("# ── END VENDORED SUPERSEDE RULE"))
    block = text[begin:end]
    recorded = re.search(r"block-ast-sha256:\s*(\S+)", block).group(1)
    assert hashlib.sha256(_canonical(ast.parse(block).body).encode()).hexdigest() == recorded


# ── a hung `gh` is a failed call, not a hung gate (pr-reviewer-plugin#254) ─────


def test_a_hung_gh_call_times_out_as_a_failed_api_call(monkeypatch):
    seen = {}

    def hung(cmd, **kwargs):
        seen.update(kwargs)
        raise rah.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(rah.subprocess, "run", hung)
    with pytest.raises(RuntimeError, match="timed out"):
        rah._gh("api", "repos/o/r/pulls/1/reviews")
    assert seen["timeout"] == rah.GH_TIMEOUT_S > 0


def test_a_timed_out_reviews_read_posts_no_status_and_still_exits_zero(monkeypatch, capsys):
    calls = []

    def hung(cmd, **kwargs):
        calls.append(cmd)
        raise rah.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(rah.subprocess, "run", hung)
    monkeypatch.setenv("PR_NUMBER", "123")
    monkeypatch.setenv("HEAD_SHA", "a" * 40)
    monkeypatch.setenv("PR_LABELS", "")
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert rah.main() == 0
    # Fail closed: the read failed, so no verdict was decided and no status was written.
    assert len(calls) == 1 and "statuses" not in " ".join(calls[0])
    assert "timed out" in capsys.readouterr().err
