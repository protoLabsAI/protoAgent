"""Retrieval-quality eval for the knowledge store (the missing measurement layer).

protoAgent's main eval suite (ADR 0012, ``evals/runner.py``) drives a live agent
over A2A and checks side effects — great for end-to-end behaviour, but it never
measures **retrieval quality** in isolation: there was no recall@k / MRR / nDCG, no
labelled query→chunk gold set, no regression guard. So an embedding/RRF/chunking
change could silently regress and nothing would catch it.

This module fills that gap. It seeds a ``HybridKnowledgeStore`` from a labelled
gold set (``evals/retrieval_gold.yaml``), runs each query, and scores the ranked
ids against the relevant ones. It can:

  * report recall@k / hit-rate@k / MRR / nDCG@k, overall and split by query mode
    (keyword vs paraphrase),
  * **compare hybrid vs keyword-only** on the same corpus (the RAG bake-off's
    headline: hybrid should beat lexical-alone, especially on paraphrase queries),
  * **sweep** the retrieval knobs (vector_k, rrf_k) to find a better operating point.

Run it live (real gateway embedder, reads your config + secrets)::

    python -m evals.retrieval                 # compare hybrid vs keyword @k=10
    python -m evals.retrieval --sweep         # + a vector_k × rrf_k grid
    python -m evals.retrieval --k 5 --json out.json

Or deterministically with the built-in bag-of-words embedder (no gateway, used by
the unit test)::

    python -m evals.retrieval --embedder bow

The metrics functions are pure and independently unit-tested.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from pathlib import Path
from typing import Callable, Iterable

import yaml

GOLD_PATH = Path(__file__).resolve().parent / "retrieval_gold.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

EmbedFn = Callable[[str], list[float]]


# ── metrics (pure) ───────────────────────────────────────────────────────────


def recall_at_k(retrieved: list, relevant: Iterable, k: int) -> float:
    """Fraction of the relevant ids found in the top-k."""
    rel = set(relevant)
    if not rel:
        return 0.0
    return len(set(retrieved[:k]) & rel) / len(rel)


def hit_rate_at_k(retrieved: list, relevant: Iterable, k: int) -> float:
    """1.0 if ANY relevant id is in the top-k (a.k.a. success@k), else 0.0."""
    return 1.0 if set(retrieved[:k]) & set(relevant) else 0.0


def mrr(retrieved: list, relevant: Iterable) -> float:
    """Reciprocal rank of the first relevant hit (0 if none)."""
    rel = set(relevant)
    for i, cid in enumerate(retrieved):
        if cid in rel:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(retrieved: list, relevant: Iterable, k: int) -> float:
    """Binary-relevance nDCG@k."""
    rel = set(relevant)
    dcg = sum(1.0 / math.log2(i + 2) for i, cid in enumerate(retrieved[:k]) if cid in rel)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / ideal if ideal else 0.0


# ── gold set ─────────────────────────────────────────────────────────────────


def load_gold(path: str | Path = GOLD_PATH) -> tuple[list[dict], list[dict]]:
    """Return ``(corpus, queries)`` from the gold YAML."""
    doc = yaml.safe_load(Path(path).read_text())
    return doc.get("corpus", []), doc.get("queries", [])


# ── harness ──────────────────────────────────────────────────────────────────


def _bow_embed_factory(corpus: list[dict], queries: list[dict]) -> EmbedFn:
    """A deterministic bag-of-words embedder over the gold set's own vocabulary —
    no gateway needed. Good enough to exercise the vector path + metric math in a
    unit test; NOT a stand-in for real embedding quality."""
    import re

    def toks(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", s.lower())

    vocab: list[str] = []
    seen: set[str] = set()
    for item in list(corpus) + list(queries):
        for w in toks(item.get("content") or item.get("q") or ""):
            if w not in seen and len(w) > 2:
                seen.add(w)
                vocab.append(w)
    index = {w: i for i, w in enumerate(vocab)}

    def embed(text: str) -> list[float]:
        v = [0.0] * len(vocab)
        for w in toks(text):
            j = index.get(w)
            if j is not None:
                v[j] = 1.0
        return v

    return embed


def _seed(corpus: list[dict], embed_fn: EmbedFn | None, *, db_path: str, **store_kwargs):
    """Build a HybridKnowledgeStore, add the corpus, return ``(store, key->id)``."""
    from knowledge.hybrid_store import HybridKnowledgeStore

    store = HybridKnowledgeStore(db_path, embed_fn=embed_fn, **store_kwargs)
    key_to_id: dict[str, int] = {}
    for item in corpus:
        cid = store.add_chunk(item["content"], domain=item.get("domain", "general"))
        key_to_id[item["key"]] = cid
    return store, key_to_id


def evaluate(
    embed_fn: EmbedFn | None,
    corpus: list[dict],
    queries: list[dict],
    *,
    k: int = 10,
    **store_kwargs,
) -> dict:
    """Seed a store, run every query, and aggregate metrics overall + by mode."""
    with tempfile.TemporaryDirectory() as tmp:
        store, key_to_id = _seed(corpus, embed_fn, db_path=str(Path(tmp) / "kb.db"), **store_kwargs)
        per_query: list[dict] = []
        for item in queries:
            relevant = [key_to_id[key] for key in item["relevant"] if key in key_to_id]
            rows = store.search(item["q"], k=max(k, 1))
            retrieved = [r.get("id") for r in rows]
            per_query.append(
                {
                    "q": item["q"],
                    "mode": item.get("mode", "?"),
                    "recall": recall_at_k(retrieved, relevant, k),
                    "hit": hit_rate_at_k(retrieved, relevant, k),
                    "mrr": mrr(retrieved, relevant),
                    "ndcg": ndcg_at_k(retrieved, relevant, k),
                }
            )

    def _agg(rows: list[dict]) -> dict:
        n = len(rows) or 1
        return {
            "n": len(rows),
            "recall@k": round(sum(r["recall"] for r in rows) / n, 4),
            "hit@k": round(sum(r["hit"] for r in rows) / n, 4),
            "mrr": round(sum(r["mrr"] for r in rows) / n, 4),
            "ndcg@k": round(sum(r["ndcg"] for r in rows) / n, 4),
        }

    modes = sorted({r["mode"] for r in per_query})
    return {
        "k": k,
        "overall": _agg(per_query),
        "by_mode": {m: _agg([r for r in per_query if r["mode"] == m]) for m in modes},
        "per_query": per_query,
    }


def compare_hybrid_vs_keyword(embed_fn: EmbedFn, corpus, queries, *, k: int = 10, **store_kwargs) -> dict:
    """The RAG bake-off's headline question, on our stack: how much does the vector
    half lift recall over keyword-only (same store, ``embed_fn=None`` ⇒ pure FTS5)?"""
    hybrid = evaluate(embed_fn, corpus, queries, k=k, **store_kwargs)
    keyword = evaluate(None, corpus, queries, k=k)
    lift = round(hybrid["overall"]["recall@k"] - keyword["overall"]["recall@k"], 4)
    return {"hybrid": hybrid, "keyword": keyword, "recall_lift": lift}


def sweep(
    embed_fn: EmbedFn, corpus, queries, *, k: int = 10, vector_ks=(10, 20, 40), rrf_ks=(20, 60, 120)
) -> list[dict]:
    """Grid over the two retrieval knobs surfaced in #985, ranked by recall@k."""
    out = []
    for vk in vector_ks:
        for rk in rrf_ks:
            rep = evaluate(embed_fn, corpus, queries, k=k, vector_k=vk, rrf_k=rk)
            out.append({"vector_k": vk, "rrf_k": rk, **rep["overall"]})
    out.sort(key=lambda r: (r["recall@k"], r["mrr"]), reverse=True)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _gateway_embed_fn() -> EmbedFn:
    """The real embedder, built from the live config + secrets (same path as boot)."""
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from graph.llm import create_embed_fn

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    fn = create_embed_fn(cfg)
    if fn is None:
        raise SystemExit("no embedder: set knowledge.embed_model + a gateway api_key, or run with --embedder bow")
    return fn


def _fmt(agg: dict) -> str:
    return f"recall@k={agg['recall@k']:.3f}  hit@k={agg['hit@k']:.3f}  mrr={agg['mrr']:.3f}  ndcg@k={agg['ndcg@k']:.3f}  (n={agg['n']})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Retrieval-quality eval for the knowledge store.")
    p.add_argument("--k", type=int, default=10, help="top-k cut for the metrics (default 10).")
    p.add_argument("--gold", default=str(GOLD_PATH), help="gold YAML path.")
    p.add_argument(
        "--embedder",
        choices=["gateway", "bow"],
        default="gateway",
        help="gateway = real qwen3-embedding (default); bow = deterministic, offline.",
    )
    p.add_argument("--sweep", action="store_true", help="also sweep vector_k × rrf_k.")
    p.add_argument("--json", dest="json_out", default="", help="write the full report to this path.")
    args = p.parse_args(argv)

    corpus, queries = load_gold(args.gold)
    embed_fn = _bow_embed_factory(corpus, queries) if args.embedder == "bow" else _gateway_embed_fn()

    cmp = compare_hybrid_vs_keyword(embed_fn, corpus, queries, k=args.k)
    print(f"\nRetrieval eval — {len(corpus)} chunks, {len(queries)} queries, k={args.k}, embedder={args.embedder}\n")
    print(f"  hybrid   {_fmt(cmp['hybrid']['overall'])}")
    print(f"  keyword  {_fmt(cmp['keyword']['overall'])}")
    print(f"  recall lift (hybrid − keyword): {cmp['recall_lift']:+.3f}\n")
    print("  hybrid by mode:")
    for mode, agg in cmp["hybrid"]["by_mode"].items():
        print(f"    {mode:12s} {_fmt(agg)}")

    report = {"compare": cmp}
    if args.sweep:
        grid = sweep(embed_fn, corpus, queries, k=args.k)
        report["sweep"] = grid
        print("\n  knob sweep (top 5 by recall@k):")
        for row in grid[:5]:
            print(
                f"    vector_k={row['vector_k']:<3} rrf_k={row['rrf_k']:<4} "
                f"recall@k={row['recall@k']:.3f} mrr={row['mrr']:.3f} ndcg@k={row['ndcg@k']:.3f}"
            )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\n  wrote {args.json_out}")
    print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
