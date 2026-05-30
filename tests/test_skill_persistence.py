"""Tests for persisting agent-emitted skills alongside disk SKILL.md skills.

Covers the `source` column, disk/emitted separation on re-seed, emitted dedup,
the curator pinning disk skills, and the _run_subagent → index persist path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from graph.config import LangGraphConfig
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

    idx = SkillsIndex(str(p))  # detects v2 → backup + rebuild to v3
    idx.add_skill(_artifact("post-migrate"), source="emitted")
    assert any(s["name"] == "post-migrate" and s["source"] == "emitted" for s in idx.all_skills())


def test_add_skill_records_source(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.add_skill(_artifact("a"), source="emitted")
    idx.add_skill(_artifact("b"), source="disk")
    by_name = {s["name"]: s["source"] for s in idx.all_skills()}
    assert by_name == {"a": "emitted", "b": "disk"}


def test_replace_disk_skills_preserves_emitted(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.add_emitted_skill(_artifact("learned"))
    idx.replace_disk_skills([_artifact("disk-one"), _artifact("disk-two")])
    names = {s["name"]: s["source"] for s in idx.all_skills()}
    assert names == {"learned": "emitted", "disk-one": "disk", "disk-two": "disk"}

    # Re-seeding disk again still leaves the emitted skill intact, and refreshes
    # the disk set (disk-two dropped).
    idx.replace_disk_skills([_artifact("disk-one")])
    names = {s["name"]: s["source"] for s in idx.all_skills()}
    assert names == {"learned": "emitted", "disk-one": "disk"}


def test_add_emitted_skill_dedupes_by_name(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.add_emitted_skill(_artifact("dup", prompt="v1"))
    idx.add_emitted_skill(_artifact("dup", prompt="v2"))
    rows = [s for s in idx.all_skills() if s["name"] == "dup"]
    assert len(rows) == 1 and rows[0]["prompt_template"] == "v2"


def test_emitted_dedup_does_not_touch_same_name_disk(tmp_path) -> None:
    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.replace_disk_skills([_artifact("shared", prompt="disk")])
    idx.add_emitted_skill(_artifact("shared", prompt="emitted"))
    by_source = {s["source"]: s["prompt_template"] for s in idx.all_skills() if s["name"] == "shared"}
    assert by_source == {"disk": "disk", "emitted": "emitted"}


def test_curator_pins_disk_skills(tmp_path) -> None:
    from graph.skills.curator import SkillCurator

    idx = SkillsIndex(str(tmp_path / "s.db"))
    idx.replace_disk_skills([_artifact("pinned")])
    idx.add_emitted_skill(_artifact("ephemeral"))

    curator = SkillCurator(db_path=str(tmp_path / "s.db"), index=idx)
    loaded = {s["name"] for s in curator._load_index()}
    assert loaded == {"ephemeral"}  # disk skill excluded from curation

    # A full run must never delete the pinned disk skill.
    curator.run()
    remaining = {s["name"] for s in idx.all_skills()}
    assert "pinned" in remaining


async def test_run_subagent_persists_emitted_skill(tmp_path, monkeypatch) -> None:
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    from graph import agent as agentmod

    class _FakeAgent:
        async def ainvoke(self, *_a, **_k):
            return {"messages": [AIMessage(
                content="found the answer",
                tool_calls=[{"name": "web_search", "args": {}, "id": "tc1"}],
            )]}

    monkeypatch.setattr(agentmod, "create_agent", lambda **_k: _FakeAgent())

    @tool
    async def web_search(query: str) -> str:
        """search"""
        return ""

    idx = SkillsIndex(str(tmp_path / "s.db"))
    out = await agentmod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"web_search": web_search},
        available_subagents="researcher",
        description="find the capital of France",
        prompt="research the capital of France",
        subagent_type="researcher",
        emit_skill=True,
        skills_index=idx,
    )
    assert "found the answer" in out
    persisted = idx.all_skills()
    assert any(s["name"] == "find the capital of France" and s["source"] == "emitted"
               for s in persisted)


async def test_run_subagent_no_persist_without_index(tmp_path, monkeypatch) -> None:
    # emit_skill=True but no index → must not raise; nothing persisted anywhere.
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    from graph import agent as agentmod

    class _FakeAgent:
        async def ainvoke(self, *_a, **_k):
            return {"messages": [AIMessage(content="ok", tool_calls=[{"name": "web_search", "args": {}, "id": "t"}])]}

    monkeypatch.setattr(agentmod, "create_agent", lambda **_k: _FakeAgent())

    @tool
    async def web_search(query: str) -> str:
        """search"""
        return ""

    out = await agentmod._run_subagent(
        config=LangGraphConfig(), tool_map={"web_search": web_search}, available_subagents="researcher",
        description="x", prompt="y", subagent_type="researcher", emit_skill=True, skills_index=None,
    )
    assert "ok" in out
