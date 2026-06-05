"""ADR 0021 Phase 1.5: the dormant embeddings layer is now wired.

`knowledge.embeddings` flips the store from keyword-only FTS5 to the
HybridKnowledgeStore (FTS5 + vector via the gateway, RRF-fused). Default off;
any failure degrades to FTS5, never KB-less.
"""

from __future__ import annotations

import server
from graph.config import LangGraphConfig
from graph.llm import create_embed_fn


def _cfg(tmp_path, *, embeddings: bool, model: str = "nomic-embed-text") -> LangGraphConfig:
    cfg = LangGraphConfig()
    cfg.knowledge_db_path = str(tmp_path / "kb.db")
    cfg.knowledge_embeddings = embeddings
    cfg.embed_model = model
    cfg.api_base = "http://gateway.test/v1"
    cfg.api_key = "test-key"
    return cfg


def test_create_embed_fn_none_without_model():
    cfg = LangGraphConfig()
    cfg.embed_model = ""
    assert create_embed_fn(cfg) is None


def test_create_embed_fn_callable_with_model():
    # Constructs the OpenAIEmbeddings client; no network call until invoked.
    cfg = LangGraphConfig()
    cfg.api_base, cfg.api_key = "http://gateway.test/v1", "k"
    fn = create_embed_fn(cfg)
    assert callable(fn)


def test_create_embed_fn_sends_raw_strings(monkeypatch):
    """Regression: OpenAIEmbeddings defaults to client-side tiktoken tokenization
    and posts `input` as int arrays, which LiteLLM/vLLM gateways 422. We must
    pass check_embedding_ctx_length=False so it sends the raw string."""
    import graph.llm as llm

    captured = {}

    class _FakeEmb:
        def __init__(self, **kw):
            captured.update(kw)

        def embed_query(self, text):
            return [0.0]

    monkeypatch.setattr(llm, "OpenAIEmbeddings", _FakeEmb)
    cfg = LangGraphConfig()
    cfg.api_base, cfg.api_key = "http://gateway.test/v1", "k"
    create_embed_fn(cfg)
    assert captured.get("check_embedding_ctx_length") is False


def test_store_is_hybrid_by_default(tmp_path):
    # knowledge.embeddings defaults on (ADR 0021); the config helper here mirrors it.
    cfg = LangGraphConfig()
    cfg.knowledge_db_path = str(tmp_path / "kb.db")
    cfg.api_base, cfg.api_key = "http://gateway.test/v1", "k"
    assert cfg.knowledge_embeddings is True
    assert type(server._build_knowledge_store(cfg)).__name__ == "HybridKnowledgeStore"


def test_store_is_keyword_when_embeddings_off(tmp_path):
    store = server._build_knowledge_store(_cfg(tmp_path, embeddings=False))
    assert type(store).__name__ == "KnowledgeStore"


def test_store_is_hybrid_when_embeddings_enabled(tmp_path):
    store = server._build_knowledge_store(_cfg(tmp_path, embeddings=True))
    assert type(store).__name__ == "HybridKnowledgeStore"


def test_hybrid_degrades_to_keyword_when_no_embed_model(tmp_path):
    # Embeddings on but no model → fall back to FTS5, never crash.
    store = server._build_knowledge_store(_cfg(tmp_path, embeddings=True, model=""))
    assert type(store).__name__ == "KnowledgeStore"
