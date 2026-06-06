"""Tests for the model-comparison eval tooling (sweep + trend + tagging).

These exercise the pure rendering/aggregation logic and the model-tag wiring
without booting an agent — the live boot path is covered manually via
``python -m evals.sweep``.
"""

from __future__ import annotations

import json


from evals.report import build_report
from evals.runner import CaseResult, _save_report
from evals.sweep import (
    _aggregate_runs,
    _majority,
    _render_matrix,
    _render_repeat_matrix,
    _slug,
)


def _fake_report(model: str, *, ts: str, rows: list[tuple[str, str, bool]]) -> dict:
    """rows: (id, category, passed)."""
    return {
        "ts": ts,
        "model": model,
        "total": len(rows),
        "passed": sum(1 for _, _, p in rows if p),
        "results": [
            {"id": i, "category": c, "name": i, "passed": p, "detail": "", "duration_ms": 100, "tokens": 50}
            for i, c, p in rows
        ],
    }


# ── model-swap env override ──────────────────────────────────────────────────


def test_protoagent_model_env_overrides_yaml(monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.setenv("PROTOAGENT_MODEL", "vendor/some-test-model")
    cfg = LangGraphConfig.from_yaml("config/langgraph-config.yaml")
    assert cfg.model_name == "vendor/some-test-model"


def test_yaml_model_used_when_env_unset(monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.delenv("PROTOAGENT_MODEL", raising=False)
    cfg = LangGraphConfig.from_yaml("config/langgraph-config.yaml")
    # Whatever the YAML/default is, it must not be the env sentinel above.
    assert cfg.model_name and cfg.model_name != "vendor/some-test-model"


# ── report tagging ───────────────────────────────────────────────────────────


def test_save_report_tags_model_and_base_url(tmp_path):
    out = tmp_path / "run.json"
    _save_report(
        [CaseResult("c1", "tool", "n", True, "OK")],
        out, model="vendor/m1", base_url="http://x:7990",
    )
    payload = json.loads(out.read_text())
    assert payload["model"] == "vendor/m1"
    assert payload["base_url"] == "http://x:7990"


# ── sweep matrix ─────────────────────────────────────────────────────────────


def test_slug_is_filesystem_safe():
    assert _slug("protolabs/reasoning") == "protolabs-reasoning"
    assert "/" not in _slug("a/b:c")


# ── best-of-N (--repeat) ─────────────────────────────────────────────────────


def test_majority_threshold():
    assert _majority(1) == 1
    assert _majority(2) == 2
    assert _majority(3) == 2
    assert _majority(5) == 3


def test_aggregate_runs_counts_passes_per_case():
    runs = [
        _fake_report("m", ts="1", rows=[("a", "tool", True), ("b", "tool", False)]),
        _fake_report("m", ts="2", rows=[("a", "tool", True), ("b", "tool", True)]),
        _fake_report("m", ts="3", rows=[("a", "tool", False), ("b", "tool", True)]),
    ]
    agg = _aggregate_runs(runs)
    assert agg["a"] == (2, 3)  # passed 2 of 3
    assert agg["b"] == (2, 3)


def test_repeat_matrix_majority_pass_and_ranking():
    # m1: a 3/3, b 2/3 → both clear majority (2/3) → best-of-3 = 2/2.
    # m2: a 1/3 (fails majority), b 3/3 → best-of-3 = 1/2.
    m1 = [
        _fake_report("m1", ts=str(i), rows=[("a", "tool", True), ("b", "tool", i != 2)])
        for i in range(3)
    ]
    m2 = [
        _fake_report("m2", ts=str(i), rows=[("a", "tool", i == 0), ("b", "tool", True)])
        for i in range(3)
    ]
    md = _render_repeat_matrix({"m1": m1, "m2": m2}, repeat=3)
    assert "best-of-3" in md
    assert "**2/2**" in md and "**1/2**" in md      # per-model best-of-N scores
    assert md.index("m1") < md.index("m2")          # m1 ranked first
    assert "1/3 ✗" in md                            # m2's case `a` flagged as majority-fail
    assert "3/3" in md and "2/3" in md


def test_render_matrix_ranks_best_model_first():
    reports = {
        "good": _fake_report("good", ts="2026-06-01T00:00:00", rows=[("a", "tool", True), ("b", "tool", True)]),
        "bad": _fake_report("bad", ts="2026-06-01T00:00:00", rows=[("a", "tool", False), ("b", "tool", False)]),
    }
    md = _render_matrix(reports)
    assert "# Model sweep" in md
    # Best model (good) appears above the worse one in the leaderboard.
    assert md.index("`good`") < md.index("`bad`")
    assert "tool" in md and "Overall" in md


# ── trend report ─────────────────────────────────────────────────────────────


def test_build_report_leaderboard_and_trend():
    runs = [
        _fake_report("m1", ts="2026-06-01T00:00:00", rows=[("a", "tool", True), ("b", "simple", False)]),
        _fake_report("m1", ts="2026-06-02T00:00:00", rows=[("a", "tool", True), ("b", "simple", True)]),
        _fake_report("m2", ts="2026-06-02T00:00:00", rows=[("a", "tool", False), ("b", "simple", False)]),
    ]
    md = build_report(runs)
    assert "Leaderboard" in md and "Trend" in md
    # m1's latest (2/2) beats m2 (0/2) → ranked first.
    assert md.index("`m1`") < md.index("`m2`")
    # Trend shows the improvement arrow for m1's second run.
    assert "▲" in md


def test_build_report_filters_to_one_model():
    runs = [
        _fake_report("m1", ts="2026-06-01T00:00:00", rows=[("a", "tool", True)]),
        _fake_report("m2", ts="2026-06-01T00:00:00", rows=[("a", "tool", True)]),
    ]
    md = build_report(runs, only_model="m1")
    assert "`m1`" in md and "`m2`" not in md


def test_build_report_handles_no_runs():
    assert "No model-tagged" in build_report([])
