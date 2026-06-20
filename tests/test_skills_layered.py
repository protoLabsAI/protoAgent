"""Layered skills tier (ADR 0041 slice 3) — commons ∪ private, write private, promote."""

from __future__ import annotations

import types

from graph.skills.index import SkillsIndex
from graph.skills.layered import LayeredSkillsIndex


def _art(name: str, desc: str):
    return types.SimpleNamespace(
        name=name, description=desc, prompt_template="do " + name, tools_used=(), source_session_id=""
    )


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

    names = {r["name"] for r in idx.skill_summaries()}
    assert {"scrape_web", "triage_ticket"} <= names  # reads BOTH tiers

    idx.add_skill(_art("new_one", "freshly learned skill"))  # write → private only
    tiers = {(s["name"], s["tier"]) for s in idx.all_skills()}
    assert ("new_one", "private") in tiers and ("new_one", "commons") not in tiers

    assert idx.promote("new_one") is True  # promote private → commons
    assert ("new_one", "commons") in {(s["name"], s["tier"]) for s in idx.all_skills()}
    assert idx.promote("does_not_exist") is False
    idx.close()


def test_promote_is_upsert_no_duplicate_rows(tmp_path):
    """Re-promoting refreshes the commons copy instead of leaving duplicate rows
    (add_skill has no dedup; the layered read hides dupes by name)."""
    private, commons = _idx(tmp_path)
    private.add_skill(_art("nightly", "v1 — buy low"))
    idx = LayeredSkillsIndex(private, commons)
    assert idx.promote("nightly") is True

    # Update the private copy, re-promote → commons reflects v2, still ONE row.
    private.add_skill(_art("nightly", "v2 — buy lower"))  # private now has 2 rows; promote uses all_skills match
    assert idx.promote("nightly") is True
    commons_rows = [s for s in commons.all_skills() if s["name"] == "nightly"]
    assert len(commons_rows) == 1  # upsert — not duplicated
    idx.close()


def test_promote_preserves_user_only(tmp_path):
    """A user_only (slash-only) private skill stays user_only in the commons — it
    must not become agent-discoverable just by being promoted."""
    private, commons = _idx(tmp_path)
    private.add_skill(
        types.SimpleNamespace(
            name="deploy", description="deploy + verify", prompt_template="run it",
            tools_used=(), source_session_id="", user_facing=True, slash="deploy", user_only=True,
        )
    )
    idx = LayeredSkillsIndex(private, commons)
    assert idx.promote("deploy") is True
    rec = commons.get_skill("deploy")
    assert rec is not None and rec["user_only"] is True
    # Withheld from the agent index, but resolvable as a /slash on the commons.
    assert "deploy" not in [s["name"] for s in commons.skill_summaries()]
    assert "deploy" in [s["slash"] for s in commons.user_facing_skills()]
    idx.close()


def test_forget_from_commons(tmp_path):
    """forget_from_commons removes a commons skill (the inverse of promote) without
    touching the private tier; missing name → False."""
    private, commons = _idx(tmp_path)
    private.add_skill(_art("scrape", "a private skill"))
    idx = LayeredSkillsIndex(private, commons)
    idx.promote("scrape")
    assert "scrape" in [s["name"] for s in commons.all_skills()]

    assert idx.forget_from_commons("scrape") is True
    assert "scrape" not in [s["name"] for s in commons.all_skills()]
    assert "scrape" in [s["name"] for s in private.all_skills()]  # private untouched
    assert idx.forget_from_commons("scrape") is False  # already gone
    idx.close()


def test_skills_cli_forget(tmp_path, monkeypatch, capsys):
    """`skills forget <name>` removes from the commons + reports the commons path;
    a missing name exits 1. (run_skills_cli closes its index, so build a fresh one
    per call over the same on-disk dbs — mirroring real invocations.)"""
    from graph.skills import cli
    from graph.skills.index import SkillsIndex

    priv_path = str(tmp_path / "priv.db")
    comm_path = str(tmp_path / "commons" / "skills.db")
    (tmp_path / "commons").mkdir(parents=True, exist_ok=True)

    def _fresh():
        return LayeredSkillsIndex(SkillsIndex(priv_path), SkillsIndex(comm_path)), comm_path

    # Seed: a skill promoted into the commons.
    seed_idx, _ = _fresh()
    seed_idx._private.add_skill(_art("ore_run", "a private skill"))
    assert seed_idx.promote("ore_run") is True
    seed_idx.close()

    monkeypatch.setattr(cli, "_layered_index", _fresh)

    assert cli.run_skills_cli(["forget", "ore_run"]) == 0
    out = capsys.readouterr().out
    assert "forgot 'ore_run'" in out and "commons" in out
    assert "ore_run" not in [s["name"] for s in SkillsIndex(comm_path).all_skills()]

    assert cli.run_skills_cli(["forget", "nope"]) == 1  # nothing to forget


def test_skills_cli_curate_commons_dedupes_only(tmp_path, monkeypatch, capsys):
    """`skills curate --tier commons` dedupes but does NOT decay/prune (bd-2mc): a
    long-idle promoted skill survives, duplicates collapse."""
    from graph.skills import cli
    from graph.skills.index import SkillsIndex

    comm = str(tmp_path / "commons.db")
    idx = SkillsIndex(comm)
    idx.add_skill(_art("deploy", "ship then verify the service"))  # dup pair → dedupe
    idx.add_skill(_art("deploy", "ship then verify the service"))
    # A stale, high-value skill seeded with an ancient last_used (private would prune it).
    conn = idx._open_conn()
    conn.execute(
        "INSERT INTO skills_fts (name, description, prompt_template, tools_used, "
        "source_session_id, created_at, confidence, last_used) VALUES (?,?,?,?,?,?,?,?)",
        ("rare runbook", "emergency restore", "do it", "", "", "2025-01-01T00:00:00+00:00", 1.0, "2025-01-01T00:00:00+00:00"),
    )
    conn.commit()
    idx.close()

    monkeypatch.setattr(cli, "_resolve_tier_db", lambda tier: comm)
    assert cli.run_skills_cli(["curate", "--tier", "commons"]) == 0
    assert "tier=commons" in capsys.readouterr().out

    after = SkillsIndex(comm).all_skills()
    assert "rare runbook" in [s["name"] for s in after]  # not decayed/pruned despite being stale
    assert len([s for s in after if s["name"] == "deploy"]) == 1  # deduped


def test_skills_scope_config_parses(tmp_path):
    from graph.config import LangGraphConfig

    cfg = tmp_path / "c.yaml"
    cfg.write_text("skills: { scope: layered }\n")
    assert LangGraphConfig.from_yaml(str(cfg)).skills_scope == "layered"


def test_layered_dedup_private_wins(tmp_path):
    """ADR 0041 contract — a skill present in BOTH tiers appears once in a layered
    read (de-duped by name; private shadows commons)."""
    private, commons = _idx(tmp_path)
    private.add_skill(_art("dup_skill", "private copy mentions apples"))
    commons.add_skill(_art("dup_skill", "commons copy mentions apples"))
    idx = LayeredSkillsIndex(private, commons)
    hits = [r for r in idx.skill_summaries() if r["name"] == "dup_skill"]
    assert len(hits) == 1  # de-duped across tiers
    assert hits[0]["description"] == "private copy mentions apples"  # private shadows commons
    assert idx.discoverable_count() == 1
    assert idx.get_skill("dup_skill")["prompt_template"] == "do dup_skill"
    idx.close()


def _uf_art(name, desc, slash):
    return types.SimpleNamespace(
        name=name,
        description=desc,
        prompt_template="do " + name,
        tools_used=(),
        source_session_id="",
        user_facing=True,
        slash=slash,
    )


def test_layered_user_facing_union_private_wins(tmp_path):
    """user_facing_skills() unions both tiers, de-duped by slash token (ADR 0052);
    a private skill sharing a token overrides the commons one."""
    private, commons = _idx(tmp_path)
    commons.add_skill(_uf_art("research", "commons research", "research"))
    commons.add_skill(_uf_art("triage", "commons triage", "triage"))
    private.add_skill(_uf_art("research", "private research override", "research"))
    idx = LayeredSkillsIndex(private, commons)

    ufs = {s["slash"]: s for s in idx.user_facing_skills()}
    assert set(ufs) == {"research", "triage"}  # union, de-duped
    assert ufs["research"]["description"] == "private research override"
    assert ufs["research"]["tier"] == "private"  # private wins
    assert ufs["triage"]["tier"] == "commons"
    idx.close()
