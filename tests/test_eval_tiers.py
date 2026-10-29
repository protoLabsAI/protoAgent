"""Tests for ADR 0126 E1b — gate/tracked eval tiers.

Covers tier resolution (both defaults + explicit override + invalid-tier load
error), the per-tier totals the runner stamps into a report, and the gate/tracked
split rendered by ``evals.report`` and ``evals.compare`` — including a legacy
report whose rows predate the ``tier`` field.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evals import runner
from evals.compare import compare_reports, resolve_tier
from evals.report import build_report


# ── tier resolution ───────────────────────────────────────────────────────────


def test_tier_defaults_to_gate_without_rubric():
    assert resolve_tier({"id": "x"}) == "gate"


def test_tier_defaults_to_tracked_with_rubric():
    assert resolve_tier({"id": "x", "verify_rubric": {"criteria": ["a"]}}) == "tracked"


def test_explicit_tier_overrides_the_rubric_default():
    # Explicit gate on a rubric case, and explicit tracked on a plain case.
    assert resolve_tier({"id": "x", "tier": "gate", "verify_rubric": {"criteria": ["a"]}}) == "gate"
    assert resolve_tier({"id": "x", "tier": "tracked"}) == "tracked"


def test_invalid_tier_is_a_load_error_naming_the_case():
    with pytest.raises(ValueError) as exc:
        resolve_tier({"id": "bogus_case", "tier": "nightly"})
    assert "bogus_case" in str(exc.value)
    assert "nightly" in str(exc.value)


def test_load_cases_rejects_an_unknown_tier(tmp_path):
    bad = tmp_path / "tasks.json"
    bad.write_text(json.dumps([{"id": "ok1"}, {"id": "ok2", "tier": "wat"}]))
    with pytest.raises(ValueError) as exc:
        runner.load_cases(bad)
    assert "ok2" in str(exc.value)


def test_load_cases_resolves_valid_tiers(tmp_path):
    good = tmp_path / "tasks.json"
    good.write_text(
        json.dumps(
            [
                {"id": "a", "kind": "ask", "prompt": "hi"},
                {"id": "b", "kind": "ask", "prompt": "hi", "verify_rubric": {"criteria": ["x"]}},
                {"id": "c", "kind": "ask", "prompt": "hi", "tier": "tracked"},
            ]
        )
    )
    cases = runner.load_cases(good)
    assert [c["id"] for c in cases] == ["a", "b", "c"]


# ── runner stamps + per-tier totals ────────────────────────────────────────────


def test_run_one_stamps_the_resolved_tier(monkeypatch):
    async def fake(client, case):
        return runner.CaseResult(case["id"], case["category"], case["name"], True, "ok")

    monkeypatch.setitem(runner._RUNNERS, "ask", fake)

    gate_case = {"id": "g", "category": "tool", "name": "G", "kind": "ask", "prompt": "hi"}
    tracked_case = {
        "id": "t",
        "category": "quality",
        "name": "T",
        "kind": "ask",
        "prompt": "hi",
        "verify_rubric": {"criteria": ["x"]},
    }
    assert asyncio.run(runner.run_one(None, gate_case)).tier == "gate"
    assert asyncio.run(runner.run_one(None, tracked_case)).tier == "tracked"


def test_tier_totals_excludes_skipped_and_computes_rate():
    results = [
        runner.CaseResult("a", "tool", "A", passed=True, detail="", tier="gate"),
        runner.CaseResult("b", "tool", "B", passed=True, detail="", tier="gate"),
        runner.CaseResult("c", "tool", "C", passed=False, detail="", tier="gate"),
        runner.CaseResult("d", "q", "D", passed=True, detail="", tier="tracked"),
        runner.CaseResult("e", "q", "E", passed=False, detail="", tier="tracked"),
        runner.CaseResult("f", "tool", "F", passed=True, detail="skip", skipped=True, tier="gate"),
    ]
    tiers = runner._tier_totals(results)
    assert tiers["gate"] == {"passed": 2, "total": 3, "pass_rate": round(2 / 3, 4)}
    assert tiers["tracked"] == {"passed": 1, "total": 2, "pass_rate": 0.5}


def test_save_report_writes_per_tier_totals(tmp_path):
    results = [
        runner.CaseResult("a", "tool", "A", passed=True, detail="", tier="gate"),
        runner.CaseResult("b", "tool", "B", passed=False, detail="", tier="gate"),
        runner.CaseResult("c", "q", "C", passed=True, detail="", tier="tracked"),
    ]
    out = tmp_path / "run-x.json"
    runner._save_report(results, out, model="m", base_url="http://x")
    rep = json.loads(out.read_text())
    assert rep["tiers"]["gate"] == {"passed": 1, "total": 2, "pass_rate": 0.5}
    assert rep["tiers"]["tracked"] == {"passed": 1, "total": 1, "pass_rate": 1.0}
    # Every report row carries its resolved tier.
    assert {r["id"]: r["tier"] for r in rep["results"]} == {"a": "gate", "b": "gate", "c": "tracked"}


def test_board_prints_gate_before_tracked(capsys):
    results = [
        runner.CaseResult("a", "tool", "A", passed=True, detail="ok", tier="gate"),
        runner.CaseResult("c", "q", "C", passed=True, detail="ok", tier="tracked"),
    ]
    runner._print_board(results)
    out = capsys.readouterr().out
    assert "gate:" in out and "tracked:" in out
    assert out.index("gate:") < out.index("tracked:")


# ── report / compare rendering of the split ────────────────────────────────────


def _row(cid, category, passed, *, tier=None, rubric=False):
    row = {"id": cid, "category": category, "name": cid.upper(), "passed": passed, "detail": ""}
    if tier is not None:
        row["tier"] = tier
    if rubric:
        row["verify_rubric"] = {"criteria": ["x"]}
    return row


def _report(ts, model, rows):
    return {
        "ts": ts,
        "model": model,
        "total": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "results": rows,
    }


def test_report_renders_gate_and_tracked_with_gate_headline():
    rows = [
        _row("g1", "tool", True, tier="gate"),
        _row("g2", "tool", False, tier="gate"),
        _row("t1", "quality", True, tier="tracked"),
    ]
    rep = _report("2026-10-20T00:00:00+00:00", "model/a", rows)
    md = build_report([rep])
    assert "Gate" in md and "Tracked" in md
    # Gate column precedes Tracked in both the leaderboard header and the trend.
    assert md.index("Gate") < md.index("Tracked")
    # Gate headline is 1/2; tracked is 1/1 — both surfaced, kept apart.
    assert "1/2" in md and "1/1" in md


def test_report_renders_a_legacy_report_with_no_tier_field():
    # Rows predate the tier field: no per-row tier, no tiers block. They must
    # still render — defaulting to gate — so the historical trend is unbroken.
    legacy = _report(
        "2026-08-19T00:00:00+00:00",
        "model/old",
        [_row("a", "tool", True), _row("b", "simple", False)],
    )
    md = build_report([legacy])
    assert "model/old" in md
    # The two legacy rows collapse into the gate tier (1 of 2 passed).
    assert "1/2" in md


def test_report_trend_mixes_legacy_and_tiered_runs():
    legacy = _report("2026-08-19T00:00:00+00:00", "m", [_row("a", "tool", True), _row("b", "tool", True)])
    tiered = _report(
        "2026-10-20T00:00:00+00:00",
        "m",
        [_row("a", "tool", True, tier="gate"), _row("b", "tool", False, tier="gate")],
    )
    md = build_report([legacy, tiered])
    # Trend arrow reflects the gate regression 2 → 1 between the two runs.
    assert "▼" in md


def test_compare_reports_changes_per_tier_gate_first():
    old = _report(
        "t0",
        "m",
        [
            _row("g1", "tool", True, tier="gate"),
            _row("g2", "tool", True, tier="gate"),
            _row("t1", "quality", True, tier="tracked"),
        ],
    )
    new = _report(
        "t1",
        "m",
        [
            _row("g1", "tool", True, tier="gate"),
            _row("g2", "tool", False, tier="gate"),  # gate regression
            _row("t1", "quality", True, tier="tracked"),
        ],
    )
    md = compare_reports(old, new)
    assert "| Tier |" in md
    # Gate row appears before the tracked row.
    assert md.index("gate") < md.index("tracked")
    # Gate went 2/2 → 1/2; tracked held at 1/1.
    assert "2/2" in md and "1/2" in md


def test_compare_handles_a_legacy_report_with_no_tier_field():
    legacy = _report("t0", "m", [_row("a", "tool", True), _row("b", "tool", False)])
    tiered = _report("t1", "m", [_row("a", "tool", True, tier="gate"), _row("b", "tool", True, tier="gate")])
    md = compare_reports(legacy, tiered)
    # Legacy rows default to gate, so the per-tier table still renders a gate row.
    assert "| Tier |" in md and "gate" in md
