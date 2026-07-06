"""Craft plugin — bundled user-only skills + the skill_writer subagent.

The plugin is prompt-only, so the tests pin its whole contract: the manifest
parses, ``register()`` contributes exactly a skill dir + one subagent, every
bundled SKILL.md is loader-valid and ``user_only`` with the expected slash
token, and no token is shadowed by a core subagent (slash precedence puts
subagents above skills).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from graph.plugins.testkit import FakeRegistry, load_plugin
from graph.skills.loader import parse_skill_md

ROOT = Path("plugins/craft")

EXPECTED_SLASHES = {"grill", "standup", "code-review", "writing-skills", "due-diligence"}

# Agent-retrievable bundled skills (no user_only/slash): guidance the AGENT pulls
# while doing the work — adr-authoring exists precisely so agent-authored ADRs
# meet the house bar (plan M3), so hiding it from retrieval would defeat it.
EXPECTED_AGENT_SKILLS = {"adr-authoring"}


def _skill_files() -> list[Path]:
    return sorted(ROOT.glob("skills/*/SKILL.md"))


def test_manifest_parses_and_declares_prompt_only():
    data = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert data["id"] == "craft"
    assert data["enabled"] is True
    assert data["version"]
    assert data["min_protoagent_version"]
    # Prompt-only contract: no declared network or filesystem reach.
    assert data["capabilities"] == {"network": [], "filesystem": "none"}


def test_register_contributes_skills_and_subagent():
    pkg = load_plugin(ROOT, "craft")
    registry = FakeRegistry("craft", plugin_dir=ROOT)
    pkg.register(registry)

    assert [Path(p).name for p in registry.skill_dirs] == ["skills"]
    assert len(registry.subagents) == 1
    sub = registry.subagents[0]
    assert sub.name == "skill_writer"
    assert sub.description and sub.system_prompt
    assert sub.tools == ["load_skill"]
    # Meta-work must not distill into skills about writing skills.
    assert sub.allow_skill_emission is False
    # Prompt-only: nothing else contributed.
    assert not registry.tools and not registry.routers and not registry.surfaces


def test_bundled_skills_are_loader_valid_and_user_only():
    files = _skill_files()
    expected_count = len(EXPECTED_SLASHES) + len(EXPECTED_AGENT_SKILLS)
    assert len(files) == expected_count, f"unexpected bundled skills: {[str(f) for f in files]}"

    seen = {}
    agent_skills = set()
    for path in files:
        artifact = parse_skill_md(path)
        assert artifact is not None, f"{path} failed to parse"
        assert artifact.prompt_template, f"{path} has an empty body"
        if artifact.name in EXPECTED_AGENT_SKILLS:
            assert not artifact.user_only, f"{path} must be agent-retrievable"
            agent_skills.add(artifact.name)
            continue
        assert artifact.user_only, f"{path} must be user_only (slash-only rituals)"
        assert artifact.user_facing, f"{path}: user_only implies user_facing"
        assert artifact.slash, f"{path} must pin an explicit slash token"
        seen[artifact.slash] = artifact.name

    assert set(seen) == EXPECTED_SLASHES
    assert agent_skills == EXPECTED_AGENT_SKILLS
    assert len(seen) == len(files) - len(agent_skills), "slash tokens must be unique"


def test_slash_tokens_not_shadowed_by_core_subagents():
    """Slash precedence is workflow > subagent > skill — a core subagent with the
    same token would silently shadow the bundled skill (the /research lesson)."""
    from graph.subagents.config import SUBAGENT_REGISTRY

    collisions = EXPECTED_SLASHES & set(SUBAGENT_REGISTRY)
    assert not collisions, f"slash tokens shadowed by core subagents: {collisions}"
