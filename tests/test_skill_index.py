"""Unit and integration tests for graph/skills/index.py.

Covers (progressive disclosure, ADR 0060):
- FTS5 database initialization (idempotent schema creation)
- add_skill() indexing
- skill_summaries() / discoverable_count() / get_skill() — the always-on index +
  on-demand body lookup that replaced per-turn BM25 retrieval
- Empty DB / absent skill graceful handling
- Schema migration: backup + recreate on version mismatch
- <available_skills> block in KnowledgeMiddleware.before_model output
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage

from graph.skills.index import SkillsIndex
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
    results = idx2.skill_summaries()
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
    """add_skill() must insert a row that appears in the index."""
    index.add_skill(_make_artifact(name="my-skill", description="does something useful"))
    results = index.skill_summaries()
    assert any(r["name"] == "my-skill" for r in results)


def test_add_skill_empty_name_skipped(index) -> None:
    """add_skill() must silently skip artifacts with empty names."""
    index.add_skill(_make_artifact(name=""))
    assert index.skill_summaries() == []


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


# ── SkillsIndex: skill_summaries / discoverable_count / get_skill (ADR 0060) ───


def test_skill_summaries_empty_db(index) -> None:
    """skill_summaries() must return an empty list on an empty database."""
    assert index.skill_summaries() == []
    assert index.discoverable_count() == 0


def test_skill_summaries_lists_name_description_slash(populated_index) -> None:
    """The index is the lightweight {name, description, slash} of every skill —
    not the full body (that's get_skill)."""
    rows = populated_index.skill_summaries()
    assert len(rows) == 3
    r = next(r for r in rows if r["name"] == "web-research")
    assert r["description"] == "Research a topic using web search tools"
    assert set(r) == {"name", "description", "slash"}
    assert "prompt_template" not in r  # body is loaded on demand, not listed


def test_skill_summaries_limit(populated_index) -> None:
    """skill_summaries(limit=) caps the index (the per-turn 'table of contents')."""
    assert len(populated_index.skill_summaries(limit=2)) == 2
    assert len(populated_index.skill_summaries()) == 3  # None = all


def test_discoverable_count_counts_all_discoverable(populated_index) -> None:
    assert populated_index.discoverable_count() == 3


def test_get_skill_returns_full_body(populated_index) -> None:
    """get_skill() returns the full procedure (prompt_template + tools) on demand."""
    rec = populated_index.get_skill("web-research")
    assert rec is not None
    assert rec["prompt_template"] == "Search the web for: {query}"
    assert rec["tools_used"] == ["web_search", "fetch_url"]


def test_get_skill_absent_returns_none(populated_index) -> None:
    assert populated_index.get_skill("no-such-skill") is None
    assert populated_index.get_skill("") is None
    assert populated_index.get_skill("   ") is None


# ── SkillsIndex: rebuild_index ────────────────────────────────────────────────


def test_rebuild_index_clears_and_reindexes(index) -> None:
    """rebuild_index() must clear existing rows and insert new ones."""
    index.add_skill(_make_artifact(name="old-skill"))
    new_artifacts = [
        _make_artifact(name="new-skill-1", description="brand new skill one"),
        _make_artifact(name="new-skill-2", description="brand new skill two"),
    ]
    index.rebuild_index(new_artifacts)

    # Old skill should not appear; new ones should.
    names = {r["name"] for r in index.skill_summaries()}
    assert "old-skill" not in names
    assert {"new-skill-1", "new-skill-2"} <= names


# ── Schema migration ──────────────────────────────────────────────────────────


def test_migration_empty_fork(tmp_db) -> None:
    """First run on empty path must create schema without error."""
    idx = SkillsIndex(db_path=tmp_db)
    assert os.path.exists(tmp_db)
    # Must be usable immediately
    idx.add_skill(_make_artifact())
    assert len(idx.skill_summaries()) == 1


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
    assert len(idx.skill_summaries()) == 1


def test_migration_compatible_schema_no_backup(tmp_db) -> None:
    """Compatible schema must not trigger a backup."""
    SkillsIndex(db_path=tmp_db)
    bak_path = tmp_db + ".bak"
    # Re-open — should not create a backup
    SkillsIndex(db_path=tmp_db)
    assert not os.path.exists(bak_path), "No backup should be created for compatible schema"


# ── KnowledgeMiddleware: <available_skills> injection (ADR 0060) ──────────────


def _make_knowledge_middleware_no_store() -> KnowledgeMiddleware:
    """Return a KnowledgeMiddleware with a stub store (no real DB)."""
    store = MagicMock()
    store.search.return_value = []
    return KnowledgeMiddleware(knowledge_store=store)


def _skills_km(idx) -> KnowledgeMiddleware:
    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx)
    km._prior_sessions_cache = ""  # skip session loading
    return km


def test_skill_index_block_empty_no_index() -> None:
    """No configured index → empty block (and before_model never adds it)."""
    km = _make_knowledge_middleware_no_store()
    assert km._skill_index_block() == ""


def test_before_model_injects_available_skills(tmp_db) -> None:
    """before_model() lists the index as an <available_skills> block — name +
    description, NOT the full body (that's load_skill's job)."""
    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(
        _make_artifact(
            name="web-research",
            description="Research topics using web search",
            prompt_template="SECRET-PROCEDURE-BODY",
        )
    )
    km = _skills_km(idx)

    # The query is irrelevant — the index is the same every turn (progressive disclosure).
    result = km.before_model({"messages": [HumanMessage(content="anything at all")]}, runtime=None)

    assert result is not None and "context" in result
    ctx = result["context"]
    assert "<available_skills>" in ctx
    assert "web-research" in ctx
    assert "Research topics using web search" in ctx
    assert "SECRET-PROCEDURE-BODY" not in ctx  # body is loaded on demand, not injected


def test_before_model_index_independent_of_query(tmp_db) -> None:
    """The same skills appear regardless of the user message — the old BM25 path
    guessed relevance from the conversation and mis-fired; the index does not."""
    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(_make_artifact(name="web-research", description="Research topics using web search"))
    km = _skills_km(idx)

    a = km.before_model({"messages": [HumanMessage(content="completely unrelated zebra")]}, runtime=None)
    assert "web-research" in a["context"]


def test_before_model_truncates_and_hints_more(tmp_db) -> None:
    """With more skills than skills_top_k, the block caps the list and hints at the rest."""
    idx = SkillsIndex(db_path=tmp_db)
    for i in range(5):
        idx.add_skill(_make_artifact(name=f"skill-{i}", description=f"does thing {i}"))
    store = MagicMock()
    store.search.return_value = []
    km = KnowledgeMiddleware(knowledge_store=store, skills_index=idx, skills_top_k=2)
    km._prior_sessions_cache = ""

    ctx = km.before_model({"messages": [HumanMessage(content="hi")]}, runtime=None)["context"]
    assert ctx.count("<skill ") == 2
    assert "+3 more" in ctx


def test_before_model_user_facing_skill_shows_slash(tmp_db) -> None:
    """A user-facing skill surfaces its /slash in the index."""
    import types

    idx = SkillsIndex(db_path=tmp_db)
    idx.add_skill(
        types.SimpleNamespace(
            name="web-research",
            description="Research on the web.",
            prompt_template="plan, search, cite",
            tools_used=["web_search"],
            created_at=datetime.now(timezone.utc),
            source_session_id="s",
            user_facing=True,
            slash="research",
        ),
        source="disk",
    )
    ctx = _skills_km(idx).before_model({"messages": [HumanMessage(content="x")]}, runtime=None)["context"]
    assert 'slash="/research"' in ctx


def test_before_model_no_skills_no_block(tmp_db) -> None:
    """before_model() must omit the block when the index is empty."""
    km = _skills_km(SkillsIndex(db_path=tmp_db))  # empty index
    result = km.before_model({"messages": [HumanMessage(content="some query")]}, runtime=None)
    if result is not None:
        assert "<available_skills>" not in result.get("context", "")


def test_before_model_no_skills_index_configured() -> None:
    """before_model() must not crash when skills_index is None."""
    km = _make_knowledge_middleware_no_store()  # no skills_index
    km._prior_sessions_cache = ""
    result = km.before_model({"messages": [HumanMessage(content="test query")]}, runtime=None)
    if result is not None:
        assert "<available_skills>" not in result.get("context", "")


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


def test_user_only_skill_is_withheld_from_agent_index_but_still_a_slash(index):
    """A user_only skill (v5): withheld from the agent's <available_skills> index
    (skill_summaries), but still a user_facing /slash command."""
    # A normal discoverable skill + a user-only one.
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
    # The always-on index (skill_summaries) shows the normal skill, NOT the user-only one.
    names = [r["name"] for r in index.skill_summaries()]
    assert "Deploy guide" in names
    assert "Deploy now" not in names
    assert index.discoverable_count() == 1
    # ...but get_skill still resolves it (so a /slash invocation can load its body).
    assert index.get_skill("Deploy now") is not None
    # But the user-only skill IS exposed as a /slash command + carries the flag.
    ufs = {s["name"]: s for s in index.user_facing_skills()}
    assert "Deploy now" in ufs
    assert ufs["Deploy now"]["slash"] == "deploy"
    assert ufs["Deploy now"]["user_only"] is True


# ── Concurrency ───────────────────────────────────────────────────────────────


def test_concurrent_access_is_thread_safe(populated_index) -> None:
    """The index keeps ONE sqlite connection reused across threads. The knowledge
    middleware reads it on the per-turn hot path (``skill_summaries``/``discoverable_count``)
    while the curator writes to it — concurrent use of a single connection races and
    corrupts cursor state (→ NULL/garbage cells → ``float(None)``, ``InterfaceError``,
    ``IndexError``). Every method touch must be serialized: hammer reads AND writes from
    many threads; nothing may raise, and reads must stay well-formed."""
    import threading

    index = populated_index
    errors: list[str] = []

    def reader() -> None:
        for _ in range(150):
            try:
                summaries = index.skill_summaries()
                assert all(isinstance(s, dict) and "name" in s for s in summaries)
                index.discoverable_count()
                index.get_skill("web-research")
                index.all_skills()
                index.user_facing_skills()
            except Exception as exc:  # noqa: BLE001 — a raised call is the regression
                errors.append(f"{type(exc).__name__}: {exc}")

    def writer() -> None:
        for i in range(150):
            try:
                # rowid 1 = web-research; churn its confidence to drive concurrent writes.
                index.update_confidence(1, 0.5 + (i % 5) / 10)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=reader) for _ in range(10)]
    threads += [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors[:3]}"
    # Index is still coherent + correctly typed after the pounding.
    assert index.discoverable_count() == 3
    ws = index.get_skill("web-research")
    assert ws is not None and isinstance(ws["confidence"], float)
