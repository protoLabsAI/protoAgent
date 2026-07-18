"""The due-diligence workflow + the codebase-mapper role (plan M2) — mirrors
tests/test_deep_research.py: the recipe validates against the shipped registry,
the new gather role reads code (not the web), and the synthesis is contractually
a verdict document."""

from __future__ import annotations

from pathlib import Path

import yaml

from graph.subagents.config import SUBAGENT_REGISTRY
from plugins.workflows.engine import validate_recipe

RECIPE_PATH = Path(__file__).parent.parent / "plugins" / "workflows" / "recipes" / "due-diligence.yaml"


def _recipe() -> dict:
    return yaml.safe_load(RECIPE_PATH.read_text())


# ── the codebase-mapper role ──────────────────────────────────────────────────


def test_codebase_mapper_registered():
    assert "codebase-mapper" in SUBAGENT_REGISTRY


def test_codebase_mapper_reads_code_not_the_web():
    tools = SUBAGENT_REGISTRY["codebase-mapper"].tools
    assert "read_file" in tools and "search_files" in tools and "list_projects" in tools
    assert "github_read_file" in tools  # unregistered/candidate repos read over gh
    assert "web_search" not in tools and "fetch_url" not in tools  # external evidence is the researcher's lane


def test_codebase_mapper_is_read_only():
    tools = SUBAGENT_REGISTRY["codebase-mapper"].tools
    assert "write_file" not in tools and "edit_file" not in tools and "run_command" not in tools
    assert "delete_file" not in tools  # a read-only mapper never gains the delete tool


# ── the recipe ────────────────────────────────────────────────────────────────


def test_recipe_validates_against_shipped_registry():
    assert validate_recipe(_recipe(), known_subagents=set(SUBAGENT_REGISTRY)) == []


def test_recipe_shape_gather_parallel_then_adversarial_parallel_then_verdict():
    steps = {s["id"]: s for s in _recipe()["steps"]}
    # Gather: codebase map ∥ external research (no deps on either).
    assert steps["map_codebase"]["subagent"] == "codebase-mapper"
    assert not steps["map_codebase"].get("depends_on")
    assert steps["research"]["subagent"] == "researcher"
    assert not steps["research"].get("depends_on")
    # Adversarial: antagonist ∥ verifier, both over BOTH gather outputs.
    for sid in ("antagonist", "verify"):
        assert sorted(steps[sid]["depends_on"]) == ["map_codebase", "research"]
    assert steps["antagonist"]["subagent"] == "antagonist"
    assert steps["verify"]["subagent"] == "verifier"
    # Synthesis sees everything.
    assert sorted(steps["synthesize"]["depends_on"]) == ["antagonist", "map_codebase", "research", "verify"]


def test_synthesis_prompt_carries_the_verdict_contract():
    steps = {s["id"]: s for s in _recipe()["steps"]}
    prompt = steps["synthesize"]["prompt"]
    assert "adopt | build | defer" in prompt
    assert "Conditions:" in prompt and "Revisit when:" in prompt


def test_recipe_output_is_the_synthesis():
    assert _recipe()["output"].strip() == "{{steps.synthesize.output}}"
