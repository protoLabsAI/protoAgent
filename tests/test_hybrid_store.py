"""Tests for HybridKnowledgeStore — embeddings-on-FTS5 reference subclass."""

import math
import sqlite3
import time


from knowledge.hybrid_store import HybridKnowledgeStore

_VOCAB = ["calculator", "math", "weather", "forecast", "python", "async"]


def _bow_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding over a tiny vocab."""
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in _VOCAB]


def _db(tmp_path):
    return str(tmp_path / "kb.db")


def _multi_chunk_doc(n: int) -> str:
    """A doc of ``n`` padded paragraphs, each carrying a distinct vocab word so it
    both splits into multiple chunks (past chunk_max_chars=120) and stays
    FTS5-searchable / vector-embeddable."""
    return "\n\n".join(f"{_VOCAB[i % len(_VOCAB)]} section. " + "padding word " * 12 for i in range(n))


def _counting_embedders():
    """(single, batch, calls) — deterministic bow embedders that count calls."""
    calls = {"batch": 0, "single": 0}

    def single(text):
        calls["single"] += 1
        return _bow_embed(text)

    def batch(texts):
        calls["batch"] += 1
        return [_bow_embed(t) for t in texts]

    return single, batch, calls


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


def test_hybrid_results_carry_an_rrf_score(tmp_path):
    # #1043: a hybrid result exposes its RRF fused score, descending by rank.
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    store.add_chunk("use the calculator for math", domain="general")
    store.add_chunk("tomorrow's weather forecast", domain="general")
    results = store.search("math calculator", k=2)
    scores = [r.get("score") for r in results]
    assert all(isinstance(s, float) for s in scores)  # every hit scored
    assert scores == sorted(scores, reverse=True)  # ranked high→low


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
        _db(tmp_path),
        embed_fn=flaky_embed,
        breaker_threshold=2,
        breaker_cooldown_s=999,
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


def test_reset_embed_breaker_closes_an_open_breaker(tmp_path):
    fail = {"on": True}

    def embed(text):
        if fail["on"]:
            raise RuntimeError("down")
        return [1.0, 0.0]

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=embed,
        breaker_threshold=2,
        breaker_cooldown_s=999,
    )
    store.add_chunk("calculator math", domain="general")
    store.search("calculator")  # trips the failures
    store.search("calculator")  # breaker now open
    assert store._breaker_open()

    # Key fixed out-of-band; reset reports it actually cleared something.
    fail["on"] = False
    assert store.reset_embed_breaker() is True
    assert not store._breaker_open()
    # A no-op when already closed.
    assert store.reset_embed_breaker() is False
    # The embedder is exercised again now the breaker is closed.
    n = {"c": 0}
    orig = store._embed_fn

    def counting(t):
        n["c"] += 1
        return orig(t)

    store._embed_fn = counting
    store.search("calculator")
    assert n["c"] > 0  # embed_fn called again post-reset


# ── batched-embed slicing (#3126) ────────────────────────────────────────────


def test_batched_add_document_single_slice_one_call(tmp_path):
    """(6a) A document whose chunks fit one slice embeds in ONE batched call —
    no regression on the existing single-batch path, every chunk vectored."""
    single, batch, calls = _counting_embedders()
    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,  # default embed_batch_size=128 » chunk count
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    ids = store.add_document(_multi_chunk_doc(3), domain="general", heading="Doc")
    assert len(ids) >= 3
    assert calls["batch"] == 1 and calls["single"] == 0  # one slice, no per-chunk fallback
    assert store.count_vectors(ids) == len(ids)  # every chunk got a vector


def test_batched_add_document_spans_multiple_slices(tmp_path):
    """(6b/r1) A document larger than one slice embeds across MULTIPLE batched
    calls — the fix for silent vector loss on a book-sized document: every chunk
    is vectored, none dropped."""
    single, batch, calls = _counting_embedders()
    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,
        embed_batch_size=2,
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    ids = store.add_document(_multi_chunk_doc(4), domain="general", heading="Doc")
    assert len(ids) >= 3  # more than one slice of two
    assert calls["batch"] == math.ceil(len(ids) / 2)  # one batched call per slice
    assert calls["single"] == 0  # slices all succeeded → no fallback
    assert store.count_vectors(ids) == len(ids)  # all vectors stored, nothing silently lost
    assert store.search("weather")  # and they're usable for retrieval


def test_slice_failure_falls_back_to_per_chunk(tmp_path):
    """(6c) When a slice's batched embed fails, fall back to per-chunk ``_embed``
    for that slice — a transient batch timeout degrades to slower, not zero."""
    calls = {"batch": 0, "single": 0}

    def single(text):
        calls["single"] += 1
        return _bow_embed(text)

    def batch(texts):
        calls["batch"] += 1
        raise RuntimeError("batch request timed out")

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,
        embed_batch_size=2,
        breaker_threshold=999,  # keep the breaker closed so the fallback actually runs
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    ids = store.add_document(_multi_chunk_doc(2), domain="general", heading="Doc")
    assert len(ids) >= 1
    assert calls["batch"] >= 1  # the batched path was attempted
    assert calls["single"] == len(ids)  # then every chunk retried on its own
    assert store.count_vectors(ids) == len(ids)  # per-chunk fallback recovered every vector


def test_one_slice_fails_others_still_embed(tmp_path):
    """(r2) One slice timing out is isolated: the failing slice's chunks are left
    FTS5-only, but the other slices still store their vectors — partial success,
    not all-or-nothing."""
    calls = {"batch": 0}

    def batch(texts):
        calls["batch"] += 1
        if calls["batch"] == 1:  # only the first slice times out
            raise RuntimeError("slice 1 timed out")
        return [_bow_embed(t) for t in texts]

    def single(text):  # the failed slice's per-chunk fallback also fails
        raise RuntimeError("single embed down")

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,
        embed_batch_size=2,
        breaker_threshold=999,  # slice-1 failures must not open the breaker before slice 2 runs
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    ids = store.add_document(_multi_chunk_doc(4), domain="general", heading="Doc")
    assert len(ids) >= 3  # spans more than one slice
    embedded = store.count_vectors(ids)
    assert 0 < embedded < len(ids)  # partial, not all-or-nothing
    assert embedded == len(ids) - 2  # exactly the first slice's two chunks missing
    assert store.search("weather")  # rows remain FTS5-searchable regardless


def test_breaker_open_skips_embedding_in_sliced_path(tmp_path):
    """(r5) An open breaker skips embedding entirely — no batched or per-chunk
    embed calls leak through, rows land FTS5-only."""
    single, batch, calls = _counting_embedders()
    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,
        embed_batch_size=2,
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    store._breaker_open_until = time.monotonic() + 999  # force the breaker open before ingest
    ids = store.add_document(_multi_chunk_doc(4), domain="general", heading="Doc")
    assert len(ids) >= 3  # rows still written
    assert calls["batch"] == 0 and calls["single"] == 0  # breaker open → no embedding at all
    assert store.count_vectors(ids) == 0  # nothing embedded while open
    assert store.search("weather")  # still keyword-searchable


def test_count_vectors_counts_stored_only(tmp_path):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    cid = store.add_chunk("calculator math", domain="general")
    assert store.count_vectors([cid]) == 1
    assert store.count_vectors([cid, 999999]) == 1  # a missing id isn't counted
    assert store.count_vectors([]) == 0


def test_count_vectors_zero_without_embeddings(tmp_path):
    """No embeddings configured → no vector table; count_vectors returns 0, never raises."""
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=None)
    cid = store.add_chunk("just fts", domain="general")
    assert store.count_vectors([cid]) == 0


async def test_ingest_result_reports_embedded_count(tmp_path):
    """(6d/r3) The ingest op carries the embedded count — every chunk vectored."""
    from ops import OpContext
    from ops.knowledge import IngestSource, ingest

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=_bow_embed,
        embed_batch_fn=lambda ts: [_bow_embed(t) for t in ts],
        embed_batch_size=2,
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    res = await ingest(
        IngestSource.from_text(_multi_chunk_doc(4), title="Doc"),
        ctx=OpContext(knowledge_store=store, graph_config=None),
    )
    assert res.chunks >= 3
    assert res.embedded == res.chunks  # every chunk embedded across the slices


async def test_ingest_result_reports_partial_embedded(tmp_path):
    """(r3) The exact silent-loss scenario: a document is stored but its embeds all
    fail — IngestResult reports ``embedded == 0`` rather than plain success."""
    from ops import OpContext
    from ops.knowledge import IngestSource, ingest

    def batch(texts):
        raise RuntimeError("every batch times out")

    def single(text):
        raise RuntimeError("single embed down")

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=single,
        embed_batch_fn=batch,
        embed_batch_size=2,
        breaker_threshold=999,
        chunk_max_chars=120,
        chunk_overlap_chars=0,
        chunk_min_chars=0,
    )
    res = await ingest(
        IngestSource.from_text(_multi_chunk_doc(4), title="Doc"),
        ctx=OpContext(knowledge_store=store, graph_config=None),
    )
    assert res.chunks >= 3
    assert res.embedded == 0  # stored FTS5-only, and the count makes that visible
