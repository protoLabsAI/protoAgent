"""Tests for scripts/evals_nightly.py — the regression → friction + flake logic.

Fakes only, no network and no sweep boot: these exercise the pure analysis
(``gate_regressions`` / ``flaky_gate_cases``), the friction write through an
injected recorder, and the nightly report marking/loading. The live sweep path
is covered manually via ``python scripts/evals_nightly.py`` (see the PR body and
docs/guides/evals.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "evals_nightly.py"


def _load():
    spec = importlib.util.spec_from_file_location("_evals_nightly_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


nightly = _load()


def _row(cid: str, passed: bool, *, tier: str = "gate", trace_id: str = "", skipped: bool = False) -> dict:
    """One report row in the shape evals.runner writes."""
    row = {
        "id": cid,
        "category": "tool",
        "name": cid,
        "passed": passed,
        "detail": "OK" if passed else "boom",
        "tier": tier,
    }
    if trace_id:
        row["trace_id"] = trace_id
    if skipped:
        row["skipped"] = True
    return row


def _report(rows: list[dict], *, ts: str = "2026-10-29T00:00:00", nightly_ts: int = 0) -> dict:
    rep = {
        "ts": ts,
        "model": "vendor/m",
        "total": len(rows),
        "passed": sum(1 for r in rows if r.get("passed") and not r.get("skipped")),
        "results": rows,
    }
    if nightly_ts:
        rep["nightly"] = True
        rep["nightly_ts"] = nightly_ts
    return rep


class _FakeRecorder:
    """Stand-in for the friction plugin's record_service; captures every call."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, kind, summary, detail="", severity="minor", *, source="", session_id="", trace_id=""):
        self.calls.append(
            {
                "kind": kind,
                "summary": summary,
                "detail": detail,
                "severity": severity,
                "source": source,
                "trace_id": trace_id,
            }
        )
        return f"{kind}:{summary}"  # record_service returns the grouped row id


def _fake_link(trace_id: str) -> str:
    return f"https://lf.example/project/p/traces/{trace_id}" if trace_id else ""


# ── regression → friction ─────────────────────────────────────────────────────


def test_pass_to_fail_records_exactly_one_friction_per_case():
    prev = _report([_row("calc", True), _row("search", True)])
    curr = _report([_row("calc", False, trace_id="tr-calc"), _row("search", True)])

    rec = _FakeRecorder()
    written = nightly.record_regressions(
        nightly.gate_regressions(prev, curr), "evals/results/run-nightly-9.json",
        recorder=rec, trace_link=_fake_link,
    )

    assert written == ["calc"]
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["kind"] == "harness"
    assert call["source"] == "evals-nightly"
    # Stable summary — no dates/counts, so the ledger groups it to one row per case.
    assert call["summary"] == "nightly gate regression: calc"
    # Detail carries the report path AND the trace link built from the row's trace_id.
    assert "run-nightly-9.json" in call["detail"]
    assert "https://lf.example/project/p/traces/tr-calc" in call["detail"]


def test_summary_is_stable_across_nights_for_the_same_case():
    # Two different nights, same regressed case → identical summary (ledger groups it).
    curr1 = _report([_row("calc", False, trace_id="a")])
    curr2 = _report([_row("calc", False, trace_id="b")])
    prev = _report([_row("calc", True)])
    s1 = nightly.record_regressions(nightly.gate_regressions(prev, curr1), "r1", recorder=_FakeRecorder(), trace_link=_fake_link)
    s2 = nightly.record_regressions(nightly.gate_regressions(prev, curr2), "r2", recorder=_FakeRecorder(), trace_link=_fake_link)
    assert s1 == s2 == ["calc"]


def test_record_returns_empty_when_recorder_unavailable(monkeypatch):
    # Friction plugin not importable → record nothing, but this must NOT be read as
    # "no regressions" by the caller (main() gates on the regressions found, not this).
    monkeypatch.setattr(nightly, "_friction_recorder", lambda: None)
    prev = _report([_row("calc", True)])
    curr = _report([_row("calc", False)])
    assert nightly.record_regressions(nightly.gate_regressions(prev, curr), "r") == []


def test_record_skips_cases_the_recorder_rejects():
    # record_service returns "" when the write is rejected/fails; that case is attempted
    # but not counted as recorded, so the caller can't claim a write that never landed.
    prev = _report([_row("calc", True), _row("search", True)])
    curr = _report([_row("calc", False), _row("search", False)])

    class _RejectingRecorder:
        def __init__(self):
            self.calls = 0

        def __call__(self, *a, **k):
            self.calls += 1
            return "" if self.calls == 1 else "harness:row"  # first write rejected

    rec = _RejectingRecorder()
    written = nightly.record_regressions(
        nightly.gate_regressions(prev, curr), "r", recorder=rec, trace_link=_fake_link
    )
    assert rec.calls == 2  # both regressions attempted
    assert written == ["search"]  # only the one the ledger took counts (calc rejected)


def test_pass_to_pass_records_nothing():
    prev = _report([_row("calc", True)])
    curr = _report([_row("calc", True)])
    rec = _FakeRecorder()
    assert nightly.record_regressions(nightly.gate_regressions(prev, curr), "r", recorder=rec, trace_link=_fake_link) == []
    assert rec.calls == []


def test_fail_to_fail_records_nothing():
    # Still broken, but it didn't regress THIS night — no new friction.
    prev = _report([_row("calc", False)])
    curr = _report([_row("calc", False)])
    rec = _FakeRecorder()
    assert nightly.record_regressions(nightly.gate_regressions(prev, curr), "r", recorder=rec, trace_link=_fake_link) == []
    assert rec.calls == []


def test_tracked_tier_flip_records_nothing():
    # A tracked case that flips pass→fail must not gate (ADR 0126 D1).
    prev = _report([_row("judge", True, tier="tracked")])
    curr = _report([_row("judge", False, tier="tracked")])
    rec = _FakeRecorder()
    assert nightly.gate_regressions(prev, curr) == []
    assert nightly.record_regressions(nightly.gate_regressions(prev, curr), "r", recorder=rec, trace_link=_fake_link) == []
    assert rec.calls == []


def test_skipped_case_is_not_a_regression():
    # A case gated behind an unset requires_env is SKIP, not a fail.
    prev = _report([_row("acp", True)])
    curr = _report([_row("acp", False, skipped=True)])
    assert nightly.gate_regressions(prev, curr) == []


def test_new_case_this_night_is_not_a_regression():
    prev = _report([_row("calc", True)])
    curr = _report([_row("calc", True), _row("brand_new", False)])
    assert nightly.gate_regressions(prev, curr) == []


def test_deliberately_broken_gate_case_yields_exactly_one_entry():
    # One gate case passed last night and fails now; everything else holds.
    prev = _report([_row("calc", True), _row("search", True), _row("time", True)])
    curr = _report([_row("calc", True), _row("search", False, trace_id="x"), _row("time", True)])
    rec = _FakeRecorder()
    written = nightly.record_regressions(nightly.gate_regressions(prev, curr), "r", recorder=rec, trace_link=_fake_link)
    assert written == ["search"]
    assert len(rec.calls) == 1


def test_record_without_trace_id_omits_the_link():
    prev = _report([_row("calc", True)])
    curr = _report([_row("calc", False)])  # no trace_id
    rec = _FakeRecorder()
    nightly.record_regressions(nightly.gate_regressions(prev, curr), "evals/results/x.json", recorder=rec, trace_link=_fake_link)
    assert "report:" in rec.calls[0]["detail"]
    assert "trace:" not in rec.calls[0]["detail"]


# ── flake detection ────────────────────────────────────────────────────────────


def _seq_reports(cid_to_seq: dict[str, list[bool]], *, tier: str = "gate") -> list[dict]:
    """Build one report per column across the given per-case pass/fail sequences."""
    n = max(len(s) for s in cid_to_seq.values())
    reports = []
    for i in range(n):
        rows = [_row(cid, seq[i], tier=tier) for cid, seq in cid_to_seq.items() if i < len(seq)]
        reports.append(_report(rows, nightly_ts=i + 1))
    return reports


def test_flake_detection_two_flips_in_seven():
    # 7 nightlies. `flaky` flips pass/fail twice (P,F,P,…); `stable` never flips.
    reports = _seq_reports(
        {
            "flaky": [True, False, True, True, True, True, True],   # 2 flips
            "stable": [True, True, True, True, True, True, True],   # 0 flips
        }
    )
    flakes = nightly.flaky_gate_cases(reports)
    assert ("flaky", 2) in flakes
    assert all(cid != "stable" for cid, _ in flakes)


def test_one_flip_is_a_regression_not_a_flake():
    # P,P,…,F: regressed and stayed failing — one flip, below the threshold.
    reports = _seq_reports({"calc": [True, True, True, True, True, True, False]})
    assert nightly.flaky_gate_cases(reports) == []


def test_flake_window_only_looks_at_last_seven():
    # An early flip falls out of the 7-report window; only the recent run is stable.
    reports = _seq_reports(
        {"calc": [True, False, True, True, True, True, True, True, True, True]}  # flips are in the first 3
    )
    # Last 7 are all True → 0 flips in-window.
    assert nightly.flaky_gate_cases(reports) == []


def test_tracked_flakes_are_ignored():
    reports = _seq_reports({"judge": [True, False, True, False, True, False, True]}, tier="tracked")
    assert nightly.flaky_gate_cases(reports) == []


def test_format_flakes_carries_the_advice():
    out = nightly.format_flakes([("calc", 3)])
    assert "calc" in out
    assert "demote to tracked or fix" in out


def test_format_flakes_empty_when_none():
    assert nightly.format_flakes([]) == ""


# ── report marking / loading ────────────────────────────────────────────────────


def test_mark_nightly_renames_and_stamps(tmp_path):
    sweep = tmp_path / "run-sweep-1730000000-vendor-m.json"
    sweep.write_text(json.dumps(_report([_row("calc", True)])), encoding="utf-8")

    dest = nightly.mark_nightly(sweep, 1730000000, tmp_path)

    assert dest.name == "run-nightly-1730000000.json"
    assert not sweep.exists()  # renamed, not copied — no double count in evals.report
    body = json.loads(dest.read_text())
    assert body["nightly"] is True
    assert body["nightly_ts"] == 1730000000


def test_load_nightly_reports_ignores_adhoc_runs(tmp_path):
    # A marked nightly, an unmarked run-sweep, and a plain run-*.json.
    (tmp_path / "run-nightly-100.json").write_text(
        json.dumps(_report([_row("calc", True)], nightly_ts=100)), encoding="utf-8"
    )
    (tmp_path / "run-sweep-200-vendor-m.json").write_text(
        json.dumps(_report([_row("calc", False)])), encoding="utf-8"
    )
    (tmp_path / "run-1234.json").write_text(json.dumps(_report([_row("calc", False)])), encoding="utf-8")

    loaded = nightly.load_nightly_reports(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["_file"] == "run-nightly-100.json"


def test_load_nightly_reports_sorted_oldest_first(tmp_path):
    for nts in (300, 100, 200):
        (tmp_path / f"run-nightly-{nts}.json").write_text(
            json.dumps(_report([_row("calc", True)], nightly_ts=nts)), encoding="utf-8"
        )
    loaded = nightly.load_nightly_reports(tmp_path)
    assert [r["nightly_ts"] for r in loaded] == [100, 200, 300]


# ── main() gating (offline: the sweep and the friction recorder are faked) ───────


def _seed_nightly(dirp: Path, rows: list[dict], *, nightly_ts: int) -> None:
    (dirp / f"run-nightly-{nightly_ts}.json").write_text(
        json.dumps(_report(rows, nightly_ts=nightly_ts)), encoding="utf-8"
    )


def _fake_sweep(dirp: Path, rows: list[dict]):
    """A run_sweep stand-in: writes one run-sweep-*.json (as evals.sweep would, into the
    same results dir) and returns its path — no agent boot, no subprocess, no network."""

    def run_sweep(model, *, port_base, results_dir):
        p = results_dir / "run-sweep-999-vendor-m.json"
        p.write_text(json.dumps(_report(rows)), encoding="utf-8")
        return p

    return run_sweep


def test_main_exits_one_on_regression_even_when_friction_unavailable(tmp_path, monkeypatch, capsys):
    # The gate verdict must be the regressions FOUND, not the ids recorded: a real gate
    # regression with the friction plugin unreachable still fails the night (exit 1) and
    # must never print "no gate regressions".
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nightly, "_default_trace_link", _fake_link)
    _seed_nightly(tmp_path, [_row("calc", True)], nightly_ts=1)
    monkeypatch.setattr(nightly, "run_sweep", _fake_sweep(tmp_path, [_row("calc", False, trace_id="tr")]))
    monkeypatch.setattr(nightly, "_friction_recorder", lambda: None)  # plugin not importable

    rc = nightly.main(["--model", "vendor/m"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "no gate regressions" not in out
    assert "REGRESSED calc" in out


def test_main_records_one_friction_and_exits_one(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nightly, "_default_trace_link", _fake_link)
    _seed_nightly(tmp_path, [_row("calc", True), _row("search", True)], nightly_ts=1)
    monkeypatch.setattr(
        nightly, "run_sweep", _fake_sweep(tmp_path, [_row("calc", False, trace_id="tr-calc"), _row("search", True)])
    )
    rec = _FakeRecorder()
    monkeypatch.setattr(nightly, "_friction_recorder", lambda: rec)

    rc = nightly.main(["--model", "vendor/m"])

    assert rc == 1
    assert len(rec.calls) == 1
    assert rec.calls[0]["summary"] == "nightly gate regression: calc"
    assert rec.calls[0]["source"] == "evals-nightly"
    assert "https://lf.example/project/p/traces/tr-calc" in rec.calls[0]["detail"]


def test_main_exits_zero_and_records_nothing_when_gate_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    _seed_nightly(tmp_path, [_row("calc", True)], nightly_ts=1)
    monkeypatch.setattr(nightly, "run_sweep", _fake_sweep(tmp_path, [_row("calc", True)]))
    rec = _FakeRecorder()
    monkeypatch.setattr(nightly, "_friction_recorder", lambda: rec)

    rc = nightly.main(["--model", "vendor/m"])

    assert rc == 0
    assert rec.calls == []


def test_main_first_run_records_a_baseline_and_exits_zero(tmp_path, monkeypatch):
    # No previous nightly to compare → baseline only, nothing gated or recorded.
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nightly, "run_sweep", _fake_sweep(tmp_path, [_row("calc", False, trace_id="t")]))
    rec = _FakeRecorder()
    monkeypatch.setattr(nightly, "_friction_recorder", lambda: rec)

    rc = nightly.main(["--model", "vendor/m"])

    assert rc == 0
    assert rec.calls == []


def test_main_exits_one_when_sweep_produced_no_report(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nightly, "run_sweep", lambda *a, **k: None)
    assert nightly.main(["--model", "vendor/m"]) == 1


# ── where regression friction lands (--friction-log) ──────────────────────────


def test_sweep_env_never_carries_friction_log(monkeypatch):
    monkeypatch.setenv("FRICTION_LOG", "/somewhere/friction.jsonl")
    env = nightly.sweep_env()
    assert "FRICTION_LOG" not in env
    assert env.get("PATH") == __import__("os").environ.get("PATH")


def test_friction_log_routes_regressions_and_never_reaches_the_sweep(tmp_path, monkeypatch):
    """The nightly's regression lands in the named ledger (e.g. a desktop member's), and
    the throwaway sweep agent never sees FRICTION_LOG, so its own friction can't."""
    import os

    # main() sets os.environ["FRICTION_LOG"] itself. Register the variable with monkeypatch
    # first (set, then delete) so teardown removes it: a bare delenv of an unset variable
    # records nothing to undo, and the leaked ledger path put this test's regression into
    # later tests' working state (test_projection goldens).
    monkeypatch.setenv("FRICTION_LOG", "unset-by-teardown")
    monkeypatch.delenv("FRICTION_LOG")
    monkeypatch.setattr(nightly, "_RESULTS_DIR", tmp_path)
    (tmp_path / "run-nightly-100.json").write_text(
        json.dumps(_report([_row("calc", True)], nightly_ts=100)), encoding="utf-8"
    )
    seen_at_sweep: list[str | None] = []

    def fake_sweep(model, *, port_base, results_dir):
        seen_at_sweep.append(os.environ.get("FRICTION_LOG"))
        out = results_dir / "run-sweep-200-vendor-m.json"
        out.write_text(json.dumps(_report([_row("calc", False)])), encoding="utf-8")
        return out

    monkeypatch.setattr(nightly, "run_sweep", fake_sweep)
    ledger = tmp_path / "member" / "friction" / "friction.jsonl"

    rc = nightly.main(["--model", "vendor/m", "--friction-log", str(ledger)])

    assert rc == 1  # the gate regressed
    assert seen_at_sweep == [None]
    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [e["summary"] for e in entries] == ["nightly gate regression: calc"]
    assert entries[0]["kind"] == "harness"
