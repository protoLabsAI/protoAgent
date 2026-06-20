"""Tests for the SKILL.md loader and the always-on skill index (ADR 0060).

Covers:
- parse_skill_md: valid, missing frontmatter, missing required fields,
  oversized description, invalid YAML.
- bundle/live name override (live wins).
- seed_skills_index → SkillsIndex.skill_summaries round-trip.
- KnowledgeMiddleware carries a skills_index and injects <available_skills>.
"""

from __future__ import annotations

from pathlib import Path

from graph.skills.index import SkillsIndex
from graph.skills.loader import (
    load_skills_from_disk,
    parse_skill_md,
    seed_skills_index,
)


def _write_skill(root: Path, slug: str, frontmatter: str, body: str = "do the thing") -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return p


def test_parse_valid_skill(tmp_path: Path) -> None:
    p = _write_skill(
        tmp_path,
        "web-research",
        "name: web-research\ndescription: Use when researching on the web.\ntools: [web_search, fetch_url]",
        "# Web Research\nPlan, search, read, cite.",
    )
    art = parse_skill_md(p)
    assert art is not None
    assert art.name == "web-research"
    assert art.description == "Use when researching on the web."
    assert art.tools_used == ["web_search", "fetch_url"]
    assert "Plan, search" in art.prompt_template
    assert art.source_session_id == "skill-md:web-research"


def test_parse_no_frontmatter_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "x" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("# just markdown, no frontmatter\n", encoding="utf-8")
    assert parse_skill_md(p) is None


def test_parse_missing_required_fields_returns_none(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "nodesc", "name: nodesc")  # no description
    assert parse_skill_md(p) is None
    p2 = _write_skill(tmp_path, "noname", "description: has desc but no name")
    assert parse_skill_md(p2) is None


def test_parse_invalid_yaml_returns_none(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "bad", "name: [unclosed\ndescription: x")
    assert parse_skill_md(p) is None


def test_parse_truncates_oversized_description(tmp_path: Path) -> None:
    long_desc = "x" * 2000
    p = _write_skill(tmp_path, "long", f"name: long\ndescription: {long_desc}")
    art = parse_skill_md(p)
    assert art is not None
    assert len(art.description) == 1024


def test_metadata_tools_fallback(tmp_path: Path) -> None:
    p = _write_skill(
        tmp_path,
        "meta",
        "name: meta\ndescription: d\nmetadata:\n  tools: [a, b]",
    )
    art = parse_skill_md(p)
    assert art is not None and art.tools_used == ["a", "b"]


def test_live_overrides_bundle_by_name(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    live = tmp_path / "live"
    _write_skill(bundle, "dup", "name: dup\ndescription: from bundle")
    _write_skill(live, "dup", "name: dup\ndescription: from live")
    arts = load_skills_from_disk([bundle, live])  # bundle first, live second
    assert len(arts) == 1
    assert arts[0].description == "from live"


def test_seed_and_retrieve_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "web-research",
        "name: web-research\ndescription: Research a topic on the web and cite sources.",
        "Plan, search, read, synthesize, cite.",
    )
    index = SkillsIndex(db_path=str(tmp_path / "skills.db"))
    count = seed_skills_index(index, [root])
    assert count == 1

    hits = index.skill_summaries()
    assert any(h["name"] == "web-research" for h in hits)
    assert index.get_skill("web-research")["prompt_template"].startswith("Plan, search")


def test_middleware_carries_skills_index_when_enabled(tmp_path: Path) -> None:
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig
    from graph.middleware.knowledge import KnowledgeMiddleware

    cfg = LangGraphConfig()  # skills_enabled defaults True, knowledge defaults True
    index = SkillsIndex(db_path=str(tmp_path / "skills.db"))
    mw = _build_middleware(cfg, knowledge_store=None, skills_index=index)
    km = next((m for m in mw if isinstance(m, KnowledgeMiddleware)), None)
    assert km is not None
    assert km._skills_index is index


def test_before_model_injects_available_skills(tmp_path: Path) -> None:
    from langchain_core.messages import HumanMessage

    from graph.middleware.knowledge import KnowledgeMiddleware

    root = tmp_path / "skills"
    _write_skill(
        root,
        "web-research",
        "name: web-research\ndescription: Research a topic on the web and cite sources.",
        "Plan, search, read, synthesize, cite.",
    )
    index = SkillsIndex(db_path=str(tmp_path / "skills.db"))
    seed_skills_index(index, [root])

    # No knowledge store — proves skills work KB-less (the None-store guard).
    mw = KnowledgeMiddleware(None, skills_index=index)
    state = {"messages": [HumanMessage(content="please research the web for me")]}
    out = mw.before_model(state, runtime=None)
    assert out is not None
    assert "<available_skills>" in out["context"]
    assert "web-research" in out["context"]


# ── User-facing skills (ADR 0052) ──────────────────────────────────────────────


def test_parse_user_facing_and_slash(tmp_path: Path) -> None:
    """`user_facing: true` + `slash:` are parsed onto the artifact (ADR 0052)."""
    p = _write_skill(
        tmp_path,
        "web-research",
        "name: web-research\ndescription: Research on the web.\nuser_facing: true\nslash: research",
        "Plan, search, cite.",
    )
    art = parse_skill_md(p)
    assert art is not None
    assert art.user_facing is True
    assert art.slash == "research"
    assert art.slash_token() == "research"


def test_user_facing_defaults_off_and_slug_fallback(tmp_path: Path) -> None:
    """Absent `user_facing` → off; an explicit-but-blank slash slugs the name."""
    plain = _write_skill(tmp_path, "plain", "name: plain\ndescription: A plain skill.")
    art = parse_skill_md(plain)
    assert art is not None and art.user_facing is False

    uf = _write_skill(
        tmp_path,
        "Big Task",
        "name: Big Task\ndescription: A user-facing skill.\nuser_facing: yes",
        "do it",
    )
    art2 = parse_skill_md(uf)
    assert art2 is not None and art2.user_facing is True
    assert art2.slash == "" and art2.slash_token() == "big-task"


def test_user_only_implies_user_facing_at_the_dataclass(tmp_path: Path) -> None:
    """user_only ⇒ user_facing is enforced on the artifact itself (single source),
    so even a SKILL.md / programmatic artifact that sets only user_only can't end up
    withheld from the index AND slash-less (ADR 0060). add_skill then derives a slash."""
    from datetime import datetime, timezone

    from graph.extensions.skills import SkillV1Artifact

    # Direct construction with only user_only set — __post_init__ coerces user_facing.
    art = SkillV1Artifact(
        name="Deploy",
        description="deploy + verify",
        prompt_template="run it",
        created_at=datetime.now(timezone.utc),
        user_only=True,
    )
    assert art.user_only is True
    assert art.user_facing is True  # coerced — never a contradictory state

    # And via the SKILL.md parser (frontmatter sets only user_only).
    p = _write_skill(
        tmp_path,
        "deploy",
        "name: deploy\ndescription: Deploy + verify.\nuser_only: true\nslash: deploy",
        "1. deploy\n2. verify",
    )
    parsed = parse_skill_md(p)
    assert parsed is not None and parsed.user_only is True and parsed.user_facing is True

    # End-to-end: it's withheld from the index but resolvable as a /slash.
    index = SkillsIndex(db_path=str(tmp_path / "skills.db"))
    index.add_skill(parsed, source="disk")
    assert "deploy" not in [s["name"] for s in index.skill_summaries()]
    assert "deploy" in [s["slash"] for s in index.user_facing_skills()]
