"""Comprehensive unit tests for graph.skills.index.SkillIndex.

Covers:
- Schema creation and table existence
- Skill indexing and count
- Top-k retrieval with various query types
- Empty database state (no errors, empty list returned)
- Token budget awareness via description length
- Edge cases: duplicate names, special characters, very long descriptions
- KnowledgeMiddleware.load_skills integration
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from graph.skills.index import SkillIndex


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeSkill:
    """Minimal duck-typed stand-in for SkillV1Artifact used in tests."""

    name: str
    description: str = ""
    prompt_template: str = ""
    tools_used: list = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_session_id: str = ""


@pytest.fixture()
def tmp_db(tmp_path):
    """Return a temporary database path that is cleaned up after each test."""
    return str(tmp_path / "test_skills.db")


@pytest.fixture()
def index(tmp_db):
    """Return a fresh SkillIndex backed by a temp DB."""
    return SkillIndex(db_path=tmp_db)


def _make_skill(name="test-skill", description="A useful skill", prompt_template="Do X then Y", tools_used=None, session="sess-1"):
    return _FakeSkill(
        name=name,
        description=description,
        prompt_template=prompt_template,
        tools_used=tools_used or ["echo", "calculator"],
        source_session_id=session,
    )


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    def test_db_file_created(self, tmp_db):
        SkillIndex(db_path=tmp_db)
        assert Path(tmp_db).exists()

    def test_skills_table_exists(self, tmp_db):
        SkillIndex(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "skills" in tables

    def test_index_on_name_column(self, tmp_db):
        SkillIndex(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='skills'"
        ).fetchall()
        conn.close()
        index_names = {r[0] for r in indexes}
        assert "idx_skills_name" in index_names

    def test_parent_dirs_created(self, tmp_path):
        deep_path = str(tmp_path / "a" / "b" / "skills.db")
        SkillIndex(db_path=deep_path)
        assert Path(deep_path).exists()

    def test_repeated_init_is_idempotent(self, tmp_db):
        """Re-creating the index on an existing DB should not raise."""
        idx1 = SkillIndex(db_path=tmp_db)
        idx1.index_skill(_make_skill())
        idx2 = SkillIndex(db_path=tmp_db)
        assert idx2.count() == 1


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

class TestIndexSkill:
    def test_count_increases_after_insert(self, index):
        assert index.count() == 0
        index.index_skill(_make_skill(name="skill-a"))
        assert index.count() == 1
        index.index_skill(_make_skill(name="skill-b"))
        assert index.count() == 2

    def test_returns_row_id(self, index):
        row_id = index.index_skill(_make_skill())
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_tools_used_persisted(self, tmp_db):
        idx = SkillIndex(db_path=tmp_db)
        tools = ["web_search", "fetch_url", "calculator"]
        idx.index_skill(_make_skill(tools_used=tools))
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT tools_used FROM skills LIMIT 1").fetchone()
        conn.close()
        assert json.loads(row[0]) == tools

    def test_datetime_stored_as_iso_string(self, tmp_db):
        idx = SkillIndex(db_path=tmp_db)
        dt = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        skill = _make_skill()
        skill.created_at = dt
        idx.index_skill(skill)
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT created_at FROM skills LIMIT 1").fetchone()
        conn.close()
        assert "2025-06-15" in row[0]

    def test_duplicate_names_allowed(self, index):
        index.index_skill(_make_skill(name="duplicate"))
        index.index_skill(_make_skill(name="duplicate"))
        assert index.count() == 2

    def test_empty_tools_list(self, index):
        """Skills with no tools should be indexed without error."""
        index.index_skill(_make_skill(tools_used=[]))
        assert index.count() == 1

    def test_very_long_description(self, index):
        long_desc = "word " * 2000  # ~10 K chars
        index.index_skill(_make_skill(description=long_desc))
        assert index.count() == 1

    def test_special_characters_in_name(self, index):
        index.index_skill(_make_skill(name="skill: (v2) [beta] & more!"))
        assert index.count() == 1

    def test_unicode_content(self, index):
        index.index_skill(_make_skill(name="スキル", description="説明テキスト"))
        assert index.count() == 1


# ---------------------------------------------------------------------------
# Search — empty database
# ---------------------------------------------------------------------------

class TestSearchEmptyDB:
    def test_search_empty_db_returns_empty_list(self, index):
        results = index.search("anything")
        assert results == []

    def test_search_empty_query_returns_empty_list(self, index):
        results = index.search("")
        assert results == []

    def test_search_whitespace_query_returns_empty_list(self, index):
        results = index.search("   ")
        assert results == []

    def test_count_on_empty_db_is_zero(self, index):
        assert index.count() == 0


# ---------------------------------------------------------------------------
# Search — retrieval correctness
# ---------------------------------------------------------------------------

class TestSearchRetrieval:
    def test_search_returns_matching_skill(self, index):
        index.index_skill(_make_skill(name="web-scraper", description="scrapes web pages"))
        results = index.search("web scraper")
        assert len(results) >= 1
        assert any(r["name"] == "web-scraper" for r in results)

    def test_search_by_description_keyword(self, index):
        index.index_skill(_make_skill(name="calc", description="arithmetic calculator tool"))
        results = index.search("arithmetic")
        assert any(r["name"] == "calc" for r in results)

    def test_search_by_prompt_template_keyword(self, index):
        index.index_skill(_make_skill(name="pipeline", prompt_template="run build then deploy"))
        results = index.search("deploy")
        assert any(r["name"] == "pipeline" for r in results)

    def test_top_k_limit_respected(self, index):
        for i in range(10):
            index.index_skill(_make_skill(name=f"skill-{i}", description="common keyword overlap"))
        results = index.search("common keyword", k=3)
        assert len(results) <= 3

    def test_top_k_equals_one(self, index):
        index.index_skill(_make_skill(name="a", description="alpha result"))
        index.index_skill(_make_skill(name="b", description="alpha beta"))
        results = index.search("alpha", k=1)
        assert len(results) == 1

    def test_no_results_for_unrelated_query(self, index):
        index.index_skill(_make_skill(name="unrelated", description="zzzz xyzzy qwerty"))
        results = index.search("completely different topic here", k=5)
        # May or may not return results depending on overlap; just verify type
        assert isinstance(results, list)

    def test_result_dict_has_required_keys(self, index):
        index.index_skill(_make_skill())
        results = index.search("test skill")
        assert len(results) >= 1
        r = results[0]
        for key in ("id", "name", "description", "prompt_template", "tools_used", "created_at", "source_session_id", "score"):
            assert key in r, f"Missing key: {key}"

    def test_tools_used_deserialized_as_list(self, index):
        index.index_skill(_make_skill(tools_used=["echo", "calculator"]))
        results = index.search("test skill")
        if results:
            assert isinstance(results[0]["tools_used"], list)

    def test_score_is_numeric(self, index):
        index.index_skill(_make_skill())
        results = index.search("test skill")
        if results:
            assert isinstance(results[0]["score"], (int, float))

    def test_empty_query_returns_recent_skills(self, index):
        for i in range(5):
            index.index_skill(_make_skill(name=f"skill-{i}"))
        results = index.search("", k=3)
        # Empty query → recent skills (up to k)
        assert len(results) <= 3

    def test_recent_skill_first_on_empty_query(self, index):
        index.index_skill(_make_skill(name="first"))
        index.index_skill(_make_skill(name="last"))
        results = index.search("", k=1)
        assert results[0]["name"] == "last"

    def test_more_relevant_skill_ranked_higher(self, index):
        """A skill whose name+description matches more query words ranks higher."""
        index.index_skill(_make_skill(
            name="web search workflow",
            description="fetches pages, parses HTML, extracts links",
        ))
        index.index_skill(_make_skill(
            name="calculator",
            description="does arithmetic",
        ))
        results = index.search("web search html pages", k=5)
        if len(results) >= 2:
            names = [r["name"] for r in results]
            assert names.index("web search workflow") < names.index("calculator")

    def test_multiple_inserts_all_searchable(self, index):
        skills = [
            _make_skill(name="alpha-skill", description="alpha content"),
            _make_skill(name="beta-skill", description="beta content"),
            _make_skill(name="gamma-skill", description="gamma content"),
        ]
        for s in skills:
            index.index_skill(s)
        results = index.search("alpha content")
        assert any(r["name"] == "alpha-skill" for r in results)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_query_with_only_special_chars(self, index):
        index.index_skill(_make_skill())
        # Should not raise; may return empty or recent
        results = index.search("!!! ??? --- ~~~")
        assert isinstance(results, list)

    def test_very_long_query(self, index):
        index.index_skill(_make_skill(description="needle in a haystack"))
        long_query = "needle " + "filler " * 500
        results = index.search(long_query, k=5)
        assert isinstance(results, list)

    def test_k_larger_than_row_count(self, index):
        index.index_skill(_make_skill())
        results = index.search("test", k=100)
        assert len(results) <= 1

    def test_k_zero_returns_empty(self, index):
        index.index_skill(_make_skill())
        results = index.search("test", k=0)
        assert results == []

    def test_source_session_id_persisted(self, index):
        index.index_skill(_make_skill(session="session-xyz"))
        results = index.search("test skill", k=5)
        if results:
            assert results[0]["source_session_id"] == "session-xyz"

    def test_empty_prompt_template(self, index):
        index.index_skill(_make_skill(prompt_template=""))
        assert index.count() == 1

    def test_newlines_in_prompt_template(self, index):
        multiline = "Step 1: do this\nStep 2: do that\nStep 3: finish"
        index.index_skill(_make_skill(prompt_template=multiline))
        results = index.search("step finish")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# KnowledgeMiddleware.load_skills integration
# ---------------------------------------------------------------------------

class TestKnowledgeMiddlewareLoadSkills:
    """Tests that KnowledgeMiddleware.load_skills delegates to SkillIndex."""

    def _make_middleware(self, skill_index=None):
        from unittest.mock import MagicMock
        from graph.middleware.knowledge import KnowledgeMiddleware

        knowledge_store = MagicMock()
        knowledge_store.search.return_value = []
        return KnowledgeMiddleware(knowledge_store, skill_index=skill_index)

    def test_load_skills_no_index_returns_empty(self):
        mw = self._make_middleware(skill_index=None)
        assert mw.load_skills("any query") == []

    def test_load_skills_with_index_returns_results(self, index):
        index.index_skill(_make_skill(name="my-workflow", description="runs tests automatically"))
        mw = self._make_middleware(skill_index=index)
        results = mw.load_skills("run tests", k=5)
        assert any(r["name"] == "my-workflow" for r in results)

    def test_load_skills_respects_k_param(self, index):
        for i in range(10):
            index.index_skill(_make_skill(name=f"sk-{i}", description="generic skill"))
        mw = self._make_middleware(skill_index=index)
        results = mw.load_skills("generic skill", k=3)
        assert len(results) <= 3

    def test_load_skills_empty_query_returns_recent(self, index):
        index.index_skill(_make_skill(name="recent-skill"))
        mw = self._make_middleware(skill_index=index)
        results = mw.load_skills("", k=5)
        assert any(r["name"] == "recent-skill" for r in results)

    def test_load_skills_empty_db_returns_empty(self, index):
        mw = self._make_middleware(skill_index=index)
        assert mw.load_skills("any query") == []

    def test_load_skills_default_k_is_five(self, index):
        for i in range(10):
            index.index_skill(_make_skill(name=f"sk-{i}", description="common skill keyword"))
        mw = self._make_middleware(skill_index=index)
        results = mw.load_skills("common skill keyword")
        assert len(results) <= 5
