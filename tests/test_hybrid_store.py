"""Tests for HybridKnowledgeStore — embeddings-on-FTS5 reference subclass."""

import sqlite3

import pytest

from knowledge.hybrid_store import HybridKnowledgeStore

_VOCAB = ["calculator", "math", "weather", "forecast", "python", "async"]


def _bow_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding over a tiny vocab."""
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in _VOCAB]


def _db(tmp_path):
    return str(tmp_path / "kb.db")


def test_no_embed_fn_behaves_like_base(tmp_path):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=None)
    store.add_chunk("use the calculator for math", domain="general")
    results = store.search("calculator")
    assert results and any("calculator" in r["content"] for r in results)
    # No vector table side effects when embeddings are off.
    db = sqlite3.connect(_db(tmp_path))
    tbls = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert "chunk_vectors" not in tbls


def test_vector_persisted_on_add(tmp_path):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    cid = store.add_chunk("tomorrow's weather forecast", domain="general")
    db = sqlite3.connect(_db(tmp_path))
    row = db.execute("SELECT chunk_id FROM chunk_vectors WHERE chunk_id = ?", (cid,)).fetchone()
    db.close()
    assert row is not None


def test_hybrid_returns_relevant_chunk(tmp_path):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    store.add_chunk("use the calculator for math", domain="general")
    store.add_chunk("tomorrow's weather forecast", domain="general")
    results = store.search("math calculator", k=2)
    assert results
    assert any("calculator" in r["content"] for r in results)


def test_vector_only_hit_is_hydrated(tmp_path):
    """A chunk FTS5 can't match (no shared tokens) still surfaces via the
    vector ranking, and is hydrated into a full result dict."""
    const_vec = lambda text: [1.0, 0.0]  # everything maps to the same vector
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=const_vec)
    cid = store.add_chunk("alpha beta gamma", domain="general")
    # Query shares no lexical tokens with the chunk → FTS5 base is empty …
    results = store.search("zzzzz", k=5)
    # … but the vector ranking (cosine 1.0) surfaces it, hydrated.
    assert any(r["id"] == cid and r["table"] == "chunks" for r in results)
    assert all("preview" in r for r in results)


def test_circuit_breaker_falls_back_to_fts(tmp_path):
    calls = {"n": 0}

    def flaky_embed(text):
        calls["n"] += 1
        raise RuntimeError("embedding service down")

    store = HybridKnowledgeStore(
        _db(tmp_path), embed_fn=flaky_embed, breaker_threshold=2, breaker_cooldown_s=999,
    )
    # add_chunk still succeeds (FTS5 path); embedding just fails silently.
    store.add_chunk("use the calculator for math", domain="general")
    # Search never raises and returns FTS5 results despite the failing embedder.
    results = store.search("calculator")
    assert any("calculator" in r["content"] for r in results)
    # After the threshold, the breaker is open → embed_fn is no longer called.
    before = calls["n"]
    store.search("calculator")
    store.search("calculator")
    assert calls["n"] == before  # breaker short-circuits embed_fn
