"""Embedding-outage hardening (#1681).

A hung gateway embedding route froze every chat turn: the embeddings client
carried the OpenAI SDK defaults (600s timeout, 2 retries), recall runs BEFORE
every model call, and the knowledge breaker can only trip when a call returns.
These pin the three defenses: a short dedicated client timeout with no
app-side retries, the constructor-time route probe that opens the breaker
before the first turn, and keyword-only recall as the shipped default.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from knowledge.hybrid_store import HybridKnowledgeStore


def test_embeddings_client_short_timeout_no_retries():
    from graph.llm import _EMBED_TIMEOUT_S, _build_embeddings

    emb = _build_embeddings(LangGraphConfig(embed_model="qwen3-embedding", api_key="k"))
    assert emb is not None
    assert emb.request_timeout == _EMBED_TIMEOUT_S
    assert emb.max_retries == 0


def test_probe_failure_opens_the_breaker_before_any_turn(tmp_path):
    calls: list[str] = []

    def _broken(text: str) -> list[float]:
        calls.append(text)
        raise RuntimeError("HTTP 524")

    store = HybridKnowledgeStore(db_path=str(tmp_path / "k.db"), embed_fn=_broken, breaker_threshold=2)
    assert store._probe_once() is False
    assert store._breaker_open() is True  # immediately — not after N in-turn failures
    # A recall-time embed now short-circuits without touching the dead route.
    assert store._embed("query") is None
    assert calls == ["ping"]


def test_probe_success_keeps_semantic_recall_live(tmp_path):
    store = HybridKnowledgeStore(db_path=str(tmp_path / "k.db"), embed_fn=lambda t: [0.1, 0.2])
    assert store._probe_once() is True
    assert store._breaker_open() is False
    assert store._embed("query") == [0.1, 0.2]


def test_embeddings_ship_off_by_default():
    assert LangGraphConfig().knowledge_embeddings is False
