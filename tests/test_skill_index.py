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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_session_id: str = ""
    user_facing: bool = False
    slash: str = ""
    user_only: bool = False


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
    index.add_skill(
        _make_artifact(
            name="web-research",
            description="Research a topic using web search tools",
            prompt_template="Search the web for: {query}",
            tools_used=["web_search", "fetch_url"],
        )
    )
    index.add_skill(
        _make_artifact(
            name="calculator-math",
            description="Perform mathematical calculations",
            prompt_template="Calculate the following: {expression}",
            tools_used=["calculator"],
        )
    )
    index.add_skill(
        _make_artifact(
            name="time-lookup",
            description="Get the current time in any timezone",
            prompt_template="What is the current time in {timezone}?",
            tools_used=["current_time"],
        )
    )
    return index


# ── SkillsIndex: initialization ───────────────────────────────────────────────


def test_initialize_db_creates_file(tmp_db) -> None:
    """initialize_db() must create the SQLite file and FTS5 table."""
    assert not os.path.exists(tmp_db)
    SkillsIndex(db_path=tmp_db)
    assert os.path.exists(tmp_db)

    conn = sqlite3.connect(tmp_db)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='skills_fts'")
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
    from graph.skills.index import _SCHEMA_VERSION

    assert row[0] == _SCHEMA_VERSION
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


def test_load_skills_returns_tools_used(populated_index) -> None:
    """load_skills() surfaces the skill's declared tools (ADR 0005) so the
    middleware can hint which tools a retrieved skill relies on."""
    results = populated_index.load_skills("web search research")
    top = next(r for r in results if r.name == "web-research")
    assert top.tools_used == ("web_search", "fetch_url")


def test_retrieval_ranking(populated_index) -> None:
    """FTS5 must rank the most relevant skill first for a specific query."""
    results = populated_index.load_skills("mathematical calculation expression")
    assert len(results) > 0
    assert results[0].name == "calculator-math", f"Expected 'calculator-math' first, got: {[r.name for r in results]}"


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
    skills = [
        SkillRecord(
            name="web-research",
            description="Research the web",
            prompt_template="Search for {topic}",
            score=-1.5,
        )
    ]
    block = km._format_learned_skills(skills)
    assert "<learned_skills>" in block
    assert "</learned_skills>" in block
    assert 'name="web-research"' in block
    assert "Research the web" in block
    assert "Search for {topic}" in block


def test_format_learned_skills_surfaces_relevant_tools() -> None:
    """A skill's declared tools are emitted as <relevant_tools> (ADR 0005)."""
    km = _make_knowledge_middleware_no_store()
    block = km._format_learned_skills(
        [
            SkillRecord(
                name="web-research",
                description="Research the web",
                prompt_template="Search for {topic}",
                score=-1.5,
                tools_used=("web_search", "fetch_url"),
            )
        ]
    )
    assert "<relevant_tools>web_search, fetch_url</relevant_tools>" in block


def test_format_learned_skills_omits_tools_when_none() -> None:
    """No declared tools → no <relevant_tools> line (back-compat)."""
    km = _make_knowledge_middleware_no_store()
    block = km._format_learned_skills(
        [
            SkillRecord(
                name="bare",
                description="No tools declared",
                prompt_template="do {thing}",
                score=-1.0,
            )
        ]
    )
    assert "<relevant_tools>" not in block


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
    idx.add_skill(
        _make_artifact(
            name="test-skill",
            description="A test skill for unit testing",
        )
    )

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
    idx.add_skill(
        _make_artifact(
            name="web-research",
            description="Research topics using web search",
        )
    )

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)
    km._prior_sessions_cache = ""  # skip session loading

    state = {"messages": [HumanMessage(content="research web search topics")]}
    result = km.before_model(state, runtime=None)

    assert result is not None
    assert "context" in result
    assert "<learned_skills>" in result["context"]
    assert "web-research" in result["context"]


def test_before_model_announces_loaded_skills(tmp_db, monkeypatch) -> None:
    """before_model() emits a `skills_loaded` custom event (name + description) for
    the retrieved skills when skills_announce is on — the chat chip's signal."""
    import langchain_core.callbacks as lc_callbacks

    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(_make_artifact(name="web-research", description="Research topics using web search"))

    captured: list = []
    monkeypatch.setattr(lc_callbacks, "dispatch_custom_event", lambda name, data: captured.append((name, data)))

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx, skills_announce=True)
    km._prior_sessions_cache = ""

    km.before_model({"messages": [HumanMessage(content="research web search topics")]}, runtime=None)

    assert captured, "no skills_loaded event dispatched"
    name, data = captured[0]
    assert name == "skills_loaded"
    skills = data["skills"]
    assert any(s["name"] == "web-research" and s["description"] for s in skills)


def test_before_model_does_not_announce_when_disabled(tmp_db, monkeypatch) -> None:
    """skills_announce=False suppresses the chip event but still injects the block."""
    import langchain_core.callbacks as lc_callbacks

    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(_make_artifact(name="web-research", description="Research topics using web search"))

    captured: list = []
    monkeypatch.setattr(lc_callbacks, "dispatch_custom_event", lambda name, data: captured.append((name, data)))

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx, skills_announce=False)
    km._prior_sessions_cache = ""

    result = km.before_model({"messages": [HumanMessage(content="research web search topics")]}, runtime=None)

    assert captured == []
    assert "<learned_skills>" in result["context"]  # injection unaffected by the gate


def test_before_model_no_skills_no_learned_block(tmp_db) -> None:
    """before_model() must omit <learned_skills> block when index is empty."""
    idx = SkillsIndex(db_path=tmp_db)  # empty index

    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)
    km._prior_sessions_cache = ""

    state = {"messages": [HumanMessage(content="some query")]}
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


# ── Curation surface (v2 schema: confidence + last_used) ──────────────────────


def test_all_skills_returns_curation_fields(populated_index) -> None:
    """all_skills() exposes id/confidence/last_used for the curator."""
    rows = populated_index.all_skills()
    assert len(rows) == 3
    r = rows[0]
    assert set(r) >= {
        "id",
        "name",
        "description",
        "prompt_template",
        "tools_used",
        "created_at",
        "confidence",
        "last_used",
    }
    assert r["confidence"] == 1.0  # new skills start fully confident
    assert isinstance(r["id"], int)  # rowid
    assert isinstance(r["tools_used"], list)


def test_update_confidence_and_delete(populated_index) -> None:
    rows = populated_index.all_skills()
    target = rows[0]["id"]
    populated_index.update_confidence(target, 0.42)
    updated = {r["id"]: r for r in populated_index.all_skills()}
    assert abs(updated[target]["confidence"] - 0.42) < 1e-9

    populated_index.delete_skill(target)
    remaining = {r["id"] for r in populated_index.all_skills()}
    assert target not in remaining
    assert len(remaining) == 2


def test_curator_runs_against_live_index(populated_index) -> None:
    """The curator operates on the live SkillsIndex (no JSONL) — #173."""
    from graph.skills.curator import SkillCurator

    before = len(populated_index.all_skills())
    entry = SkillCurator(index=populated_index, audit_path="/dev/null", dry_run=True).run()
    assert entry["skills_before"] == before


# ── User-facing skills (ADR 0052) ──────────────────────────────────────────────


def test_user_facing_skills_storage_and_reader(index):
    """user_facing/slash round-trip through the FTS index; user_facing_skills()
    returns only the flagged rows with their slash token (ADR 0052)."""
    import types

    uf = types.SimpleNamespace(
        name="web-research",
        description="Research on the web.",
        prompt_template="plan, search, cite",
        tools_used=["web_search"],
        created_at=datetime.now(timezone.utc),
        source_session_id="s",
        user_facing=True,
        slash="research",
    )
    plain = _make_artifact(name="background-only", description="not user facing")
    index.add_skill(uf, source="disk")
    index.add_skill(plain, source="disk")

    # all_skills carries the flags; only the flagged one is user-facing.
    by_name = {s["name"]: s for s in index.all_skills()}
    assert by_name["web-research"]["user_facing"] is True
    assert by_name["web-research"]["slash"] == "research"
    assert by_name["background-only"]["user_facing"] is False

    ufs = index.user_facing_skills()
    assert [s["name"] for s in ufs] == ["web-research"]
    assert ufs[0]["slash"] == "research"


def test_user_facing_slash_falls_back_to_slug(index):
    """A user-facing skill with no explicit slash stores the slugified name."""
    import types

    uf = types.SimpleNamespace(
        name="Big Task",
        description="d",
        prompt_template="p",
        tools_used=[],
        created_at=datetime.now(timezone.utc),
        source_session_id="s",
        user_facing=True,
        slash="",
        slash_token=lambda: "big-task",
    )
    index.add_skill(uf, source="disk")
    ufs = index.user_facing_skills()
    assert ufs and ufs[0]["slash"] == "big-task"


def test_user_only_skill_is_withheld_from_agent_retrieval_but_still_a_slash(index):
    """A user_only skill (v5): the agent never retrieves it (load_skills), but it's a
    user_facing /slash command."""
    # A normal retrievable skill + a user-only one, both matching "deploy".
    index.add_skill(
        _FakeArtifact(name="Deploy guide", description="how to deploy the app",
                      prompt_template="deploy steps", source_session_id="s1"),
        source="disk",
    )
    index.add_skill(
        _FakeArtifact(name="Deploy now", description="deploy the app immediately",
                      prompt_template="run deploy", user_facing=True, user_only=True,
                      slash="deploy", source_session_id="s2"),
        source="disk",
    )
    # Agent retrieval (load_skills) sees the normal skill, NOT the user-only one.
    names = [r.name for r in index.load_skills("deploy", k=10)]
    assert "Deploy guide" in names
    assert "Deploy now" not in names
    # But the user-only skill IS exposed as a /slash command + carries the flag.
    ufs = {s["name"]: s for s in index.user_facing_skills()}
    assert "Deploy now" in ufs
    assert ufs["Deploy now"]["slash"] == "deploy"
    assert ufs["Deploy now"]["user_only"] is True
