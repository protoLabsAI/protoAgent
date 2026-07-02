"""Knowledge lifecycle (#1634): purge_domain + epoch scoping.

Long-running plugins accumulate knowledge that becomes actively wrong
(spacetraders: weekly universe wipes). purge_domain retires a bucket outright —
consistently from EVERY index (main rows, FTS via the delete trigger, vectors on
the hybrid store) — and the epoch tag scopes retrieval to the current era so old
lessons stay for post-mortems without polluting search.
"""

from __future__ import annotations

import sqlite3

from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore


def _backdate(path, chunk_id: int, created_at: str) -> None:
    """Rewrite a row's created_at (every insert stamps 'now', so age is simulated)."""
    db = sqlite3.connect(str(path))
    db.execute("UPDATE chunks SET created_at = ? WHERE id = ?", (created_at, chunk_id))
    db.commit()
    db.close()


# ── purge_domain: base store ────────────────────────────────────────────────


def test_purge_domain_deletes_only_that_domain(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("dead market route", domain="st-routes")
    store.add_chunk("another dead route", domain="st-routes")
    store.add_chunk("still-good fact", domain="general")

    assert store.purge_domain("st-routes") == 2
    assert store.search("route", domain="st-routes") == []
    assert store.list_chunks(domain="st-routes") == []
    # The other domain is untouched.
    assert [c.content for c in store.list_chunks(domain="general")] == ["still-good fact"]


def test_purge_domain_with_before_deletes_only_the_stale_tail(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    old = store.add_chunk("old lesson", domain="st-routes")
    store.add_chunk("fresh lesson", domain="st-routes")
    _backdate(store.path, old, "2026-01-15T00:00:00+00:00")

    assert store.purge_domain("st-routes", before="2026-02-01") == 1
    remaining = [c.content for c in store.list_chunks(domain="st-routes")]
    assert remaining == ["fresh lesson"]
    # The purged chunk is gone from search too.
    assert all("old" not in r["content"] for r in store.search("lesson", domain="st-routes"))


def test_purge_domain_before_accepts_iso_variants(tmp_path):
    # A date-only string and a 'Z'-suffixed timestamp both normalize to the
    # stored UTC ISO format — strictly-before semantics on the boundary.
    store = KnowledgeStore(tmp_path / "kb.db")
    a = store.add_chunk("jan lesson", domain="d")
    b = store.add_chunk("feb lesson", domain="d")
    _backdate(store.path, a, "2026-01-15T12:00:00+00:00")
    _backdate(store.path, b, "2026-02-15T12:00:00+00:00")

    assert store.purge_domain("d", before="2026-02-01T00:00:00Z") == 1
    assert [c.content for c in store.list_chunks(domain="d")] == ["feb lesson"]
    assert store.purge_domain("d", before="2026-03-01") == 1
    assert store.list_chunks(domain="d") == []


def test_purge_domain_refuses_bad_input(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("keep me", domain="d")
    # Unparseable cutoff → refuse (0 deleted) rather than purge the wrong rows.
    assert store.purge_domain("d", before="not-a-timestamp") == 0
    # Empty domain → refuse (never a wipe-everything predicate).
    assert store.purge_domain("") == 0
    assert store.purge_domain("   ") == 0
    assert len(store.list_chunks(domain="d")) == 1


# ── purge_domain: hybrid store (vectors must go too) ────────────────────────


def _const_embed(text: str) -> list[float]:
    return [1.0, 0.0]  # every text maps to the same vector → cosine 1.0 hits


def test_hybrid_purge_removes_chunk_from_both_search_modes(tmp_path):
    path = tmp_path / "kb.db"
    store = HybridKnowledgeStore(path, embed_fn=_const_embed)
    cid = store.add_chunk("alpha beta gamma", domain="st-routes")
    # Present via BOTH modes before the purge: lexical (FTS) …
    assert any(r["id"] == cid for r in store.search("alpha", k=5))
    # … and vector-only (no shared tokens; const embedding surfaces it).
    assert any(r["id"] == cid for r in store.search("zzzzz", k=5))

    assert store.purge_domain("st-routes") == 1
    # Absent from BOTH modes after — no FTS ghost, no vector-only resurrection.
    assert not any(r.get("id") == cid for r in store.search("alpha", k=5))
    assert not any(r.get("id") == cid for r in store.search("zzzzz", k=5))
    # And the vector row itself is gone (no orphan in the side table).
    db = sqlite3.connect(str(path))
    n = db.execute("SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id = ?", (cid,)).fetchone()[0]
    db.close()
    assert n == 0


def test_hybrid_purge_with_before_keeps_fresh_vectors(tmp_path):
    path = tmp_path / "kb.db"
    store = HybridKnowledgeStore(path, embed_fn=_const_embed)
    old = store.add_chunk("old era route", domain="d")
    fresh = store.add_chunk("fresh era route", domain="d")
    _backdate(path, old, "2026-01-01T00:00:00+00:00")

    assert store.purge_domain("d", before="2026-02-01") == 1
    db = sqlite3.connect(str(path))
    left = {r[0] for r in db.execute("SELECT chunk_id FROM chunk_vectors")}
    db.close()
    assert left == {fresh}


def test_hybrid_purge_refuses_bad_cutoff_without_touching_vectors(tmp_path):
    path = tmp_path / "kb.db"
    store = HybridKnowledgeStore(path, embed_fn=_const_embed)
    cid = store.add_chunk("keep my vector", domain="d")
    assert store.purge_domain("d", before="garbage") == 0
    db = sqlite3.connect(str(path))
    n = db.execute("SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id = ?", (cid,)).fetchone()[0]
    db.close()
    assert n == 1  # the refusal happened BEFORE the vector delete


# ── purge_domain: layered store (private-only — the commons is curated) ─────


def test_layered_purge_targets_private_never_commons(tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)
    private.add_chunk("my stale lesson", domain="d")
    commons.add_chunk("fleet-curated lesson", domain="d")

    assert layered.purge_domain("d") == 1  # __getattr__ → private
    assert private.list_chunks(domain="d") == []
    assert [c.content for c in commons.list_chunks(domain="d")] == ["fleet-curated lesson"]


# ── epoch scoping ────────────────────────────────────────────────────────────


def test_epoch_tagged_add_and_filtered_search(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("route via market X", domain="st-routes", epoch="2026-06-22")
    store.add_chunk("route via market Y", domain="st-routes", epoch="2026-06-29")
    store.add_chunk("route wisdom, untagged", domain="st-routes")

    # Unfiltered search still sees every era (today's behavior).
    assert len(store.search("route", k=10)) == 3
    # Epoch-filtered search sees ONLY that era — other epochs AND untagged excluded.
    hits = store.search("route", k=10, epoch="2026-06-29")
    assert [r["content"] for r in hits] == ["route via market Y"]
    assert hits[0]["epoch"] == "2026-06-29"
    # A never-used epoch matches nothing.
    assert store.search("route", k=10, epoch="2099-01-01") == []


def test_epoch_filter_on_the_like_fallback(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("fallback lesson one", domain="d", epoch="e1")
    store.add_chunk("fallback lesson two", domain="d", epoch="e2")
    store._fts_available = False  # force the LIKE path
    hits = store.search("fallback", k=10, epoch="e2")
    assert [r["content"] for r in hits] == ["fallback lesson two"]


def test_epoch_filters_the_vector_ranking_too(tmp_path):
    # A vector-only hit (const embedding, no shared tokens) from another era must
    # not leak through an epoch-filtered hybrid search.
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    store.add_chunk("alpha beta", domain="d", epoch="e1")
    cid2 = store.add_chunk("gamma delta", domain="d", epoch="e2")
    hits = store.search("zzzzz", k=5, epoch="e2")  # vector-only path
    assert [r["id"] for r in hits] == [cid2]


def test_epoch_passes_through_the_layered_store(tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)
    private.add_chunk("private era lesson", domain="d", epoch="e1")
    commons.add_chunk("commons era lesson", domain="d", epoch="e2")

    assert [r["content"] for r in layered.search("lesson", epoch="e1")] == ["private era lesson"]
    assert [r["content"] for r in layered.search("lesson", epoch="e2")] == ["commons era lesson"]


def test_epoch_persists_on_the_chunk_and_add_document(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_document("doc-sized lesson", domain="d", epoch="e1")
    c = store.list_chunks(domain="d", limit=1)[0]
    assert c.epoch == "e1"
    assert c.as_dict()["epoch"] == "e1"


def test_epoch_migration_on_preexisting_db(tmp_path):
    """A DB created without the epoch column gets it added on next open."""
    path = tmp_path / "old.db"
    # Simulate a pre-#1634 schema: chunks table with no epoch column.
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, source_type TEXT, "
        "finding_type TEXT, namespace TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "invalidated_at TEXT)"
    )
    db.execute("INSERT INTO chunks (content, domain, created_at, updated_at) VALUES ('old row', 'd', 'x', 'x')")
    db.commit()
    db.close()

    store = KnowledgeStore(path)
    store.add_chunk("new era row", domain="d", epoch="e1")
    rows = {c.content: c.epoch for c in store.list_chunks(domain="d", limit=10)}
    assert rows["old row"] is None
    assert rows["new era row"] == "e1"
    # Old (untagged) rows drop out of an epoch-filtered search, stay in unfiltered.
    assert [r["content"] for r in store.search("row", k=10, epoch="e1")] == ["new era row"]
    assert len(store.search("row", k=10)) == 2
