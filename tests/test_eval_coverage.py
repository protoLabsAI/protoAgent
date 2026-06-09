"""Tests for the eval coverage slice (ADR 0012 §2.5):
LLM-judge rubric + workflow-case runner + the new tasks.json cases.

The grader call and the workflow run are both mocked — the live paths run only
against a real agent via ``python -m evals.runner``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals import judge, runner, verify

TASKS = json.loads((Path(__file__).parent.parent / "evals" / "tasks.json").read_text())


# ── judge parsing ─────────────────────────────────────────────────────────────


def test_judge_parses_verdict_and_scores_fraction(monkeypatch):
    criteria = ["A", "B", "C"]
    monkeypatch.setattr(judge, "_invoke_grader", lambda prompt, model: json.dumps({
        "criteria": [
            {"criterion": "A", "met": True, "why": "yes"},
            {"criterion": "B", "met": False, "why": "no"},
            {"criterion": "C", "met": True, "why": "yes"},
        ]
    }))
    res = judge.score_rubric("some output", criteria)
    assert res.score == pytest.approx(2 / 3)
    assert res.met == {"A": True, "B": False, "C": True}
    assert res.error is None


def test_judge_tolerates_code_fenced_json(monkeypatch):
    monkeypatch.setattr(judge, "_invoke_grader", lambda p, m:
                        '```json\n{"criteria": [{"criterion": "A", "met": true}]}\n```')
    res = judge.score_rubric("o", ["A"])
    assert res.score == 1.0


def test_judge_reports_error_on_garbage(monkeypatch):
    monkeypatch.setattr(judge, "_invoke_grader", lambda p, m: "I cannot comply.")
    res = judge.score_rubric("o", ["A"])
    assert res.score == 0.0 and res.error


def test_judge_never_raises_on_grader_failure(monkeypatch):
    def boom(prompt, model):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(judge, "_invoke_grader", boom)
    res = judge.score_rubric("o", ["A"])
    assert res.score == 0.0 and "gateway down" in res.error


def test_empty_rubric_is_a_pass():
    assert judge.score_rubric("o", []).score == 1.0


# ── runner rubric wiring ───────────────────────────────────────────────────────


def test_check_rubric_passes_at_threshold(monkeypatch):
    monkeypatch.setattr(
        judge, "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=0.8, met={"a": True}),
    )
    case = {"verify_rubric": {"criteria": ["a"], "threshold": 0.66}}
    assert runner._check_rubric(case, "out") == []


def test_check_rubric_fails_below_threshold(monkeypatch):
    monkeypatch.setattr(
        judge, "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=0.4, met={"a": False}),
    )
    case = {"verify_rubric": {"criteria": ["a"], "threshold": 0.75}}
    problems = runner._check_rubric(case, "out")
    assert problems and "rubric" in problems[0]


def test_check_rubric_noop_without_block():
    assert runner._check_rubric({}, "out") == []


# ── any-tool assertion ─────────────────────────────────────────────────────────


def _audit(*names):
    return [{"tool": n, "success": True} for n in names]


def test_assert_any_tool_fired_matches_one():
    ok, _ = verify.assert_any_tool_fired(_audit("run_workflow", "web_search"), ["task", "run_workflow"])
    assert ok


def test_assert_any_tool_fired_none_matches():
    ok, detail = verify.assert_any_tool_fired(_audit("web_search"), ["task", "run_workflow"])
    assert not ok and "none of" in detail


def test_assert_any_tool_requires_success_when_asked():
    entries = [{"tool": "task", "success": False}]
    assert not verify.assert_any_tool_fired(entries, ["task"], require_success=True)[0]
    assert verify.assert_any_tool_fired(entries, ["task"], require_success=False)[0]


# ── workflow case runner ───────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, output):
        self._output = output
        self.called = None

    async def run_workflow(self, name, inputs, *, timeout_s=300):
        self.called = (name, inputs)
        return {"output": self._output}


def test_workflow_case_passes_on_pattern_and_rubric(monkeypatch):
    monkeypatch.setattr(
        judge, "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=1.0),
    )
    client = _FakeClient("# Report\n\n## Counterpoints & caveats\nthings [1]")
    case = {
        "id": "wf", "category": "workflow", "kind": "workflow",
        "name": "wf", "workflow": "deep-research", "inputs": {"topic": "x"},
        "expected_patterns": ["counterpoint"],
        "verify_rubric": {"criteria": ["balanced"], "threshold": 0.75},
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail
    assert client.called[0] == "deep-research"


def test_workflow_case_fails_on_missing_pattern():
    client = _FakeClient("a report with no opposing view")
    case = {
        "id": "wf", "category": "workflow", "kind": "workflow", "name": "wf",
        "workflow": "deep-research", "inputs": {}, "expected_patterns": ["counterpoint"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert not res.passed and "counterpoint" in res.detail


def test_workflow_case_fails_on_empty_output():
    res = asyncio.run(runner._run_workflow_case(_FakeClient("  "), {
        "id": "wf", "category": "workflow", "kind": "workflow", "name": "wf",
        "workflow": "research-and-brief", "inputs": {},
    }))
    assert not res.passed and "empty" in res.detail


def test_workflow_case_asserts_expected_tool_fired(monkeypatch):
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("backtest_strategy"))
    client = _FakeClient("# Desk call\nGO — Sharpe 1.2, beat buy-and-hold.")
    case = {
        "id": "wf", "category": "workflow", "kind": "workflow", "name": "wf",
        "workflow": "quant-desk", "inputs": {"idea": "x"},
        "expected_tools": ["backtest_strategy"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail


def test_workflow_case_fails_when_expected_tool_missing(monkeypatch):
    # A step that writes/describes code instead of calling the tool: the output
    # reads fine, but the tool never fired in the audit log — must fail.
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("web_search"))
    monkeypatch.setattr(runner, "_AUDIT_POLL_DEADLINE_S", 0.0)
    client = _FakeClient("# Desk call\nNO-GO — here's the backtest code I'd run: ...")
    case = {
        "id": "wf", "category": "workflow", "kind": "workflow", "name": "wf",
        "workflow": "quant-desk", "inputs": {"idea": "x"},
        "expected_tools": ["backtest_strategy"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert not res.passed and "backtest_strategy" in res.detail


def test_workflow_case_asserts_any_tool_fired(monkeypatch):
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("factor_zoo"))
    client = _FakeClient("# Factors\nmomentum alive, IR 1.1.")
    case = {
        "id": "wf", "category": "workflow", "kind": "workflow", "name": "wf",
        "workflow": "quant-desk", "inputs": {"idea": "x"},
        "expected_any_tools": ["factor_eval", "factor_zoo"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail


def test_workflow_kind_is_dispatchable():
    assert "workflow" in runner._RUNNERS


# ── the new cases are well-formed ───────────────────────────────────────────────


def test_new_cases_present_and_valid():
    by_id = {c["id"]: c for c in TASKS}
    for cid in ("research_delegation", "workflow_research_brief", "workflow_deep_research_adversarial"):
        assert cid in by_id, f"{cid} missing"

    # Delegation is satisfied by any hand-off tool (subagent or workflow).
    assert "run_workflow" in by_id["research_delegation"]["expected_any_tools"]
    assert "task" in by_id["research_delegation"]["expected_any_tools"]

    for cid in ("workflow_research_brief", "workflow_deep_research_adversarial"):
        case = by_id[cid]
        assert case["kind"] == "workflow"
        assert case.get("workflow") and isinstance(case.get("inputs"), dict)
        crit = case["verify_rubric"]["criteria"]
        assert crit and all(isinstance(c, str) for c in crit)


def test_workflow_cases_reference_real_recipes():
    # The recipes the cases drive must actually be bundled.
    bundled = {p.stem for p in (Path(__file__).parent.parent / "plugins" / "workflows" / "recipes").glob("*.yaml")}
    for c in TASKS:
        if c.get("kind") == "workflow":
            assert c["workflow"] in bundled, f"{c['id']} → unknown recipe {c['workflow']!r}"


# ── requires_env skip mechanism (ADR 0024 coding-agent eval) ──────────────────


def test_requirements_unmet_skips_when_env_missing(monkeypatch):
    monkeypatch.delenv("EVAL_X", raising=False)
    assert runner._requirements_unmet({"requires_env": ["EVAL_X"]}) == "requires_env EVAL_X"


def test_requirements_met_runs_when_env_set(monkeypatch):
    monkeypatch.setenv("EVAL_X", "1")
    assert runner._requirements_unmet({"requires_env": ["EVAL_X"]}) is None


def test_no_requirements_runs():
    assert runner._requirements_unmet({}) is None
    assert runner._requirements_unmet({"requires_env": []}) is None


def test_acp_delegation_eval_case_present_and_gated():
    case = {c["id"]: c for c in TASKS}.get("acp_delegation")
    assert case is not None, "acp_delegation case missing"
    assert case["requires_env"] == ["EVAL_CODING_AGENT"]   # skips by default
    assert case["expected_tools"] == ["delegate_to"]
    assert case["kind"] == "ask"


def test_board_counts_skipped_separately(capsys):
    results = [
        runner.CaseResult("a", "tool", "A", passed=True, detail="ok"),
        runner.CaseResult("b", "tool", "B", passed=True, detail="skipped: requires_env X", skipped=True),
    ]
    runner._print_board(results)
    out = capsys.readouterr().out
    assert "1/1 passed (1 skipped)" in out
    assert "SKIP" in out
