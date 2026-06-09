"""Layered skills tier (ADR 0041 slice 3) — commons ∪ private, write private, promote."""

from __future__ import annotations

import types

from graph.skills.index import SkillsIndex
from graph.skills.layered import LayeredSkillsIndex


def _art(name: str, desc: str):
    return types.SimpleNamespace(name=name, description=desc, prompt_template="do " + name,
                                 tools_used=(), source_session_id="")


def _idx(tmp_path):
    private = SkillsIndex(db_path=str(tmp_path / "priv.db"))
    commons = SkillsIndex(db_path=str(tmp_path / "commons.db"))
    for ix in (private, commons):
        if hasattr(ix, "initialize_db"):
            ix.initialize_db()
    return private, commons


def test_layered_union_write_private_promote(tmp_path):
    private, commons = _idx(tmp_path)
    private.add_skill(_art("scrape_web", "a private scraping skill"))
    commons.add_skill(_art("triage_ticket", "a commons triage skill"))
    idx = LayeredSkillsIndex(private, commons)

    names = {r.name for r in idx.load_skills("skill", k=10)}
    assert {"scrape_web", "triage_ticket"} <= names  # reads BOTH tiers

    idx.add_skill(_art("new_one", "freshly learned skill"))  # write → private only
    tiers = {(s["name"], s["tier"]) for s in idx.all_skills()}
    assert ("new_one", "private") in tiers and ("new_one", "commons") not in tiers

    assert idx.promote("new_one") is True                    # promote private → commons
    assert ("new_one", "commons") in {(s["name"], s["tier"]) for s in idx.all_skills()}
    assert idx.promote("does_not_exist") is False
    idx.close()


def test_skills_scope_config_parses(tmp_path):
    from graph.config import LangGraphConfig
    cfg = tmp_path / "c.yaml"
    cfg.write_text("skills: { scope: layered }\n")
    assert LangGraphConfig.from_yaml(str(cfg)).skills_scope == "layered"


def test_layered_dedup_best_match_wins(tmp_path):
    """ADR 0041 contract — a skill present in BOTH tiers appears once in a layered
    read (de-duped by name; the better BM25 match wins)."""
    private, commons = _idx(tmp_path)
    private.add_skill(_art("dup_skill", "private copy mentions apples"))
    commons.add_skill(_art("dup_skill", "commons copy mentions apples"))
    idx = LayeredSkillsIndex(private, commons)
    hits = [r for r in idx.load_skills("apples", k=10) if r.name == "dup_skill"]
    assert len(hits) == 1  # de-duped across tiers, single best-scoring record
    idx.close()
