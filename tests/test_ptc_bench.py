"""The PTC graduation bench harness (ADR 0103, #2807) — pure parts.

The live run needs an agent + model; these lock the harness itself: fixture
determinism, the two lanes' provability assertions, the telemetry join, and
the pre-registered verdict math.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evals import ptc_bench
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


def test_recent_response_unwrap_never_iterates_dict_keys():
    """The live run's crash: /api/telemetry/recent returns a DICT envelope
    ({enabled, turns, …}); falling back to the dict itself iterates its string
    keys and _lane_rows explodes. The unwrap must take the list or nothing."""
    from evals.ptc_bench import _lane_rows

    body = {
        "enabled": True,
        "turns": [{"session_id": "a2a:ptc-eval-loop-0", "llm_calls": 11}],
        "langfuse_trace_url_template": None,
    }
    turns = body.get("turns") or [] if isinstance(body, dict) else (body or [])
    assert _lane_rows(turns, "loop", 1)[0]["llm_calls"] == 11
    empty = {"enabled": False}
    turns2 = empty.get("turns") or [] if isinstance(empty, dict) else (empty or [])
    assert _lane_rows(turns2, "loop", 1) == []


def test_kernel_fixture_and_two_turn_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptc_bench", "fixtures", "--suite", "kernel", "--dir", str(tmp_path)])
    assert ptc_bench.main() == 0
    body = (tmp_path / "issues.json").read_bytes()
    ptc_bench.main()
    assert (tmp_path / "issues.json").read_bytes() == body
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugins/execute_code/protoagent.plugin.yaml").read_text())
    # Over output_truncate (so printing it into context is penalised), yet UNDER
    # the read_file one-call cap (so turn 1 loads the whole file in a single
    # bridged read, not a truncated head that would make the verdict measure
    # paging instead of session state — ADR 0134 D7 review).
    assert manifest["config"]["output_truncate"] < len(body) < ptc_bench._READ_FILE_ONE_CALL_CHARS
    records = json.loads(body)
    assert len(records) == 200
    # The ranking is unambiguous: the 5th-most-commented scheduler record is
    # strictly ahead of the 6th, so the top-5 set never turns on a tie-break.
    sched = sorted((r for r in records if r["subsystem"] == "scheduler"),
                   key=lambda r: (-len(r["comments"]), r["id"]))
    assert len(sched[4]["comments"]) > len(sched[5]["comments"])
    matches = [r for r in records if r["subsystem"] == "scheduler"]
    top = sorted(matches, key=lambda r: (-len(r["comments"]), r["id"]))[:5]
    answers = [f"MATCH_COUNT={len(matches)};", "\n".join(
        f"RANK={i};ID={r['id']};COMMENTS={len(r['comments'])};" for i, r in enumerate(top, 1)
    )]
    cases = ptc_bench.build_kernel_tasks("proj", 3, run_id="unique")
    assert len(cases) == 12
    contexts = set()
    for rep in range(3):
        fresh = cases[rep * 2:rep * 2 + 2]
        kernel = cases[6 + rep * 2:8 + rep * 2]
        assert [c["prompt"] for c in fresh] == [c["prompt"] for c in kernel]
        for lane in (fresh, kernel):
            assert lane[0]["context_id"] == lane[1]["context_id"]
            contexts.add(lane[0]["context_id"])
            for turn, case in enumerate(lane):
                assert case["expected_tools"] == ["execute_code"]
                assert "forbidden_tools" not in case
                for pattern in case["expected_patterns"]:
                    assert pattern in answers[turn]
                    assert pattern not in case["prompt"]
    assert len(contexts) == 6


def test_fixture_cap_constant_tracks_the_real_read_file_tool():
    """The fixture is sized to load in ONE bridged read_file call. Pin our cap
    constant to the tool's actual per-call char cap so a tool-side change can't
    silently let the fixture grow past a truncating read (ADR 0134 D7 review)."""
    fs_tools = pytest.importorskip("tools.fs_tools")
    assert ptc_bench._READ_FILE_ONE_CALL_CHARS == fs_tools._MAX_READ_CHARS


@pytest.mark.parametrize("ratio,fresh_pass,kernel_pass,turns,expected", [
    (0.80, (6, 6), (6, 6), 6, True),
    (0.85, (6, 6), (6, 6), 6, True),
    (0.90, (6, 6), (6, 6), 6, False),
    (0.80, (6, 6), (5, 6), 6, False),
    (0.80, (4, 6), (5, 6), 6, False),
    (0.80, (6, 6), (6, 6), 5, False),
    (0.80, (2, 2), (2, 2), 6, False),
    (0.0, (6, 6), (6, 6), 6, False),
])
def test_kernel_thresholds(ratio, fresh_pass, kernel_pass, turns, expected, capsys):
    fresh = _agg([{"input_tokens": 1000}] * 6)
    kernel = _agg([{"input_tokens": int(1000 * ratio)}] * turns)
    v = ptc_bench.kernel_verdict(fresh, kernel, fresh_pass, kernel_pass, 3)
    assert v["graduated"] is expected
    ptc_bench.print_kernel_verdict(v)
    output = capsys.readouterr().out
    assert f"VERDICT: {'PASS' if expected else 'FAIL'}" in output
    for label, op, threshold, value, passed in v["checks"]:
        assert f"{label}: need {op}{threshold:.2f}, measured {value:.4f} — {'PASS' if passed else 'FAIL'}" in output


@pytest.mark.parametrize("telemetry_problem", [None, "missing", "duplicate", "unavailable"])
def test_kernel_cli_routes_lanes_and_joins_only_current_run(tmp_path, monkeypatch, capsys, telemetry_problem):
    import httpx

    monkeypatch.setattr(ptc_bench, "__file__", str(tmp_path / "ptc_bench.py"))
    monkeypatch.setattr("sys.argv", ["ptc_bench", "run", "--suite", "kernel", "--project", "proj",
                                    "--reps", "3", "--fresh-base-url", "http://fresh",
                                    "--kernel-base-url", "http://kernel", "--fresh-audit-path", "fresh.jsonl",
                                    "--kernel-audit-path", "kernel.jsonl"])
    telemetry = {}

    def runner(command, *, env):
        url = command[command.index("--base-url") + 1]
        lane = url.removeprefix("http://")
        assert env["AUDIT_PATH"] == f"{lane}.jsonl"
        cases = json.loads(Path(command[command.index("--tasks-file") + 1]).read_text(encoding="utf-8"))
        assert {c["category"] for c in cases} == {f"ptc-{lane}"}
        Path(command[command.index("--out") + 1]).write_text(json.dumps({
            "results": [{"category": c["category"], "passed": True} for c in cases]
        }), encoding="utf-8")
        telemetry[url] = [{"session_id": "a2a:" + c["context_id"],
                           "input_tokens": 1000 if lane == "fresh" else 800} for c in cases]
        telemetry[url].append({"session_id": f"a2a:ptc-eval-old-{lane}-0", "input_tokens": 999999})
        return 0

    def recent(url, **kwargs):
        rows = telemetry[url.removesuffix("/api/telemetry/recent")]
        if "kernel" in url:
            if telemetry_problem == "missing":
                rows.pop(0)
            elif telemetry_problem == "duplicate":
                rows[0] = rows[2]
            elif telemetry_problem == "unavailable":
                raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"turns": telemetry[url.removesuffix("/api/telemetry/recent")]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(ptc_bench.subprocess, "call", runner)
    monkeypatch.setattr(httpx, "get", recent)
    assert ptc_bench.main() == (1 if telemetry_problem else 0)
    output = capsys.readouterr().out
    assert f"VERDICT: {'FAIL' if telemetry_problem else 'PASS'}" in output
    if not telemetry_problem:
        assert "measured 0.8000 — PASS" in output
