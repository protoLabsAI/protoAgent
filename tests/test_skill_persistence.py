"""Tests for the skill index's `source` column and disk/non-disk separation.

Covers schema migration, the `source` tag on stored skills, that re-seeding the
disk source leaves other sources intact, and the curator pinning disk skills.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from graph.skills.index import SkillsIndex


def _artifact(name: str, desc: str = "d", prompt: str = "p", tools=("web_search",)):
    return SimpleNamespace(
        name=name,
        description=desc,
        prompt_template=prompt,
        tools_used=list(tools),
        source_session_id="s1",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_v2_db_migrates_to_v3(tmp_path) -> None:
    # An older v2 index (no `source` column) must auto-migrate (backup + rebuild)
    # rather than crash when the bumped SkillsIndex opens it.
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(
        """
        CREATE VIRTUAL TABLE skills_fts USING fts5(
            name, description, prompt_template, tools_used, source_session_id,
            created_at UNINDEXED, confidence UNINDEXED, last_used UNINDEXED
        );
        CREATE TABLE _skills_meta (key TEXT PRIMARY KEY, version INTEGER NOT NULL);
        INSERT INTO _skills_meta (key, version) VALUES ('schema_version', 2);
        """
    )
    conn.commit()
    conn.close()

    idx = SkillsIndex(str(p))  # detects v2 → backup + rebuild to the current schema
    idx.add_skill(_artifact("post-migrate"), source="disk")
    assert any(s["name"] == "post-migrate" and s["source"] == "disk" for s in idx.all_skills())


def test_add_skill_records_source(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.add_skill(_artifact("a"), source="promoted")
    idx.add_skill(_artifact("b"), source="disk")
    by_name = {s["name"]: s["source"] for s in idx.all_skills()}
    assert by_name == {"a": "promoted", "b": "disk"}


def test_replace_disk_skills_preserves_other_sources(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.add_skill(_artifact("promoted-one"), source="promoted")
    idx.replace_disk_skills([_artifact("disk-one"), _artifact("disk-two")])
    names = {s["name"]: s["source"] for s in idx.all_skills()}
    assert names == {"promoted-one": "promoted", "disk-one": "disk", "disk-two": "disk"}

    # Re-seeding disk again still leaves the non-disk skill intact, and refreshes
    # the disk set (disk-two dropped).
    idx.replace_disk_skills([_artifact("disk-one")])
    names = {s["name"]: s["source"] for s in idx.all_skills()}
    assert names == {"promoted-one": "promoted", "disk-one": "disk"}


def test_curator_pins_disk_skills(tmp_path) -> None:
    from graph.skills.curator import SkillCurator

    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.replace_disk_skills([_artifact("pinned")])
    idx.add_skill(_artifact("ephemeral"), source="promoted")

    curator = SkillCurator(db_path=str(tmp_path / "s.db"), index=idx)
    loaded = {s["name"] for s in curator._load_index()}
    assert loaded == {"ephemeral"}  # disk skill excluded from curation

    # A full run must never delete the pinned disk skill.
    curator.run()
    remaining = {s["name"] for s in idx.all_skills()}
    assert "pinned" in remaining
