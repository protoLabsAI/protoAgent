"""ADR 0069 D9 — deterministic staleness: supersede, don't delete.

Facts that revise existing facts mark the old row ``invalidated_at`` (kept for
audit) instead of updating in place or deleting; every default retrieval path
(search across all three stores, hot memory, memory_recall) excludes
invalidated rows, with ``include_invalidated=True`` as the audit escape hatch;
auto-injected RAG lines carry the chunk's stored date as a deterministic
recency signal. No LLM freshness judging anywhere.
"""

from __future__ import annotations

import asyncio
import sqlite3

from langchain_core.messages import HumanMessage

from graph.memory_facts import consolidate_and_store
from graph.middleware.knowledge import KnowledgeMiddleware
from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore
from tools.lg_tools import _build_memory_tools


# ── migration: pre-existing DBs gain invalidated_at, idempotently ───────────


_OLD_SCHEMA = """
CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    domain        TEXT NOT NULL DEFAULT 'general',
    heading       TEXT,
    source        TEXT,
    source_type   TEXT,
    finding_type  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def test_migration_adds_invalidated_at_to_preexisting_db(tmp_path):
    """A DB created before the column existed is migrated in place (ALTER TABLE
    + index, the namespace precedent) and its rows stay valid + searchable."""
    db_path = tmp_path / "old.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(_OLD_SCHEMA)  # pre-namespace, pre-invalidated_at era
    db.execute(
        "INSERT INTO chunks (content, domain, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("legacy fact about the gateway", "fact", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    db.commit()
    db.close()

    store = KnowledgeStore(db_path)
    db = sqlite3.connect(str(db_path))
    cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    indexes = {r[1] for r in db.execute("PRAGMA index_list(chunks)")}
    db.close()
    assert "invalidated_at" in cols
    assert "idx_chunks_invalidated_at" in indexes
    # The migrated row is NULL-invalidated (valid) and still retrievable.
    hits = store.search("legacy gateway")
    assert hits and hits[0]["invalidated_at"] is None

    # Idempotent: re-opening the migrated DB is a no-op, nothing lost.
    KnowledgeStore(db_path)
    assert KnowledgeStore(db_path).search("legacy gateway")


# ── invalidate_chunk ─────────────────────────────────────────────────────────


def test_invalidate_chunk_stamps_and_keeps_the_row(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("deploys go out Fridays", domain="fact")
    assert store.invalidate_chunk(cid) is True
    # The row survives (audit history) — it is not deleted.
    row = store.get_chunk(cid)
    assert row is not None and row["invalidated_at"]
    # Already-invalidated and unknown ids report False (nothing changed).
    assert store.invalidate_chunk(cid) is False
    assert store.invalidate_chunk(99999) is False


# ── default retrieval excludes; include_invalidated is the escape hatch ─────


def test_search_excludes_invalidated_by_default(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("the gateway alias is protolabs/reasoning", domain="fact")
    assert store.search("gateway alias")  # valid → found
    store.invalidate_chunk(cid)
    assert store.search("gateway alias") == []  # superseded → hidden
    hits = store.search("gateway alias", include_invalidated=True)  # audit hatch
    assert len(hits) == 1 and hits[0]["invalidated_at"]


def test_like_fallback_excludes_invalidated(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("the gateway alias is protolabs/reasoning", domain="fact")
    store.invalidate_chunk(cid)
    store._fts_available = False  # force the LIKE path
    assert store.search("gateway alias") == []
    assert len(store.search("gateway alias", include_invalidated=True)) == 1


def test_list_chunks_excludes_invalidated_by_default(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    keep = store.add_chunk("releases are cut manually", domain="fact")
    gone = store.add_chunk("releases are automated", domain="fact")
    store.invalidate_chunk(gone)
    ids = {c.id for c in store.list_chunks(domain="fact")}
    assert ids == {keep}
    all_ids = {c.id for c in store.list_chunks(domain="fact", include_invalidated=True)}
    assert all_ids == {keep, gone}


def test_hybrid_vector_ranking_excludes_invalidated(tmp_path):
    """A vector-only hit (no shared tokens with the query) must also honor
    invalidated_at — both rankings are filtered, not just FTS5."""
    store = HybridKnowledgeStore(str(tmp_path / "kb.db"), embed_fn=lambda text: [1.0, 0.0])
    cid = store.add_chunk("alpha beta gamma", domain="general")
    assert any(r["id"] == cid for r in store.search("zzzzz", k=5))  # vector-only hit
    store.invalidate_chunk(cid)
    assert store.search("zzzzz", k=5) == []
    assert any(r["id"] == cid for r in store.search("zzzzz", k=5, include_invalidated=True))


def test_layered_search_passes_include_invalidated_to_both_tiers(tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    cid = private.add_chunk("the operator prefers metric units", domain="fact")
    private.invalidate_chunk(cid)
    layered = LayeredKnowledgeStore(private, commons)
    assert layered.search("metric units") == []
    assert layered.search("metric units", include_invalidated=True)


def test_hot_memory_excludes_invalidated(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("operator prefers metric units", domain="hot", heading="prefs")
    assert "metric units" in store.get_hot_memory()
    store.invalidate_chunk(cid)
    assert store.get_hot_memory() == ""
    assert store.get_hot_memory_entries() == []


def test_memory_recall_excludes_invalidated(tmp_path):
    """#1579 cited dates on recall; superseded rows must no longer appear."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("the deploy day is Friday", domain="fact", source="a2a:chat-1")
    recall = next(t for t in _build_memory_tools(store) if t.name == "memory_recall")
    assert "Friday" in asyncio.run(recall.ainvoke({"query": "deploy day"}))
    store.invalidate_chunk(cid)
    assert asyncio.run(recall.ainvoke({"query": "deploy day"})) == "No matches."


# ── supersede semantics in fact consolidation ────────────────────────────────


def test_revised_fact_supersedes_old_row(tmp_path):
    """A revised fact (same subject, changed details — token overlap in the
    supersede band) invalidates the old row and inserts the new one. The old
    row is kept (never deleted, never updated in place)."""
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["The operator's preferred deploy day is Friday"], namespace="p")
    counts = consolidate_and_store(store, ["The operator's preferred deploy day is Monday"], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}

    valid = store.list_chunks(domain="fact")
    assert [c.content for c in valid] == ["The operator's preferred deploy day is Monday"]
    everything = store.list_chunks(domain="fact", include_invalidated=True)
    assert len(everything) == 2  # history kept
    old = next(c for c in everything if "Friday" in c.content)
    new = next(c for c in everything if "Monday" in c.content)
    assert old.invalidated_at and new.invalidated_at is None
    # Retrieval now only surfaces the current fact.
    hits = store.search("preferred deploy day")
    assert len(hits) == 1 and "Monday" in hits[0]["content"]


def test_supersede_applies_within_one_batch(tmp_path):
    """The batch-local bookkeeping supersedes too: a later fact in the same
    call revises an earlier one."""
    store = KnowledgeStore(tmp_path / "kb.db")
    counts = consolidate_and_store(
        store,
        [
            "The operator's preferred deploy day is Friday",
            "The operator's preferred deploy day is Monday",
        ],
        namespace="p",
    )
    assert counts == {"added": 2, "skipped": 0, "superseded": 1}
    assert [c.content for c in store.list_chunks(domain="fact")] == ["The operator's preferred deploy day is Monday"]


def test_unrelated_facts_do_not_supersede(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["The gateway alias is protolabs/reasoning"], namespace="p")
    counts = consolidate_and_store(store, ["Releases are cut manually"], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert len(store.list_chunks(domain="fact")) == 2


def test_consolidate_degrades_without_invalidate_chunk(tmp_path):
    """A minimal backend without invalidate_chunk still gets the new fact
    (add-only degrade — never raises, never blocks the write)."""

    class MinimalStore:
        def __init__(self, inner):
            self._inner = inner

        def list_chunks(self, **kwargs):
            kwargs.pop("include_invalidated", None)
            return self._inner.list_chunks(**kwargs)

        def add_chunk(self, *args, **kwargs):
            return self._inner.add_chunk(*args, **kwargs)

    inner = KnowledgeStore(tmp_path / "kb.db")
    store = MinimalStore(inner)
    consolidate_and_store(store, ["The operator's preferred deploy day is Friday"], namespace="p")
    counts = consolidate_and_store(store, ["The operator's preferred deploy day is Monday"], namespace="p")
    assert counts["added"] == 1 and counts["superseded"] == 0  # no invalidation half
    assert len(inner.list_chunks(domain="fact")) == 2  # both rows remain valid


# ── recency surfaced in the auto-injected context ────────────────────────────


def test_injected_rag_lines_carry_stored_date(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("the gateway alias is protolabs/reasoning", domain="fact")
    stored_date = store.list_chunks(limit=1)[0].created_at[:10]

    km = KnowledgeMiddleware(knowledge_store=store)
    km._prior_sessions_cache = ""  # skip session loading
    result = km.before_model({"messages": [HumanMessage(content="what is the gateway alias?")]}, runtime=None)
    assert result is not None
    # The stored date leads the hit's metadata suffix; the trust tier label
    # rides the same parens (ADR 0069 D8).
    assert f"(stored {stored_date};" in result["context"]


def test_injection_skips_invalidated_chunks(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("the gateway alias is protolabs/reasoning", domain="fact")
    store.invalidate_chunk(cid)

    km = KnowledgeMiddleware(knowledge_store=store)
    km._prior_sessions_cache = ""
    result = km.before_model({"messages": [HumanMessage(content="what is the gateway alias?")]}, runtime=None)
    assert result is None or "protolabs/reasoning" not in result.get("context", "")
