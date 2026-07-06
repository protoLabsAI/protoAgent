"""The code-review workflow + its finder/synthesizer roles (ADR 0077) — mirrors
tests/test_deep_research.py: the recipe must validate against the shipped
subagent registry, and each role must actually hold the tools its stage needs."""

from __future__ import annotations

from pathlib import Path

import yaml

from graph.review.findings import FINDINGS_CONTRACT
from graph.subagents.config import SUBAGENT_REGISTRY
from plugins.workflows.engine import validate_recipe

RECIPE_PATH = Path(__file__).parent.parent / "plugins" / "workflows" / "recipes" / "code-review.yaml"


def _recipe() -> dict:
    return yaml.safe_load(RECIPE_PATH.read_text())


# ── the review roles ──────────────────────────────────────────────────────────


def test_review_roles_registered():
    for name in ("review-finder", "review-synthesizer"):
        assert name in SUBAGENT_REGISTRY, f"{name} missing from registry"


def test_finder_can_read_the_change():
    tools = SUBAGENT_REGISTRY["review-finder"].tools
    assert "github_pr_diff" in tools and "github_read_file" in tools


def test_verifier_can_recheck_the_diff():
    # The verify pass re-reads the code; verdicts from memory would be theater.
    tools = SUBAGENT_REGISTRY["verifier"].tools
    assert "github_pr_diff" in tools and "github_read_file" in tools


def test_synthesizer_has_a_tool():
    # A toolless subagent config fails at runtime ("No tools available") — the
    # text-in/text-out synthesizer still needs at least one benign tool bound.
    assert SUBAGENT_REGISTRY["review-synthesizer"].tools


def test_review_roles_speak_the_findings_contract():
    for name in ("review-finder", "review-synthesizer"):
        assert FINDINGS_CONTRACT in SUBAGENT_REGISTRY[name].system_prompt


def test_review_roles_do_not_emit_skills():
    for name in ("review-finder", "review-synthesizer"):
        assert SUBAGENT_REGISTRY[name].allow_skill_emission is False


# ── the recipe ────────────────────────────────────────────────────────────────


def test_recipe_validates_against_shipped_registry():
    assert validate_recipe(_recipe(), known_subagents=set(SUBAGENT_REGISTRY)) == []


def test_recipe_shape_four_finders_then_synthesize_verify_report():
    steps = {s["id"]: s for s in _recipe()["steps"]}
    finders = [sid for sid, s in steps.items() if s["subagent"] == "review-finder"]
    assert len(finders) == 4
    assert all(not steps[sid].get("depends_on") for sid in finders), "finders must run in parallel"
    assert sorted(steps["synthesize"]["depends_on"]) == sorted(finders)
    assert steps["verify"]["subagent"] == "verifier"
    assert steps["verify"]["depends_on"] == ["synthesize"]
    assert steps["report"]["depends_on"] == ["verify"]


def test_recipe_output_is_the_final_report():
    assert _recipe()["output"].strip() == "{{steps.report.output}}"


# ── the noise ledger + delta re-review (ADR 0078 Phase A1) ────────────────────


def test_finder_carries_the_noise_ledger_and_gap_rule():
    p = SUBAGENT_REGISTRY["review-finder"].system_prompt
    assert "OUT OF SCOPE" in p and "linter or formatter already owns" in p
    assert "Gap: unverified" in p  # unverifiable claims are Gaps, never severities
    assert "80% confidence" in p
    assert "DELTA re-review" in p


def test_verifier_grounding_rule_never_confirms_on_plausibility():
    p = SUBAGENT_REGISTRY["verifier"].system_prompt
    assert "gap: unverified" in p
    assert "never confirmed on plausibility alone" in p


def test_synthesizer_filters_ledger_slippage_and_gap_lines():
    p = SUBAGENT_REGISTRY["review-synthesizer"].system_prompt
    assert "out-of-scope ledger" in p
    assert "never in the array" in p  # Gap prose lines stay out of the findings JSON


def test_recipe_declares_prior_findings_and_threads_it_into_every_finder():
    r = _recipe()
    prior = next(i for i in r["inputs"] if i["name"] == "prior_findings")
    assert prior["default"].startswith("(none")  # empty state is explicit, not a blank section
    finders = [s for s in r["steps"] if s["subagent"] == "review-finder"]
    assert len(finders) == 4
    for s in finders:
        assert "{{inputs.prior_findings}}" in s["prompt"], s["id"]
