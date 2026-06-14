"""Tests for knowledge ingest chunking (ADR 0021).

Pure ``chunk_text`` behavior + an ``add_document`` integration over a real
SQLite store (chunks are individually searchable).
"""

from __future__ import annotations

from knowledge.chunking import chunk_text
from knowledge.store import KnowledgeStore


# ── chunk_text: pure behavior ────────────────────────────────────────────────


def test_empty_and_whitespace_return_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_unchanged_chunk():
    text = "A single short fact about the system."
    assert chunk_text(text, max_chars=1200) == [text]


def test_text_at_limit_not_split():
    text = "x" * 100
    assert chunk_text(text, max_chars=100) == [text]


def test_long_text_splits_into_multiple_chunks():
    paras = [f"Paragraph {i} " + "word " * 30 for i in range(10)]
    text = "\n\n".join(paras)
    chunks = chunk_text(text, max_chars=300, overlap_chars=0, min_chars=0)
    assert len(chunks) > 1
    # No chunk meaningfully exceeds the ceiling (a folded tail may add up to
    # min_chars, which is 0 here).
    assert all(len(c) <= 300 for c in chunks)


def test_no_content_lost():
    paras = [f"Distinct{i} alpha beta gamma delta epsilon" for i in range(12)]
    text = "\n\n".join(paras)
    chunks = chunk_text(text, max_chars=120, overlap_chars=0, min_chars=0)
    joined = " ".join(chunks)
    for i in range(12):
        assert f"Distinct{i}" in joined


def test_overlap_shares_a_boundary_tail():
    paras = [f"Para{i} " + "lorem ipsum dolor sit amet " * 4 for i in range(8)]
    text = "\n\n".join(paras)
    no_ov = chunk_text(text, max_chars=300, overlap_chars=0, min_chars=0)
    ov = chunk_text(text, max_chars=300, overlap_chars=80, min_chars=0)
    # Overlap re-includes prior context, so the same content needs >= as many
    # chunks and the total character count is larger than the no-overlap split.
    assert sum(len(c) for c in ov) > sum(len(c) for c in no_ov)
    # Some trailing words of an earlier chunk reappear in the next one.
    shared = False
    for a, b in zip(ov, ov[1:]):
        tail_words = a.split()[-3:]
        if tail_words and " ".join(tail_words) in b:
            shared = True
            break
    assert shared


def test_oversized_single_token_is_hard_windowed():
    blob = "A" * 1000  # no whitespace anywhere
    chunks = chunk_text(blob, max_chars=200, overlap_chars=0, min_chars=0)
    assert len(chunks) >= 5
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks) == blob


def test_tiny_trailing_fragment_folds_back():
    body = "\n\n".join("sentence number %d here we go again" % i for i in range(20))
    text = body + "\n\ntiny"
    folded = chunk_text(text, max_chars=200, overlap_chars=0, min_chars=50)
    unfolded = chunk_text(text, max_chars=200, overlap_chars=0, min_chars=0)
    # The "tiny" tail is below the 50-char floor, so folding yields fewer chunks
    # and no chunk is just the fragment.
    assert len(folded) <= len(unfolded)
    assert "tiny" not in {c.strip() for c in folded}
    assert any("tiny" in c for c in folded)


def test_deterministic():
    text = "\n\n".join(f"Block {i} " + "alpha beta " * 20 for i in range(15))
    assert chunk_text(text, max_chars=250) == chunk_text(text, max_chars=250)


def test_prefers_paragraph_boundaries():
    a = "First paragraph. " * 10
    b = "Second paragraph. " * 10
    chunks = chunk_text((a + "\n\n" + b).strip(), max_chars=len(a) + 20,
                        overlap_chars=0, min_chars=0)
    # Each paragraph fits its own chunk → the split lands on the blank line.
    assert len(chunks) == 2
    assert chunks[0].startswith("First")
    assert chunks[1].startswith("Second")


# ── add_document: integration over a real store ──────────────────────────────


def _long_doc() -> str:
    return "\n\n".join(
        f"Topic {i}: the quick brown fox jumps over the lazy dog number {i}. "
        "It was the best of times, it was the worst of times, repeated for bulk."
        for i in range(20)
    )


def test_add_document_splits_and_is_searchable(tmp_path):
    store = KnowledgeStore(
        db_path=str(tmp_path / "agent.db"),
        chunk_max_chars=300,
        chunk_overlap_chars=40,
        chunk_min_chars=50,
    )
    ids = store.add_document(_long_doc(), domain="conversation", heading="Doc")
    assert len(ids) > 1                       # genuinely chunked
    assert store.stats().get("total") == len(ids)
    # A query for a passage that lived deep in the doc finds its own chunk.
    hits = store.search("Topic 17 quick brown fox", k=3)
    assert hits
    assert any("Topic 17" in h["content"] for h in hits)


def test_add_document_short_body_is_single_chunk(tmp_path):
    store = KnowledgeStore(db_path=str(tmp_path / "agent.db"), chunk_max_chars=1200)
    ids = store.add_document("a short note", domain="general")
    assert len(ids) == 1


def test_hybrid_add_document_embeds_each_chunk(tmp_path):
    """The actual win: add_document on a hybrid store creates one vector per
    chunk, so each passage is independently retrievable by similarity."""
    import sqlite3

    from knowledge.hybrid_store import HybridKnowledgeStore

    vocab = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

    def bow(text: str) -> list[float]:
        t = text.lower()
        return [1.0 if w in t else 0.0 for w in vocab]

    db = str(tmp_path / "kb.db")
    store = HybridKnowledgeStore(db, embed_fn=bow, chunk_max_chars=120,
                                 chunk_overlap_chars=0, chunk_min_chars=0)
    doc = "\n\n".join([
        "alpha section. " + "padding word " * 12,
        "bravo section. " + "padding word " * 12,
        "charlie section. " + "padding word " * 12,
    ])
    ids = store.add_document(doc, domain="conversation", heading="Doc")
    assert len(ids) >= 3
    conn = sqlite3.connect(db)
    n_vecs = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    conn.close()
    assert n_vecs == len(ids)            # one embedding per chunk, not one for the doc


def _bow_factory():
    vocab = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

    def bow(text: str) -> list[float]:
        t = text.lower()
        return [1.0 if w in t else 0.0 for w in vocab]

    return bow


def _multi_chunk_doc() -> str:
    return "\n\n".join([
        "alpha section. " + "padding word " * 12,
        "bravo section. " + "padding word " * 12,
        "charlie section. " + "padding word " * 12,
    ])


def test_add_document_batches_embeddings_into_one_call(tmp_path):
    """The batched path: N chunks → ONE embed_batch call, zero per-chunk embeds,
    and every chunk still gets its vector."""
    import sqlite3

    from knowledge.hybrid_store import HybridKnowledgeStore

    bow = _bow_factory()
    calls = {"batch": 0, "single": 0}

    def single(text):
        calls["single"] += 1
        return bow(text)

    def batch(texts):
        calls["batch"] += 1
        return [bow(t) for t in texts]

    db = str(tmp_path / "kb.db")
    store = HybridKnowledgeStore(db, embed_fn=single, embed_batch_fn=batch,
                                 chunk_max_chars=120, chunk_overlap_chars=0, chunk_min_chars=0)
    ids = store.add_document(_multi_chunk_doc(), domain="conversation", heading="Doc")
    assert len(ids) >= 3
    assert calls["batch"] == 1 and calls["single"] == 0   # one batched call, no per-chunk
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == len(ids)
    conn.close()
    # And the batched vectors are actually usable for retrieval.
    assert store.search("bravo", k=3)


def test_add_document_single_chunk_skips_batch(tmp_path):
    from knowledge.hybrid_store import HybridKnowledgeStore

    bow = _bow_factory()
    calls = {"batch": 0, "single": 0}

    def single(text):
        calls["single"] += 1
        return bow(text)

    def batch(texts):
        calls["batch"] += 1
        return [bow(t) for t in texts]

    store = HybridKnowledgeStore(str(tmp_path / "kb.db"), embed_fn=single,
                                 embed_batch_fn=batch, chunk_max_chars=5000)
    ids = store.add_document("a short single-chunk note", domain="general")
    assert len(ids) == 1
    assert calls["batch"] == 0 and calls["single"] == 1   # single chunk → per-chunk embed


def test_add_document_batch_failure_keeps_fts_rows(tmp_path):
    """A batched-embed failure still stores the chunks (FTS5-searchable) and trips
    the breaker — never loses the document."""
    import sqlite3

    from knowledge.hybrid_store import HybridKnowledgeStore

    bow = _bow_factory()
    db = str(tmp_path / "kb.db")
    store = HybridKnowledgeStore(
        db, embed_fn=bow,
        embed_batch_fn=lambda ts: (_ for _ in ()).throw(RuntimeError("gateway down")),
        breaker_threshold=1, chunk_max_chars=120, chunk_overlap_chars=0, chunk_min_chars=0,
    )
    ids = store.add_document(_multi_chunk_doc(), domain="conversation", heading="Doc")
    assert len(ids) >= 3                       # rows written despite the embed failure
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0  # no vectors
    conn.close()
    assert store._breaker_open()               # breaker tripped
    assert store.search("bravo")               # still searchable via FTS5


# ── contextual enrichment ────────────────────────────────────────────────────


def test_enrichment_prepends_context_to_each_chunk(tmp_path):
    calls = []

    def ctx(doc, chunk):
        calls.append((doc, chunk))
        return f"CTX[{chunk.split()[0]}]"

    store = KnowledgeStore(
        db_path=str(tmp_path / "agent.db"),
        chunk_max_chars=120, chunk_overlap_chars=0, chunk_min_chars=0,
        context_fn=ctx,
    )
    doc = "\n\n".join(["alpha " + "w " * 40, "bravo " + "w " * 40, "charlie " + "w " * 40])
    ids = store.add_document(doc, domain="conversation", heading="Doc")
    assert len(ids) >= 3
    assert len(calls) == len(ids)                 # one enrichment call per chunk
    # Every call saw the FULL document, and every stored chunk carries its context.
    assert all(c[0] == doc for c in calls)
    hits = store.search("CTX", k=10)
    assert hits and all(h["content"].startswith("CTX[") for h in hits)


def test_single_chunk_doc_is_not_enriched(tmp_path):
    calls = []
    store = KnowledgeStore(
        db_path=str(tmp_path / "agent.db"),
        chunk_max_chars=5000,
        context_fn=lambda d, c: calls.append(1) or "CTX",
    )
    ids = store.add_document("a short single-chunk note", domain="general")
    assert len(ids) == 1
    assert calls == []                            # the chunk IS the whole doc — no call


def test_enrich_false_disables_enrichment(tmp_path):
    calls = []
    store = KnowledgeStore(
        db_path=str(tmp_path / "agent.db"),
        chunk_max_chars=120, chunk_overlap_chars=0, chunk_min_chars=0,
        context_fn=lambda d, c: calls.append(1) or "CTX",
    )
    doc = "\n\n".join(["alpha " + "w " * 40, "bravo " + "w " * 40])
    ids = store.add_document(doc, enrich=False)
    assert len(ids) >= 2
    assert calls == []


def test_enrichment_failure_degrades_to_raw_chunks(tmp_path):
    n = {"calls": 0}

    def boom(doc, chunk):
        n["calls"] += 1
        raise RuntimeError("gateway down")

    store = KnowledgeStore(
        db_path=str(tmp_path / "agent.db"),
        chunk_max_chars=120, chunk_overlap_chars=0, chunk_min_chars=0,
        context_fn=boom,
    )
    doc = "\n\n".join(["alpha " + "w " * 40, "bravo " + "w " * 40, "charlie " + "w " * 40])
    ids = store.add_document(doc)                 # never raises
    assert len(ids) >= 3
    # First failure disables enrichment for the rest of the doc — not N failing calls.
    assert n["calls"] == 1
    assert not any(h["content"].startswith("CTX") for h in store.search("alpha", k=5))


def test_add_document_helper_falls_back_on_chunkless_backend():
    from knowledge import add_document

    class OnlyAddChunk:
        def __init__(self):
            self.calls = []

        def add_chunk(self, content, domain="general", **kw):
            self.calls.append((content, domain, kw))
            return len(self.calls)

    backend = OnlyAddChunk()
    ids = add_document(backend, "x" * 5000, domain="conversation",
                       heading="H", max_chars=200)
    # No add_document on the backend → one un-chunked add_chunk, chunk-only
    # kwargs stripped (add_chunk never sees max_chars).
    assert ids == [1]
    assert len(backend.calls) == 1
    assert "max_chars" not in backend.calls[0][2]
