"""Tests for the retrieval-quality eval harness (evals/retrieval.py).

Deterministic — no gateway. Covers the pure metric math, an end-to-end run over the
shipped gold set with the built-in bag-of-words embedder, and a constructed case
proving the harness actually captures the hybrid (vector) lift over keyword-only.
"""

from __future__ import annotations

import math

from evals import retrieval as R


# ── pure metrics ─────────────────────────────────────────────────────────────


def test_recall_at_k():
    assert R.recall_at_k([1, 2, 3], [2], 10) == 1.0
    assert R.recall_at_k([1, 2, 3], [9], 10) == 0.0
    assert R.recall_at_k([1, 2, 3, 4], [2, 4], 10) == 1.0
    assert R.recall_at_k([1, 2, 3, 4], [2, 4], 2) == 0.5  # only id 2 is in the top-2
    assert R.recall_at_k([1, 2, 3], [], 10) == 0.0        # no relevant → 0, not a crash


def test_hit_rate_at_k():
    assert R.hit_rate_at_k([1, 2, 3], [3], 3) == 1.0
    assert R.hit_rate_at_k([1, 2, 3], [3], 2) == 0.0      # id 3 falls outside top-2
    assert R.hit_rate_at_k([1, 2, 3], [9], 3) == 0.0


def test_mrr():
    assert R.mrr([5, 2, 9], [2]) == 0.5                   # first relevant at rank 2
    assert R.mrr([2, 5, 9], [2]) == 1.0
    assert R.mrr([1, 2, 3], [9]) == 0.0


def test_ndcg_at_k():
    assert R.ndcg_at_k([7, 1, 2], [7], 10) == 1.0         # relevant first → ideal
    # single relevant at rank 3: dcg = 1/log2(4), ideal = 1/log2(2) = 1
    assert math.isclose(R.ndcg_at_k([1, 2, 7], [7], 10), 1.0 / math.log2(4), rel_tol=1e-9)
    assert R.ndcg_at_k([1, 2, 3], [9], 10) == 0.0


# ── end-to-end over the shipped gold set (bow embedder) ──────────────────────


def test_evaluate_gold_set_with_bow_embedder():
    corpus, queries = R.load_gold()
    assert corpus and queries
    embed = R._bow_embed_factory(corpus, queries)
    rep = R.evaluate(embed, corpus, queries, k=10)

    # well-formed aggregate
    assert rep["k"] == 10
    assert rep["overall"]["n"] == len(queries)
    for key in ("recall@k", "hit@k", "mrr", "ndcg@k"):
        assert 0.0 <= rep["overall"][key] <= 1.0
    assert "per_query" in rep and len(rep["per_query"]) == len(queries)

    # keyword-mode queries share tokens with their target, so even lexical/bow
    # retrieval must find them — a strong, deterministic floor.
    kw = rep["by_mode"].get("keyword")
    assert kw and kw["recall@k"] == 1.0


# ── the harness captures the vector lift ─────────────────────────────────────


def test_hybrid_beats_keyword_on_a_non_lexical_match():
    """A query with NO lexical overlap with its relevant chunk: keyword-only (FTS5)
    can't find it (recall 0), but an embedder that maps them together makes the
    hybrid surface it (recall 1). Proves the harness measures the vector lift."""
    corpus = [
        {"key": "target", "domain": "general", "content": "alpha beta gamma"},
        {"key": "other", "domain": "general", "content": "delta epsilon zeta"},
    ]
    queries = [{"q": "zzzzz", "relevant": ["target"], "mode": "paraphrase"}]

    def embed(text: str) -> list[float]:
        # query "zzzzz" and the target chunk both map to the same vector.
        return [1.0, 0.0] if ("alpha" in text or "zzzzz" in text) else [0.0, 1.0]

    cmp = R.compare_hybrid_vs_keyword(embed, corpus, queries, k=5)
    assert cmp["keyword"]["overall"]["recall@k"] == 0.0
    assert cmp["hybrid"]["overall"]["recall@k"] == 1.0
    assert cmp["recall_lift"] == 1.0
