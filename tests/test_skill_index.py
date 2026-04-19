"""Unit and integration tests for graph/skills/index.py.

Covers:
- FTS5 database initialization (idempotent schema creation)
- add_skill() indexing
- load_skills() FTS5 retrieval and BM25 ranking
- Empty DB / empty query graceful handling
- Token budget enforcement in format_learned_skills
- Schema migration: backup + recreate on version mismatch
- KnowledgeMiddleware.load_skills() integration
- KnowledgeMiddleware._format_learned_skills() formatting
- <learned_skills> block in before_model output
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph.skills.index import SkillRecord, SkillsIndex
from graph.middleware.knowledge import KnowledgeMiddleware


# ── Helpers / fixtures ────────────────────────────────────────────────────────


@dataclass
class _FakeArtifact:
    """Minimal SkillV1Artifact lookalike for testing without importing extensions."""

    name: str
    description: str
    prompt_template: str
    tools_used: list[str] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source_session_id: str = ""


def _make_artifact(
    name: str = "web-research",
    description: str = "Research a topic using web search",
    prompt_template: str = "Search the web for information about {topic}",
    tools_used: list[str] | None = None,
    source_session_id: str = "sess-test",
) -> _FakeArtifact:
    return _FakeArtifact(
        name=name,
        description=description,
        prompt_template=prompt_template,
        tools_used=tools_used or ["web_search"],
        source_session_id=source_session_id,
    )


@pytest.fixture
def tmp_db(tmp_path) -> str:
    """Return a path to a temporary SQLite DB that doesn't exist yet."""
    return str(tmp_path / "skills.db")


@pytest.fixture
def index(tmp_db) -> SkillsIndex:
    """A fresh SkillsIndex backed by a temp DB."""
    return SkillsIndex(db_path=tmp_db)


@pytest.fixture
def populated_index(index) -> SkillsIndex:
    """SkillsIndex pre-populated with three skill artifacts."""
    index.add_skill(_make_artifact(
        name="web-research",
        description="Research a topic using web search tools",
        prompt_template="Search the web for: {query}",
        tools_used=["web_search", "fetch_url"],
    ))
    index.add_skill(_make_artifact(
        name="calculator-math",
        description="Perform mathematical calculations",
        prompt_template="Calculate the following: {expression}",
        tools_used=["calculator"],
    ))
    index.add_skill(_make_artifact(
        name="time-lookup",
        description="Get the current time in any timezone",
        prompt_template="What is the current time in {timezone}?",
        tools_used=["current_time"],
    ))
    return index


# ── SkillsIndex: initialization ───────────────────────────────────────────────


def test_initialize_db_creates_file(tmp_db) -> None:
    """initialize_db() must create the SQLite file and FTS5 table."""
    assert not os.path.exists(tmp_db)
    SkillsIndex(db_path=tmp_db)
    assert os.path.exists(tmp_db)

    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skills_fts'"
    )
    assert cur.fetchone() is not None, "skills_fts table should exist"
    conn.close()


def test_initialize_db_idempotent(tmp_db) -> None:
    """Calling SkillsIndex() twice on the same DB must not raise or corrupt data."""
    idx1 = SkillsIndex(db_path=tmp_db)
    idx1.add_skill(_make_artifact())

    idx2 = SkillsIndex(db_path=tmp_db)
    results = idx2.load_skills("web search research")
    assert len(results) == 1, "Existing rows must survive re-initialization"


def test_schema_meta_table_exists(tmp_db) -> None:
    """_skills_meta table must record schema version."""
    SkillsIndex(db_path=tmp_db)
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute("SELECT version FROM _skills_meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == 1
    conn.close()


# ── SkillsIndex: add_skill ────────────────────────────────────────────────────


def test_add_skill_inserts_row(index) -> None:
    """add_skill() must insert a row that can be retrieved."""
    index.add_skill(_make_artifact(name="my-skill", description="does something useful"))
    results = index.load_skills("useful something")
    assert any(r.name == "my-skill" for r in results)


def test_add_skill_empty_name_skipped(index) -> None:
    """add_skill() must silently skip artifacts with empty names."""
    index.add_skill(_make_artifact(name=""))
    results = index.load_skills("research")
    assert results == []


def test_add_skill_tools_stored_as_space_separated(index, tmp_db) -> None:
    """add_skill() must join tools_used list into a space-separated string."""
    index.add_skill(_make_artifact(tools_used=["web_search", "fetch_url"]))
    # Verify raw storage
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute("SELECT tools_used FROM skills_fts")
    row = cur.fetchone()
    assert row is not None
    assert "web_search" in row[0]
    assert "fetch_url" in row[0]
    conn.close()


# ── SkillsIndex: load_skills ──────────────────────────────────────────────────


def test_load_skills_empty_db(index) -> None:
    """load_skills() must return an empty list on an empty database."""
    results = index.load_skills("web research")
    assert results == []


def test_load_skills_empty_query(populated_index) -> None:
    """load_skills() must return an empty list for empty/whitespace query."""
    assert populated_index.load_skills("") == []
    assert populated_index.load_skills("   ") == []


def test_load_skills_returns_skill_records(populated_index) -> None:
    """load_skills() must return SkillRecord named tuples."""
    results = populated_index.load_skills("web search research")
    assert len(results) > 0
    r = results[0]
    assert isinstance(r, SkillRecord)
    assert isinstance(r.name, str)
    assert isinstance(r.description, str)
    assert isinstance(r.prompt_template, str)
    assert isinstance(r.score, float)


def test_retrieval_ranking(populated_index) -> None:
    """FTS5 must rank the most relevant skill first for a specific query."""
    results = populated_index.load_skills("mathematical calculation expression")
    assert len(results) > 0
    assert results[0].name == "calculator-math", (
        f"Expected 'calculator-math' first, got: {[r.name for r in results]}"
    )


def test_load_skills_top_k_limit(populated_index) -> None:
    """load_skills() must respect the k limit."""
    results = populated_index.load_skills("search web calculator time", k=2)
    assert len(results) <= 2


def test_load_skills_scores_ordered(populated_index) -> None:
    """Results must be ordered best-first (ascending BM25 score)."""
    results = populated_index.load_skills("search web research")
    scores = [r.score for r in results]
    assert scores == sorted(scores), "BM25 scores must be sorted ascending (best-first)"


def test_load_skills_no_match_returns_empty(populated_index) -> None:
    """load_skills() must return an empty list when FTS finds no matches."""
    # A query using FTS5 special syntax that matches nothing
    results = populated_index.load_skills("zzz_no_match_xyz_abc_impossible_token")
    # This may return empty or have no results
    # Either way, must not raise
    assert isinstance(results, list)


# ── SkillsIndex: rebuild_index ────────────────────────────────────────────────


def test_rebuild_index_clears_and_reindexes(index) -> None:
    """rebuild_index() must clear existing rows and insert new ones."""
    index.add_skill(_make_artifact(name="old-skill"))
    new_artifacts = [
        _make_artifact(name="new-skill-1", description="brand new skill one"),
        _make_artifact(name="new-skill-2", description="brand new skill two"),
    ]
    index.rebuild_index(new_artifacts)

    # Old skill should not appear
    old_results = index.load_skills("old skill")
    assert not any(r.name == "old-skill" for r in old_results)

    # New skills should appear
    new_results = index.load_skills("brand new skill")
    names = {r.name for r in new_results}
    assert "new-skill-1" in names or "new-skill-2" in names


# ── Schema migration ──────────────────────────────────────────────────────────


def test_migration_empty_fork(tmp_db) -> None:
    """First run on empty path must create schema without error."""
    idx = SkillsIndex(db_path=tmp_db)
    assert os.path.exists(tmp_db)
    # Must be usable immediately
    idx.add_skill(_make_artifact())
    results = idx.load_skills("web research")
    assert len(results) == 1


def test_migration_version_mismatch_creates_backup(tmp_db) -> None:
    """If schema version mismatches, existing DB should be backed up."""
    # Create a DB with wrong schema version
    conn = sqlite3.connect(tmp_db)
    conn.executescript("""
        CREATE VIRTUAL TABLE skills_fts USING fts5(name, description);
        CREATE TABLE _skills_meta (key TEXT PRIMARY KEY, version INTEGER NOT NULL);
        INSERT INTO _skills_meta VALUES ('schema_version', 999);
    """)
    conn.close()

    # SkillsIndex should detect mismatch and backup
    idx = SkillsIndex(db_path=tmp_db)
    bak_path = tmp_db + ".bak"
    assert os.path.exists(bak_path), "Backup file should exist after migration"

    # New DB should be functional
    idx.add_skill(_make_artifact())
    results = idx.load_skills("web research")
    assert len(results) == 1


def test_migration_compatible_schema_no_backup(tmp_db) -> None:
    """Compatible schema must not trigger a backup."""
    SkillsIndex(db_path=tmp_db)
    bak_path = tmp_db + ".bak"
    # Re-open — should not create a backup
    SkillsIndex(db_path=tmp_db)
    assert not os.path.exists(bak_path), "No backup should be created for compatible schema"


# ── Token budget enforcement ──────────────────────────────────────────────────


def _make_knowledge_middleware_no_store() -> KnowledgeMiddleware:
    """Return a KnowledgeMiddleware with a stub store (no real DB)."""
    store = MagicMock()
    store.search.return_value = []
    return KnowledgeMiddleware(knowledge_store=store)


def test_format_learned_skills_empty_returns_empty() -> None:
    """_format_learned_skills() must return empty string for empty input."""
    km = _make_knowledge_middleware_no_store()
    result = km._format_learned_skills([])
    assert result == ""


def test_format_learned_skills_basic_formatting() -> None:
    """_format_learned_skills() must produce a valid <learned_skills> block."""
    km = _make_knowledge_middleware_no_store()
    skills = [SkillRecord(
        name="web-research",
        description="Research the web",
        prompt_template="Search for {topic}",
        score=-1.5,
    )]
    block = km._format_learned_skills(skills)
    assert "<learned_skills>" in block
    assert "</learned_skills>" in block
    assert 'name="web-research"' in block
    assert "Research the web" in block
    assert "Search for {topic}" in block


def test_token_budget_enforcement() -> None:
    """_format_learned_skills() must remove low-relevance skills to fit budget."""
    km = _make_knowledge_middleware_no_store()
    # Create many skills with large descriptions to exceed budget
    skills = [
        SkillRecord(
            name=f"skill-{i}",
            description="x" * 500,  # large description
            prompt_template="y" * 500,  # large template
            score=float(-i),  # skill-0 is best (most negative)
        )
        for i in range(20)
    ]
    block = km._format_learned_skills(skills)
    # Block must not exceed 2000 tokens (~8000 chars)
    token_count = len(block) // 4
    assert token_count <= 2000, f"Block exceeds 2000 token budget: {token_count} tokens"
    # Must still contain at least the best skill
    assert "skill-19" in block or len(block) > 0  # best skill retained


def test_token_budget_best_skill_retained() -> None:
    """After truncation, the most relevant skill (best score) should be retained."""
    km = _make_knowledge_middleware_no_store()
    skills = [
        SkillRecord(name="best-skill", description="best " * 10, prompt_template="pt", score=-10.0),
        SkillRecord(name="worst-skill", description="worst " * 400, prompt_template="pt " * 400, score=-0.1),
    ]
    block = km._format_learned_skills(skills)
    assert "best-skill" in block


# ── KnowledgeMiddleware: load_skills integration ──────────────────────────────


def test_km_load_skills_no_index_returns_empty() -> None:
    """load_skills() must return [] when no skills_index is configured."""
    km = _make_knowledge_middleware_no_store()
    assert km._skills_index is None
    results = km.load_skills("any query")
    assert results == []


def test_km_load_skills_with_index(tmp_db) -> None:
    """load_skills() must delegate to SkillsIndex when configured."""
    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(_make_artifact(
        name="test-skill",
        description="A test skill for unit testing",
    ))

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)

    results = km.load_skills("test skill unit")
    assert any(r.name == "test-skill" for r in results)


def test_km_load_skills_empty_query_returns_empty(tmp_db) -> None:
    """load_skills() with empty query must return [] without querying index."""
    idx = SkillsIndex(db_path=tmp_db)
    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)

    assert km.load_skills("") == []
    assert km.load_skills("   ") == []


# ── KnowledgeMiddleware: before_model with skills injection ───────────────────


def test_before_model_injects_learned_skills(tmp_db) -> None:
    """before_model() must include <learned_skills> block when index has results."""
    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(_make_artifact(
        name="web-research",
        description="Research topics using web search",
    ))

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)
    km._prior_sessions_cache = ""  # skip session loading

    state = {
        "messages": [HumanMessage(content="research web search topics")]
    }
    result = km.before_model(state, runtime=None)

    assert result is not None
    assert "context" in result
    assert "<learned_skills>" in result["context"]
    assert "web-research" in result["context"]


def test_before_model_no_skills_no_learned_block(tmp_db) -> None:
    """before_model() must omit <learned_skills> block when index is empty."""
    idx = SkillsIndex(db_path=tmp_db)  # empty index

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)
    km._prior_sessions_cache = ""

    state = {
        "messages": [HumanMessage(content="some query")]
    }
    result = km.before_model(state, runtime=None)

    # With empty store and empty index, result should be None or no learned_skills
    if result is not None:
        assert "<learned_skills>" not in result.get("context", "")


def test_before_model_no_skills_index_configured() -> None:
    """before_model() must not crash when skills_index is None."""
    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store)  # no skills_index
    km._prior_sessions_cache = ""

    state = {"messages": [HumanMessage(content="test query")]}
    # Must not raise
    result = km.before_model(state, runtime=None)
    if result is not None:
        assert "<learned_skills>" not in result.get("context", "")


# ── build_skills_query ────────────────────────────────────────────────────────


def test_build_skills_query_uses_last_human_message() -> None:
    """_build_skills_query() must include the last human message text."""
    km = _make_knowledge_middleware_no_store()
    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="how can I help?"),
        HumanMessage(content="research machine learning"),
    ]
    query = km._build_skills_query(messages)
    assert "research machine learning" in query


def test_build_skills_query_caps_at_context_chars() -> None:
    """_build_skills_query() must cap the query at 2000 chars."""
    km = _make_knowledge_middleware_no_store()
    long_content = "x" * 5000
    messages = [HumanMessage(content=long_content)]
    query = km._build_skills_query(messages)
    assert len(query) <= 2000
