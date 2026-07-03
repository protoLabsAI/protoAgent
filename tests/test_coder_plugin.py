"""Tests for the coder plugin — execution-grounded code-solve (ADR 0064).

Covers the deterministic ladder with injected generate/verify stubs (no live
model, no subprocess), the real subprocess pytest verifier, and code extraction.
"""

from __future__ import annotations

import pytest

from plugins.coder.generate import extract_code
from plugins.coder.solve import Budget, Verdict, solve
from plugins.coder.verify import _parse, run_tests


# ── ladder helpers ────────────────────────────────────────────────────────────


def _gen_sequence(outputs):
    """A generate() stub that returns successive outputs, recording feedback seen."""
    calls = {"n": 0, "feedbacks": []}

    async def generate(task, *, feedback=None):
        calls["feedbacks"].append(feedback)
        out = outputs[min(calls["n"], len(outputs) - 1)]
        calls["n"] += 1
        return out

    return generate, calls


def _verify_passes_on(good):
    """A verify() stub: a candidate passes iff it equals ``good``; otherwise 1/1 fail."""

    async def verify(code):
        if code == good:
            return Verdict(passed=True, total=1, failed=0)
        return Verdict(passed=False, total=1, failed=1, failing=["test_x"], output="boom")

    return verify


# ── the ladder ──────────────────────────────────────────────────────────────


async def test_greedy_solves_one_shot():
    gen, calls = _gen_sequence(["good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6))
    assert res.passed is True
    assert res.rung == "greedy"
    assert res.gens_spent == 1
    assert calls["n"] == 1  # never escalated


async def test_escalates_to_best_of_k():
    # greedy ("bad") fails; one of the k extra candidates ("good") passes.
    gen, _ = _gen_sequence(["bad", "good", "bad"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), k=3)
    assert res.passed is True
    assert res.rung == "best-of-k"
    assert res.solution == "good"


async def test_tree_search_uses_failing_feedback():
    # greedy + best-of-k all "bad"; the refine round returns "good".
    gen, calls = _gen_sequence(["bad", "bad", "bad", "good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), k=3, tree_depth=2)
    assert res.passed is True
    assert res.rung == "tree-search"
    # the refine call (4th) must have received non-None failing feedback
    assert calls["feedbacks"][-1] is not None
    assert "failing" in calls["feedbacks"][-1]


async def test_fusion_rung_fires_only_after_cheaper_rungs_fail():
    # greedy + best-of-k + tree-search all "bad"; the fusion generator returns "good".
    # Fusion must fire last and solve. Budget large enough to reach it.
    gen, gcalls = _gen_sequence(["bad"])  # everything from the base generator fails

    fcalls = {"n": 0}

    async def fusion_gen(task, *, feedback=None):
        fcalls["n"] += 1
        return "good"

    res = await solve(
        "t",
        generate=gen,
        verify=_verify_passes_on("good"),
        budget=Budget(12),
        k=3,
        tree_depth=2,
        fusion_generate=fusion_gen,
        fusion_k=2,
    )
    assert res.passed is True
    assert res.rung == "fusion"
    assert fcalls["n"] >= 1  # fusion was actually invoked
    assert gcalls["n"] >= 1  # cheaper rungs ran first


async def test_fusion_not_invoked_when_cheaper_rung_solves():
    gen, _ = _gen_sequence(["good"])  # greedy solves immediately
    fcalls = {"n": 0}

    async def fusion_gen(task, *, feedback=None):
        fcalls["n"] += 1
        return "good"

    res = await solve(
        "t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(12), fusion_generate=fusion_gen
    )
    assert res.rung == "greedy"
    assert fcalls["n"] == 0  # fusion never paid for


# ── force_rung: an operator/testing affordance, never used by the real ladder ────


async def test_force_rung_greedy_reports_pass_without_escalating():
    gen, calls = _gen_sequence(["good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), force_rung="greedy")
    assert res.passed is True and res.rung == "greedy"
    assert calls["n"] == 1


async def test_force_rung_greedy_reports_fail_without_escalating():
    """A forced rung STOPS after that one rung — even though best-of-k would have
    solved it (see test_escalates_to_best_of_k), forcing greedy never gets there."""
    gen, calls = _gen_sequence(["bad", "good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), k=3, force_rung="greedy")
    assert res.passed is False and res.rung == "greedy"
    assert calls["n"] == 1  # never escalated to best-of-k, even though gen[1] would pass


async def test_force_rung_best_of_k_generates_k_fresh_candidates():
    gen, calls = _gen_sequence(["bad", "bad", "good"])
    res = await solve(
        "t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), k=3, force_rung="best-of-k"
    )
    assert res.passed is True and res.rung == "best-of-k"
    assert calls["n"] == 3  # k candidates generated fresh — no greedy pre-spend to reuse


async def test_force_rung_tree_search_seeds_then_refines_on_failure():
    # seed "bad" fails; refine round 1 returns "good".
    gen, calls = _gen_sequence(["bad", "good"])
    res = await solve(
        "t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), tree_depth=2, force_rung="tree-search"
    )
    assert res.passed is True and res.rung == "tree-search"
    assert calls["feedbacks"][-1] is not None and "failing" in calls["feedbacks"][-1]


async def test_force_rung_tree_search_seed_can_pass_outright():
    gen, calls = _gen_sequence(["good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), force_rung="tree-search")
    assert res.passed is True and res.rung == "tree-search"
    assert calls["n"] == 1  # solved on the seed — no refine round needed


async def test_force_rung_fusion_generates_fresh_no_prior_feedback():
    gen, gcalls = _gen_sequence(["irrelevant"])  # the base generator is never touched

    async def fusion_gen(task, *, feedback=None):
        assert feedback is None  # no prior attempt in an isolated forced-fusion test
        return "good"

    res = await solve(
        "t",
        generate=gen,
        verify=_verify_passes_on("good"),
        budget=Budget(6),
        fusion_generate=fusion_gen,
        fusion_k=2,
        force_rung="fusion",
    )
    assert res.passed is True and res.rung == "fusion"
    assert gcalls["n"] == 0  # the ACP generator was never called — fusion only


async def test_force_rung_fusion_without_a_fusion_generate_raises():
    gen, _ = _gen_sequence(["x"])
    with pytest.raises(ValueError, match="fusion_generate"):
        await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), force_rung="fusion")


async def test_force_rung_unknown_name_raises():
    gen, _ = _gen_sequence(["x"])
    with pytest.raises(ValueError, match="force_rung"):
        await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(6), force_rung="nonsense")


async def test_force_rung_requires_a_verifier():
    gen, _ = _gen_sequence(["x"])
    with pytest.raises(ValueError, match="verifier"):
        await solve("t", generate=gen, verify=None, budget=Budget(6), force_rung="greedy")


async def test_no_oracle_degrades_to_greedy():
    gen, calls = _gen_sequence(["whatever"])
    res = await solve("t", generate=gen, verify=None, budget=Budget(6))
    assert res.passed is None  # no oracle ⇒ un-verified
    assert res.rung == "greedy"
    assert calls["n"] == 1
    assert "verif" in res.note.lower()


async def test_budget_zero_with_verifier_is_unknown_not_failed():
    # Budget exhausted before any generation: the oracle never ran ⇒ passed is None
    # ("unknown"), not False ("ran and failed").
    gen, calls = _gen_sequence(["x"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(0))
    assert res.passed is None
    assert res.rung == "none"
    assert calls["n"] == 0


async def test_budget_caps_generations():
    # budget of 1 ⇒ only the greedy gen; no best-of-k, no refine, even though all fail.
    gen, calls = _gen_sequence(["bad", "good", "good", "good"])
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(1), k=3, tree_depth=2)
    assert res.passed is False
    assert res.gens_spent == 1
    assert calls["n"] == 1


async def test_best_partial_returned_when_exhausted():
    gen, _ = _gen_sequence(["bad"])  # nothing ever passes
    res = await solve("t", generate=gen, verify=_verify_passes_on("good"), budget=Budget(4), k=2, tree_depth=1)
    assert res.passed is False
    assert res.rung == "best-partial"
    assert res.verdict is not None and res.verdict.failing == ["test_x"]


# ── the real subprocess verifier ──────────────────────────────────────────────


async def test_run_tests_passes():
    code = "def add(a, b):\n    return a + b\n"
    tests = "from solution import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    v = await run_tests(code, tests, timeout=30)
    assert v.passed is True
    assert v.total == 1 and v.failed == 0


async def test_run_tests_reports_failing_case():
    code = "def add(a, b):\n    return a - b\n"  # wrong
    tests = "from solution import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    v = await run_tests(code, tests, timeout=30)
    assert v.passed is False
    assert v.failed >= 1
    assert any("test_add" in name for name in v.failing)
    assert v.feedback()  # non-empty model-facing summary


def test_parse_ignores_candidate_stdout_pollution():
    # A candidate that prints a pytest-looking count must not pollute the verdict:
    # only the real summary line (with the "in <t>s" suffix) is parsed.
    output = "1000 passed\n5 failed\n===== 1 failed, 2 passed in 0.04s =====\n"
    v = _parse(output, returncode=1)
    assert v.total == 3 and v.failed == 1
    assert v.passed is False


def test_parse_no_summary_with_error_exit_is_failed():
    # Collection/import error: no count summary, non-zero exit ⇒ failed, not passed.
    v = _parse("ImportError: no module named solution\n", returncode=2)
    assert v.passed is False
    assert v.failed == 1 and v.total == 1


# ── code extraction ──────────────────────────────────────────────────────────


def test_extract_code_prefers_largest_fence():
    text = "blah\n```python\nx = 1\n```\nmid\n```\nlonger block here\nmore\n```\n"
    assert extract_code(text) == "longer block here\nmore"


def test_extract_code_falls_back_to_whole_reply():
    assert extract_code("def f():\n    return 1") == "def f():\n    return 1"
