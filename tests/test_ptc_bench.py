"""The PTC graduation bench harness (ADR 0103, #2807) — pure parts.

The live run needs an agent + model; these lock the harness itself: fixture
determinism, the two lanes' provability assertions, the telemetry join, and
the pre-registered verdict math.
"""

from __future__ import annotations

from evals.ptc_bench import (
    _SIZES,
    RQ_ROUNDS,
    _agg,
    _lane_rows,
    build_tasks,
    verdict,
    write_fixtures,
)


def test_fixtures_are_deterministic_and_carry_the_labeled_answer(tmp_path):
    names = write_fixtures(tmp_path, 10)
    assert names == [f"f{i}.txt" for i in range(10)]
    body = (tmp_path / "f3.txt").read_text()
    assert body.startswith(f"SIZE: {_SIZES[3]}\n")  # the answer is labeled, not counted
    # Re-writing produces identical bytes — reruns are comparable.
    again = (tmp_path / "f3.txt").read_text()
    write_fixtures(tmp_path, 10)
    assert (tmp_path / "f3.txt").read_text() == again


def test_lanes_are_provable_not_just_prompted():
    cases = build_tasks("proj", 10, reps=2)
    assert len(cases) == 4
    loop = [c for c in cases if c["category"] == "ptc-loop"]
    code = [c for c in cases if c["category"] == "ptc-code"]
    # Loop: execute_code must NOT fire; code: the BRIDGE must provably fire
    # (the ptc: audit prefix from ADR 0103 S3) and the direct tool must not.
    assert all(c["forbidden_tools"] == ["execute_code"] for c in loop)
    assert all("ptc:read_file" in c["expected_tools"] for c in code)
    assert all(c["forbidden_tools"] == ["read_file"] for c in code)
    # Correctness is symmetric: both lanes must surface every labeled size.
    for c in cases:
        assert c["expected_patterns"] == [str(s) for s in _SIZES]
        assert c["context_id"].startswith("ptc-eval-")  # the telemetry join key


def test_lane_rows_join_by_pinned_session_suffix():
    turns = [
        {"session_id": "a2a:ptc-eval-loop-0", "llm_calls": 12},
        {"session_id": "a2a:ptc-eval-code-0", "llm_calls": 2},
        {"session_id": "a2a:unrelated", "llm_calls": 99},
    ]
    assert [t["llm_calls"] for t in _lane_rows(turns, "loop", 1)] == [12]
    assert [t["llm_calls"] for t in _lane_rows(turns, "code", 1)] == [2]


def test_verdict_applies_the_preregistered_thresholds():
    loop = _agg([{"llm_calls": 12, "input_tokens": 300_000, "cost_usd": 3.0, "duration_ms": 120_000}])
    code = _agg([{"llm_calls": 2, "input_tokens": 60_000, "cost_usd": 0.5, "duration_ms": 30_000}])
    v = verdict(loop, code, loop_pass=(2, 2), code_pass=(2, 2))
    assert v["rounds_x"] == 6.0 and v["graduated"] is True

    # A cheaper-but-wrong code lane NEVER graduates — the whole point of the
    # eval-verifier framing over a stopwatch script.
    v2 = verdict(loop, code, loop_pass=(2, 2), code_pass=(1, 2))
    assert v2["graduated"] is False

    # Below the pre-registered rounds collapse → not graduated, however cheap.
    weak_code = _agg([{"llm_calls": 12 / (RQ_ROUNDS - 1), "input_tokens": 1, "cost_usd": 0, "duration_ms": 1}])
    v3 = verdict(loop, weak_code, loop_pass=(2, 2), code_pass=(2, 2))
    assert v3["graduated"] is False
