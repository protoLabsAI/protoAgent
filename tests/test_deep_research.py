"""Tests for the deep-research workflow + its adversarial subagent roles (ADR 0011)."""

from __future__ import annotations

from pathlib import Path

import yaml

from graph.subagents.config import SUBAGENT_REGISTRY
from graph.workflows.engine import validate_recipe

RECIPE_PATH = Path(__file__).parent.parent / "workflows" / "deep-research.yaml"


def _recipe() -> dict:
    return yaml.safe_load(RECIPE_PATH.read_text())


# ── the new adversarial/synthesis roles ───────────────────────────────────────


def test_new_roles_registered():
    for name in ("antagonist", "verifier", "synthesizer"):
        assert name in SUBAGENT_REGISTRY, f"{name} missing from registry"


def test_antagonist_can_search_for_disconfirming_evidence():
    # The headline capability: it must be able to hunt opposing sources itself.
    tools = SUBAGENT_REGISTRY["antagonist"].tools
    assert "web_search" in tools and "fetch_url" in tools


def test_verifier_can_check_against_sources():
    tools = SUBAGENT_REGISTRY["verifier"].tools
    assert "web_search" in tools and "fetch_url" in tools


def test_synthesizer_can_persist():
    assert "memory_ingest" in SUBAGENT_REGISTRY["synthesizer"].tools


# ── the recipe ─────────────────────────────────────────────────────────────────


def test_recipe_validates_against_the_live_registry():
    # Every subagent the recipe references must exist; DAG + templates must resolve.
    errors = validate_recipe(_recipe(), known_subagents=set(SUBAGENT_REGISTRY))
    assert errors == [], errors


def test_recipe_dag_shape():
    steps = {s["id"]: s for s in _recipe()["steps"]}
    # research + dissent are roots (parallel gather/contrarian).
    assert not steps["research"].get("depends_on")
    assert not steps["dissent"].get("depends_on")
    # antagonist + verify both wait on the evidence (they run in parallel).
    assert "gap_fill" in steps["antagonist"]["depends_on"]
    assert "gap_fill" in steps["verify"]["depends_on"]
    # synthesize is the sink — depends on the whole pipeline incl. both reviewers.
    deps = set(steps["synthesize"]["depends_on"])
    assert {"research", "dissent", "gap_fill", "antagonist", "verify"} <= deps


def test_recipe_wires_the_right_roles():
    by_id = {s["id"]: s["subagent"] for s in _recipe()["steps"]}
    assert by_id["antagonist"] == "antagonist"
    assert by_id["verify"] == "verifier"
    assert by_id["synthesize"] == "synthesizer"
    assert by_id["research"] == "researcher"
