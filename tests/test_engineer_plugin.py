"""Engineer plugin — the navigator skill pack behind the Engineer archetype.

Prompt-only, so the tests pin its whole contract: the manifest parses and ships OFF
(these skills make an agent hold back and ask — wrong for an autonomous persona),
``register()`` contributes exactly one skill dir, every bundled SKILL.md is
loader-valid and agent-retrievable (the persona reaches for them on its own), no
skill name collides with another bundled plugin's, and the soul preset that leans on
them names both.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from graph.plugins.testkit import FakeRegistry, load_plugin
from graph.skills.loader import parse_skill_md

ROOT = Path("plugins/engineer")
EXPECTED_SKILLS = {"repo-onboard", "debug-loop"}


def test_manifest_parses_ships_off_and_declares_prompt_only():
    data = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert data["id"] == "engineer"
    assert data["enabled"] is False, "navigator skills must stay opt-in — the archetype bundle enables them"
    assert data["version"]
    assert data["min_protoagent_version"]
    assert data["capabilities"] == {"network": [], "filesystem": "none"}


def test_register_contributes_only_the_skill_dir():
    pkg = load_plugin(ROOT, "engineer")
    registry = FakeRegistry("engineer", plugin_dir=ROOT)
    pkg.register(registry)

    assert [Path(p).name for p in registry.skill_dirs] == ["skills"]
    assert not registry.tools and not registry.routers and not registry.surfaces and not registry.subagents


def test_bundled_skills_are_loader_valid_and_agent_retrievable():
    files = sorted(ROOT.glob("skills/*/SKILL.md"))
    names = set()
    for path in files:
        artifact = parse_skill_md(path)
        assert artifact is not None, f"{path} failed to parse"
        assert artifact.prompt_template, f"{path} has an empty body"
        assert not artifact.user_only, f"{path} must be agent-retrievable — the persona loads it itself"
        assert artifact.name == path.parent.name, f"{path}: frontmatter name must match its directory"
        names.add(artifact.name)
    assert names == EXPECTED_SKILLS


def test_skill_names_do_not_collide_with_other_bundled_plugins():
    others = {
        p.parent.name
        for p in Path("plugins").glob("*/skills/*/SKILL.md")
        if p.parents[2].name != "engineer"
    }
    assert not (EXPECTED_SKILLS & others), f"skill names shadowed by another bundled plugin: {EXPECTED_SKILLS & others}"


def test_the_engineer_soul_preset_names_both_skills():
    soul = Path("config/soul-presets/engineer.md").read_text(encoding="utf-8")
    for name in EXPECTED_SKILLS:
        assert f"`{name}`" in soul, f"the engineer persona never points at its {name} skill"
