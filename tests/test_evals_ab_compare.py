"""Tests for the learned-context A/B comparison (ADR 0126 D6, bd-kucq.5).

``evals.compare.compare_ab`` / ``format_ab_markdown`` / ``write_ab_comparison`` diff
the two arm reports ``scripts/evals_ab.py`` produces — arm A (template only) vs arm B
(template + a copy of a source agent's learned memories and private skills). What the
card owns and this file pins:

* per-case and per-tier (gate/tracked) pass-rate deltas, B minus A, plus turn count
  and latency per arm;
* every case that flips between arms, each carrying BOTH runs' trace links;
* the seeded bad-memory fixture surfaces as a visible A-pass/B-fail regression flip —
  proof the A/B detects harm, not just help;
* the existing ``compare_reports`` (ordinary two-report diff) is untouched.

No live model and no agent boot: everything runs off report dicts and on-disk fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.compare import (
    compare_ab,
    compare_reports,
    format_ab_markdown,
    write_ab_comparison,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "evals_ab"


# ── helpers ───────────────────────────────────────────────────────────────────


def _row(cid, tier, passed, *, trace, duration_ms=1000, tokens=500, category="tool"):
    return {
        "id": cid,
        "category": category,
        "name": cid.replace("_", " ").title(),
        "passed": passed,
        "detail": "OK" if passed else "boom",
        "duration_ms": duration_ms,
        "tokens": tokens,
        "skipped": False,
        "trace_id": trace,
        "tier": tier,
        "instance": False,
    }


def _report(rows):
    return {
        "model": "fake/eval-model",
        "total": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "skipped": 0,
        "results": rows,
    }


# ── per-case / per-tier deltas and flips (both directions) ──────────────────────


def _two_arms():
    """Arm A vs arm B with one stable case, one regression, one improvement, and a
    tracked-tier regression — so every delta sign and flip direction is exercised."""
    arm_a = _report(
        [
            _row("g1", "gate", True, trace="a-g1"),
            _row("g2", "gate", True, trace="a-g2"),  # will regress
            _row("g3", "gate", False, trace="a-g3"),  # will improve
            _row("t1", "tracked", True, trace="a-t1", duration_ms=8000, tokens=4000),  # will regress
        ]
    )
    arm_b = _report(
        [
            _row("g1", "gate", True, trace="b-g1"),
            _row("g2", "gate", False, trace="b-g2"),  # regression
            _row("g3", "gate", True, trace="b-g3"),  # improvement
            _row("t1", "tracked", False, trace="b-t1", duration_ms=9000, tokens=4800),  # regression
        ]
    )
    return arm_a, arm_b


def test_per_tier_deltas_b_minus_a():
    cmp = compare_ab(*_two_arms())

    # Gate: 2/3 both arms (one case regressed, one improved) → net zero.
    assert cmp["tier_deltas"]["gate"]["a"] == [2, 3]
    assert cmp["tier_deltas"]["gate"]["b"] == [2, 3]
    assert cmp["tier_deltas"]["gate"]["delta_passed"] == 0
    assert cmp["tier_deltas"]["gate"]["delta_rate"] == 0.0

    # Tracked: 1/1 → 0/1, a clean regression.
    assert cmp["tier_deltas"]["tracked"]["delta_passed"] == -1
    assert cmp["tier_deltas"]["tracked"]["delta_rate"] == -1.0

    # Headline is the gate delta on its own (ADR 0126 D1).
    assert cmp["headline"]["gate_delta_passed"] == 0
    assert cmp["headline"]["gate_delta_rate"] == 0.0


def test_per_case_deltas():
    cmp = compare_ab(*_two_arms())
    by_id = {c["id"]: c for c in cmp["case_deltas"]}
    assert by_id["g1"]["delta"] == 0  # pass → pass
    assert by_id["g2"]["delta"] == -1  # pass → fail
    assert by_id["g3"]["delta"] == +1  # fail → pass
    assert by_id["t1"]["delta"] == -1  # pass → fail
    assert by_id["g2"]["a_passed"] is True and by_id["g2"]["b_passed"] is False


def test_flips_both_directions_partitioned():
    cmp = compare_ab(*_two_arms())
    regressions = {f["id"] for f in cmp["regressions"]}
    improvements = {f["id"] for f in cmp["improvements"]}
    assert regressions == {"g2", "t1"}
    assert improvements == {"g3"}
    # g1 did not flip, so it is absent from the flip list entirely.
    assert {f["id"] for f in cmp["flips"]} == {"g2", "g3", "t1"}


def test_turn_count_and_latency_per_arm():
    cmp = compare_ab(*_two_arms())
    a, b = cmp["arms"]["A"], cmp["arms"]["B"]
    # Four non-skipped cases ran in each arm.
    assert a["turns"] == 4 and b["turns"] == 4
    # Latency and tokens are summed per arm; B is the heavier (learned-context) arm.
    assert a["latency_ms_total"] == 1000 + 1000 + 1000 + 8000
    assert b["latency_ms_total"] == 1000 + 1000 + 1000 + 9000
    assert b["latency_ms_total"] > a["latency_ms_total"]
    assert a["tokens_total"] == 500 * 3 + 4000
    assert b["latency_ms_avg"] == round(b["latency_ms_total"] / 4)


def test_every_flip_carries_both_trace_links():
    cmp = compare_ab(*_two_arms())
    for f in cmp["flips"]:
        # The A and B traces are the ones stamped on that case in each arm's report.
        assert f["a_trace"] == f"a-{f['id']}"
        assert f["b_trace"] == f"b-{f['id']}"
        assert f["a_trace"] and f["b_trace"]


def test_skipped_rows_excluded_from_turns_and_tiers():
    """A skipped case carries no pass/fail or cost signal, so it never counts toward
    a tier split or the turn count (same convention as the runner)."""
    arm_a = _report([_row("g1", "gate", True, trace="a-g1")])
    arm_b = _report(
        [
            _row("g1", "gate", True, trace="b-g1"),
            {**_row("g2", "gate", True, trace="b-g2"), "skipped": True, "passed": True},
        ]
    )
    cmp = compare_ab(arm_a, arm_b)
    assert cmp["arms"]["B"]["turns"] == 1
    assert cmp["tier_deltas"]["gate"]["b"] == [1, 1]
    # A skipped-only case never flips.
    assert cmp["flips"] == []


# ── markdown rendering ─────────────────────────────────────────────────────────


def test_markdown_renders_tiers_cost_and_flip_traces():
    cmp = compare_ab(*_two_arms())
    # Inject a resolver so trace links render without a live Langfuse.
    md = format_ab_markdown(
        cmp,
        a_file="ab-A-1.json",
        b_file="ab-B-1.json",
        trace_link=lambda t: f"https://trace.example/{t}",
    )
    # Headline + per-tier + cost sections.
    assert "Headline — gate" in md
    assert "## Per-tier pass rate" in md
    assert "## Cost per arm" in md
    assert "Cases (turns)" in md
    # Flip sections, regressions before improvements.
    assert "Regressions (A pass" in md and "Improvements (A fail" in md
    assert md.index("Regressions") < md.index("Improvements")
    # Both trace links present on the regressed case.
    assert "https://trace.example/a-g2" in md
    assert "https://trace.example/b-g2" in md
    # Points back at the arm reports it was built from.
    assert "ab-A-1.json" in md and "ab-B-1.json" in md


def test_markdown_default_trace_link_falls_back_to_raw_id():
    """With no Langfuse template resolvable (the test env), the raw trace id is still
    rendered so a human can find the run."""
    cmp = compare_ab(*_two_arms())
    md = format_ab_markdown(cmp)
    assert "a-g2" in md and "b-g2" in md


def test_markdown_no_flips_message():
    arm = _report([_row("g1", "gate", True, trace="x")])
    md = format_ab_markdown(compare_ab(arm, arm))
    assert "No cases flipped between arms" in md


# ── write_ab_comparison ────────────────────────────────────────────────────────


def test_write_ab_comparison_emits_json_and_md(tmp_path):
    json_path, md_path = write_ab_comparison(
        *_two_arms(), out_dir=tmp_path / "results", ts=9000, a_file="ab-A-9000.json", b_file="ab-B-9000.json"
    )
    assert json_path.name == "ab-compare-9000.json"
    assert md_path.name == "ab-compare-9000.md"
    payload = json.loads(json_path.read_text())
    assert payload["kind"] == "ab-compare"
    assert payload["tier_deltas"]["tracked"]["delta_passed"] == -1
    assert "Headline — gate" in md_path.read_text()


# ── the bad-memory fixture: a visible regression flip in arm B ──────────────────


def test_bad_memory_fixture_yields_a_pass_b_fail_flip():
    arm_a = json.loads((_FIXTURES / "ab-A-9000.json").read_text())
    arm_b = json.loads((_FIXTURES / "ab-B-9000.json").read_text())
    cmp = compare_ab(arm_a, arm_b)

    # The bad memory flips the region case A pass → B fail (a gate regression).
    assert [f["id"] for f in cmp["regressions"]] == ["memory_region_recall"]
    flip = cmp["regressions"][0]
    assert flip["direction"] == "regression"
    assert flip["tier"] == "gate"
    # Both runs' trace links are present so the two traces open side by side.
    assert flip["a_trace"] == "tr-A-region"
    assert flip["b_trace"] == "tr-B-region"
    # The harm shows in the headline gate delta too (3/3 → 2/3).
    assert cmp["headline"]["gate_delta_passed"] == -1
    assert cmp["tier_deltas"]["gate"]["a"] == [3, 3]
    assert cmp["tier_deltas"]["gate"]["b"] == [2, 3]


def test_bad_memory_fixture_is_internally_coherent():
    """The seeded false fact really does contradict the case the arm reports fail on,
    so the fixture is a faithful demonstration, not a rigged pair of numbers."""
    bad = json.loads((_FIXTURES / "bad_memory.json").read_text())
    arm_b = json.loads((_FIXTURES / "ab-B-9000.json").read_text())
    failed = next(r for r in arm_b["results"] if not r["passed"])
    assert bad["contradicts_case"] == failed["id"] == "memory_region_recall"
    # The false region appears in B's reply and in the seed; the true one is forbidden.
    assert bad["forbidden_answer"] in failed["raw"]["reply"]
    assert bad["forbidden_answer"] in bad["seed"]["kb_ingest"]["content"]
    assert bad["expected_answer"] not in failed["raw"]["reply"]


def test_bad_memory_fixture_renders_regression_in_markdown():
    arm_a = json.loads((_FIXTURES / "ab-A-9000.json").read_text())
    arm_b = json.loads((_FIXTURES / "ab-B-9000.json").read_text())
    md = format_ab_markdown(
        compare_ab(arm_a, arm_b), trace_link=lambda t: f"https://trace.example/{t}"
    )
    assert "Regressions (A pass" in md
    assert "`memory_region_recall`" in md
    assert "https://trace.example/tr-A-region" in md
    assert "https://trace.example/tr-B-region" in md


# ── the existing two-report compare is unchanged ────────────────────────────────


def test_compare_reports_behaviour_unchanged():
    """r4: adding the A/B functions must not touch the ordinary ``compare_reports``
    diff — same overall line, flip sections and per-category table as before."""

    def _plain(results):
        return {"total": len(results), "passed": sum(1 for r in results if r["passed"]), "results": results}

    old = _plain(
        [
            {"id": "a", "category": "tool", "name": "A", "passed": True, "detail": ""},
            {"id": "b", "category": "tool", "name": "B", "passed": False, "detail": ""},
            {"id": "c", "category": "a2a", "name": "C", "passed": True, "detail": ""},
        ]
    )
    new = _plain(
        [
            {"id": "a", "category": "tool", "name": "A", "passed": True, "detail": ""},
            {"id": "b", "category": "tool", "name": "B", "passed": True, "detail": ""},
            {"id": "c", "category": "a2a", "name": "C", "passed": False, "detail": ""},
        ]
    )
    md = compare_reports(old, new)
    assert "Newly passing" in md and "`b`" in md
    assert "Newly failing" in md and "`c`" in md
    assert "| tool |" in md and "| a2a |" in md
    # No flips → the dedicated message, exactly as before.
    assert "No cases flipped" in compare_reports(old, old)
