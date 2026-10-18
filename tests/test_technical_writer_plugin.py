"""The writer pack must load as retrievable skills without acquiring capabilities."""

from pathlib import Path

from graph.plugins.testkit import FakeRegistry, load_plugin
from graph.skills.loader import parse_skill_md
from graph.plugins.loader import load_manifest

ROOT = Path("plugins/technical_writer")


def test_writer_pack_loads_without_tools_or_routes():
    manifest = load_manifest(ROOT)
    assert manifest is not None and manifest.id == "technical_writer"
    assert manifest.enabled is False
    registry = FakeRegistry("technical_writer", plugin_dir=ROOT)
    load_plugin(ROOT, "technical_writer").register(registry)
    assert len(registry.skill_dirs) == 1
    assert not registry.tools and not registry.routers and not registry.surfaces
    paths = sorted((ROOT / registry.skill_dirs[0]).glob("*/SKILL.md"))
    assert len(paths) == 6
    names = set()
    for path in paths:
        skill = parse_skill_md(path)
        assert skill is not None and skill.prompt_template and not skill.user_only
        assert skill.name == path.parent.name
        names.add(skill.name)
    assert len(names) == len(paths)
    others = {
        skill.name
        for path in Path("plugins").glob("*/skills/*/SKILL.md")
        if path.parents[2].name != "technical_writer"
        if (skill := parse_skill_md(path)) is not None
    }
    assert not names & others
