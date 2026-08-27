"""Typed memory schema (#3072): additive columns for memory classification.

Tests the additive migration (memory_kind, subject, review_state, expires_at)
and the filter plumbing across all three store types without changing delivery
behavior (owned by #3187).
"""

from __future__ import annotations

import sqlite3

from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore


# ── basic add + retrieve ─────────────────────────────────────────────────────


def test_add_chunk_with_memory_kind(tmp_path):
    """New typed-memory fields are stored and returned on the Chunk."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk(
        "The operator prefers dark mode",
        domain="preferences",
        memory_kind="profile",
        subject="operator",
        review_state="confirmed",
        expires_at="2027-01-01T00:00:00+00:00",
    )
    assert cid is not None
    chunks = store.list_chunks(domain="preferences", limit=1)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.id == cid
    assert c.memory_kind == "profile"
    assert c.subject == "operator"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-01-01T00:00:00+00:00"
    # as_dict includes the new fields
    d = c.as_dict()
    assert d["memory_kind"] == "profile"
    assert d["subject"] == "operator"
    assert d["review_state"] == "confirmed"
    assert d["expires_at"] == "2027-01-01T00:00:00+00:00"


def test_add_chunk_without_kind_defaults_none(tmp_path):
    """Backward compatibility: omitting typed fields leaves them NULL."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("plain fact", domain="general")
    assert cid is not None
    c = store.list_chunks(limit=1)[0]
    assert c.memory_kind is None
    assert c.subject is None
    assert c.review_state is None
    assert c.expires_at is None
    d = c.as_dict()
    assert d["memory_kind"] is None
    assert d["subject"] is None
    assert d["review_state"] is None
    assert d["expires_at"] is None


# ── migration ────────────────────────────────────────────────────────────────


def test_migration_adds_columns(tmp_path):
    """A DB created without the typed-memory columns gets them added on next open."""
    path = tmp_path / "old.db"
    # Simulate a pre-#3072 schema: chunks table without the new columns.
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, "
        "source_type TEXT, finding_type TEXT, namespace TEXT, epoch TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "invalidated_at TEXT, invalidation_reason TEXT)"
    )
    db.execute(
        "INSERT INTO chunks (content, domain, created_at, updated_at) "
        "VALUES ('legacy row', 'general', 'x', 'x')"
    )
    db.commit()
    db.close()

    # Opening the store triggers the migration.
    store = KnowledgeStore(path)
    store.add_chunk("new typed row", domain="general", memory_kind="fact", subject="project-x")

    rows = {c.content: c for c in store.list_chunks(limit=10)}
    assert rows["legacy row"].memory_kind is None
    assert rows["legacy row"].subject is None
    assert rows["new typed row"].memory_kind == "fact"
    assert rows["new typed row"].subject == "project-x"

    # Indexes were created.
    idx_db = sqlite3.connect(str(path))
    indexes = {r[1] for r in idx_db.execute("PRAGMA index_list(chunks)")}
    idx_db.close()
    assert "idx_chunks_memory_kind" in indexes
    assert "idx_chunks_review_state" in indexes


# ── search filters ───────────────────────────────────────────────────────────


def test_search_filter_by_kind(tmp_path):
    """search(memory_kind=...) returns only chunks of that kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("operator likes coffee", domain="prefs", memory_kind="profile")
    store.add_chunk("deploy on Friday", domain="prefs", memory_kind="decision")
    store.add_chunk("untyped preference", domain="prefs")

    hits = store.search("preference coffee deploy Friday", k=10, memory_kind="profile")
    assert len(hits) == 1
    assert hits[0]["memory_kind"] == "profile"

    # Unfiltered sees all.
    all_hits = store.search("preference coffee deploy Friday", k=10)
    assert len(all_hits) == 3


def test_search_filter_by_kind_like_fallback(tmp_path):
    """The LIKE fallback path also respects memory_kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("alpha fact", domain="d", memory_kind="fact")
    store.add_chunk("alpha decision", domain="d", memory_kind="decision")
    store._fts_available = False  # force LIKE path

    hits = store.search("alpha", k=10, memory_kind="fact")
    assert len(hits) == 1
    assert hits[0]["memory_kind"] == "fact"


def test_search_filter_by_review_state(tmp_path):
    """search(review_state=...) returns only chunks with that state."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("confirmed insight", domain="d", memory_kind="fact", review_state="confirmed")
    store.add_chunk("candidate insight", domain="d", memory_kind="fact", review_state="candidate")

    hits = store.search("insight", k=10, review_state="confirmed")
    assert len(hits) == 1
    assert hits[0]["review_state"] == "confirmed"


# ── list filters ─────────────────────────────────────────────────────────────


def test_list_filter_by_kind(tmp_path):
    """list_chunks(memory_kind=...) returns only chunks of that kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("profile entry", domain="d", memory_kind="profile")
    store.add_chunk("note entry", domain="d", memory_kind="note")
    store.add_chunk("untyped entry", domain="d")

    profile_chunks = store.list_chunks(memory_kind="profile")
    assert len(profile_chunks) == 1
    assert profile_chunks[0].memory_kind == "profile"

    note_chunks = store.list_chunks(memory_kind="note")
    assert len(note_chunks) == 1
    assert note_chunks[0].memory_kind == "note"


def test_list_filter_by_review_state(tmp_path):
    """list_chunks(review_state=...) returns only chunks with that state."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("confirmed", domain="d", review_state="confirmed")
    store.add_chunk("rejected", domain="d", review_state="rejected")
    store.add_chunk("unreviewed", domain="d")

    confirmed = store.list_chunks(review_state="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].review_state == "confirmed"


# ── hybrid store passthrough ─────────────────────────────────────────────────


def _const_embed(text: str) -> list[float]:
    return [1.0, 0.0]


def test_hybrid_store_passthrough(tmp_path):
    """HybridKnowledgeStore passes typed-memory fields through to the base."""
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    cid = store.add_chunk(
        "hybrid typed fact",
        domain="d",
        memory_kind="standing",
        subject="the-project",
        review_state="candidate",
    )
    assert cid is not None

    # Searchable with the kind filter (both FTS and vector paths).
    hits = store.search("hybrid typed", k=5, memory_kind="standing")
    assert any(r["id"] == cid for r in hits)

    # No hits for a different kind.
    misses = store.search("hybrid typed", k=5, memory_kind="profile")
    assert not any(r["id"] == cid for r in misses)


def test_hybrid_search_review_state_filters_both_rankings(tmp_path):
    """memory_kind and review_state filter both FTS5 and vector rankings."""
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    store.add_chunk("gamma delta", domain="d", memory_kind="fact", review_state="confirmed")
    store.add_chunk("gamma delta too", domain="d", memory_kind="fact", review_state="candidate")

    # Filter by review_state — vector-only path (no shared tokens with query).
    hits = store.search("zzzzz", k=5, review_state="confirmed")
    assert len(hits) == 1
    assert hits[0]["review_state"] == "confirmed"


# ── layered store passthrough ────────────────────────────────────────────────


def test_layered_store_passthrough(tmp_path):
    """LayeredKnowledgeStore passes typed-memory fields through on writes and reads."""
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)

    # Write via the layered store (targets private via __getattr__).
    layered.add_chunk("layered typed fact", domain="d", memory_kind="episode", subject="session-42")

    # Verify on the private store directly.
    c = private.list_chunks(domain="d", limit=1)[0]
    assert c.memory_kind == "episode"
    assert c.subject == "session-42"

    # Search with kind filter through the layered store.
    hits = layered.search("layered typed", k=5, memory_kind="episode")
    assert len(hits) == 1
    assert hits[0]["content"] == "layered typed fact"

    # No hits for a different kind.
    assert layered.search("layered typed", k=5, memory_kind="profile") == []


def test_layered_search_filters_both_tiers(tmp_path):
    """memory_kind filter is passed through to both private and commons tiers."""
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)

    private.add_chunk("private fact", domain="d", memory_kind="fact")
    commons.add_chunk("commons decision", domain="d", memory_kind="decision")

    fact_hits = layered.search("private commons fact decision", k=10, memory_kind="fact")
    assert len(fact_hits) == 1
    assert fact_hits[0]["content"] == "private fact"

    decision_hits = layered.search("private commons fact decision", k=10, memory_kind="decision")
    assert len(decision_hits) == 1
    assert decision_hits[0]["content"] == "commons decision"


# ── existing rows unaffected ─────────────────────────────────────────────────


def test_existing_rows_unaffected(tmp_path):
    """Legacy rows (no typed-memory fields) remain fully searchable and listable."""
    store = KnowledgeStore(tmp_path / "kb.db")
    # Write a legacy-style row (no typed fields).
    cid = store.add_chunk(
        "I remember the old days",
        domain="general",
        heading="nostalgia",
        source="conversation",
        source_type="chat",
        namespace="ns1",
        epoch="e1",
    )
    assert cid is not None

    # All existing fields intact.
    c = store.list_chunks(limit=1)[0]
    assert c.content == "I remember the old days"
    assert c.domain == "general"
    assert c.heading == "nostalgia"
    assert c.source == "conversation"
    assert c.source_type == "chat"
    assert c.namespace == "ns1"
    assert c.epoch == "e1"
    assert c.memory_kind is None
    assert c.subject is None
    assert c.review_state is None
    assert c.expires_at is None

    # Searchable.
    hits = store.search("old days", k=5)
    assert len(hits) == 1
    assert hits[0]["id"] == cid

    # Unfiltered list still includes it.
    all_chunks = store.list_chunks(limit=50)
    assert any(ch.id == cid for ch in all_chunks)

    # Kind-filtered search excludes it (NULL != 'fact').
    typed_hits = store.search("old days", k=5, memory_kind="fact")
    assert len(typed_hits) == 0


# ── add_document passthrough ─────────────────────────────────────────────────


def test_add_document_passes_typed_fields(tmp_path):
    """add_document forwards typed-memory kwargs to each chunk's add_chunk."""
    store = KnowledgeStore(tmp_path / "kb.db")
    ids = store.add_document(
        "doc-sized content for classification",
        domain="d",
        memory_kind="reference",
        subject="api-docs",
        review_state="confirmed",
        expires_at="2027-06-01T00:00:00+00:00",
    )
    assert len(ids) >= 1
    c = store.list_chunks(domain="d", limit=1)[0]
    assert c.memory_kind == "reference"
    assert c.subject == "api-docs"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-06-01T00:00:00+00:00"
