"""Typed memory schema (#3072, ADR 0107): memory_kind, subject, review_state, expires_at.

Verifies the additive migration, round-trip storage, filtered search, and
backward compatibility — no delivery behavior changes, only new columns and
filter dimensions.
"""

from __future__ import annotations

import sqlite3

from knowledge.store import KnowledgeStore


# ── migration ──────────────────────────────────────────────────────────────


def test_migration_adds_columns(tmp_path):
    """The 4 new columns exist after init (fresh DB)."""
    store = KnowledgeStore(tmp_path / "kb.db")
    db = sqlite3.connect(str(store.path))
    cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    db.close()
    assert "memory_kind" in cols
    assert "subject" in cols
    assert "review_state" in cols
    assert "expires_at" in cols


def test_migration_idempotent_on_existing_db(tmp_path):
    """Re-opening an already-migrated DB does not raise."""
    path = tmp_path / "kb.db"
    KnowledgeStore(path)
    # Second open triggers the same migration code path — must be a no-op.
    store2 = KnowledgeStore(path)
    db = sqlite3.connect(str(store2.path))
    cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    db.close()
    assert "memory_kind" in cols


# ── add_chunk with typed fields ────────────────────────────────────────────


def test_add_chunk_with_typed_fields(tmp_path):
    """Store and retrieve a chunk with all 4 new fields populated."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk(
        "The operator prefers dark mode.",
        domain="preferences",
        heading="ui-theme",
        memory_kind="profile",
        subject="operator-preferences",
        review_state="verified",
        expires_at="2027-01-01T00:00:00+00:00",
    )
    assert cid is not None

    row = store.get_chunk(cid)
    assert row is not None
    assert row["memory_kind"] == "profile"
    assert row["subject"] == "operator-preferences"
    assert row["review_state"] == "verified"
    assert row["expires_at"] == "2027-01-01T00:00:00+00:00"


# ── search filters ────────────────────────────────────────────────────────


def test_search_filter_by_kind(tmp_path):
    """search(memory_kind=...) returns only matching rows."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("auth migration decided on JWT", memory_kind="decision")
    store.add_chunk("auth migration uses tokens", memory_kind="fact")

    results = store.search("auth migration", memory_kind="decision")
    assert len(results) == 1
    assert results[0]["memory_kind"] == "decision"

    results_all = store.search("auth migration")
    assert len(results_all) == 2


def test_search_filter_by_review_state(tmp_path):
    """search(review_state=...) returns only matching rows."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("verified insight about caching", review_state="verified")
    store.add_chunk("draft insight about caching", review_state="draft")

    results = store.search("caching", review_state="verified")
    assert len(results) == 1
    assert results[0]["review_state"] == "verified"


def test_search_filter_by_kind_and_review_state(tmp_path):
    """Both filters can be combined."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("decision A", memory_kind="decision", review_state="verified")
    store.add_chunk("decision B", memory_kind="decision", review_state="draft")
    store.add_chunk("fact C", memory_kind="fact", review_state="verified")

    results = store.search("decision fact", memory_kind="decision", review_state="verified")
    assert len(results) == 1
    assert "decision A" in results[0]["content"]


# ── list_chunks filters ───────────────────────────────────────────────────


def test_list_filter_by_kind(tmp_path):
    """list_chunks(memory_kind=...) returns only matching rows."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("profile item", memory_kind="profile")
    store.add_chunk("episode item", memory_kind="episode")
    store.add_chunk("untyped item")

    chunks = store.list_chunks(memory_kind="profile")
    assert len(chunks) == 1
    assert chunks[0].memory_kind == "profile"


def test_list_filter_by_review_state(tmp_path):
    """list_chunks(review_state=...) returns only matching rows."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("verified chunk", review_state="verified")
    store.add_chunk("draft chunk", review_state="draft")

    chunks = store.list_chunks(review_state="verified")
    assert len(chunks) == 1
    assert chunks[0].review_state == "verified"


# ── backward compatibility ────────────────────────────────────────────────


def test_backward_compat_no_typed_fields(tmp_path):
    """Existing add_chunk calls without new params still work."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("plain old fact", domain="general", heading="compat")
    assert cid is not None

    row = store.get_chunk(cid)
    assert row is not None
    assert row["memory_kind"] is None
    assert row["subject"] is None
    assert row["review_state"] is None
    assert row["expires_at"] is None


def test_legacy_rows_searchable(tmp_path):
    """Rows without typed fields still appear in unfiltered search."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("legacy knowledge about widgets")

    results = store.search("widgets")
    assert len(results) == 1
    assert "widgets" in results[0]["content"]


def test_legacy_rows_excluded_by_kind_filter(tmp_path):
    """Rows without memory_kind are excluded when a kind filter is applied."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("untyped widget info")
    store.add_chunk("typed widget info", memory_kind="fact")

    results = store.search("widget", memory_kind="fact")
    assert len(results) == 1
    assert results[0]["memory_kind"] == "fact"

    results_all = store.search("widget")
    assert len(results_all) == 2


# ── Chunk dataclass ───────────────────────────────────────────────────────


def test_chunk_as_dict_includes_typed_fields(tmp_path):
    """as_dict() includes the 4 new fields."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk(
        "test content",
        memory_kind="note",
        subject="testing",
        review_state="draft",
        expires_at="2027-06-01T00:00:00+00:00",
    )
    chunks = store.list_chunks()
    assert len(chunks) == 1
    d = chunks[0].as_dict()
    assert d["memory_kind"] == "note"
    assert d["subject"] == "testing"
    assert d["review_state"] == "draft"
    assert d["expires_at"] == "2027-06-01T00:00:00+00:00"


def test_chunk_as_dict_typed_fields_none_when_absent(tmp_path):
    """as_dict() typed fields are None when the chunk was stored without them."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("plain content")
    chunks = store.list_chunks()
    d = chunks[0].as_dict()
    assert d["memory_kind"] is None
    assert d["subject"] is None
    assert d["review_state"] is None
    assert d["expires_at"] is None


# ── expires_at round-trip ─────────────────────────────────────────────────


def test_expires_at_stored_and_returned(tmp_path):
    """expires_at round-trips correctly through store/search/list."""
    store = KnowledgeStore(tmp_path / "kb.db")
    exp = "2028-12-31T23:59:59+00:00"
    cid = store.add_chunk("expiring fact about deployment", expires_at=exp)

    # Via get_chunk (raw dict)
    row = store.get_chunk(cid)
    assert row["expires_at"] == exp

    # Via search
    results = store.search("deployment")
    assert len(results) == 1
    assert results[0]["expires_at"] == exp

    # Via list_chunks (Chunk dataclass)
    chunks = store.list_chunks()
    assert chunks[0].expires_at == exp
