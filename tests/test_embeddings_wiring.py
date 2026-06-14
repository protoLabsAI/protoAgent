"""ADR 0021 Phase 1.5: the dormant embeddings layer is now wired.

`knowledge.embeddings` flips the store from keyword-only FTS5 to the
HybridKnowledgeStore (FTS5 + vector via the gateway, RRF-fused). Default off;
any failure degrades to FTS5, never KB-less.
"""

from __future__ import annotations

import graph.agent  # noqa: F401 — bind graph.agent.create_llm to the REAL fn before
#   any test monkeypatches graph.llm.create_llm. create_context_fn lazily imports from
#   graph.agent; if that first import happened under the patch, graph.agent would
#   capture the fake create_llm permanently and leak into later middleware-wiring tests.
import server
from graph.config import LangGraphConfig
from graph.llm import (
    create_context_fn,
    create_embed_batch_fn,
    create_embed_fn,
    create_transcribe_fn,
)


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


def test_create_embed_batch_fn():
    cfg = LangGraphConfig()
    cfg.api_base, cfg.api_key = "http://gateway.test/v1", "k"
    assert callable(create_embed_batch_fn(cfg))   # OpenAIEmbeddings.embed_documents
    cfg.embed_model = ""
    assert create_embed_batch_fn(cfg) is None


def test_hybrid_store_gets_a_batch_embedder(tmp_path):
    store = server._build_knowledge_store(_cfg(tmp_path, embeddings=True))
    assert type(store).__name__ == "HybridKnowledgeStore"
    assert store._embed_batch_fn is not None       # batched ingest wired in


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


# ── contextual enrichment (ADR 0021) ─────────────────────────────────────────


def test_create_context_fn_builds_doc_aware_prompt(monkeypatch):
    """create_context_fn caps the document, formats the chunk + doc into the
    prompt, and returns the model's reply stripped of any reasoning tags."""
    import graph.llm as llm

    captured = {}

    class _FakeLLM:
        def invoke(self, messages):
            captured["content"] = messages[0].content

            class _R:
                content = "<scratch_pad>noise</scratch_pad>The Q3 finance section."
            return _R()

    monkeypatch.setattr(llm, "create_llm", lambda config, model_name=None: _FakeLLM())
    cfg = LangGraphConfig()
    cfg.api_base, cfg.api_key = "http://gateway.test/v1", "k"
    cfg.knowledge_context_max_doc_chars = 50
    fn = create_context_fn(cfg)
    out = fn("D" * 500, "the chunk text")
    assert out == "The Q3 finance section."          # reasoning stripped
    assert "the chunk text" in captured["content"]
    assert "D" * 50 in captured["content"] and "D" * 51 not in captured["content"]  # doc capped


def test_store_wires_context_fn_when_enrichment_on(tmp_path, monkeypatch):
    import graph.llm as llm

    monkeypatch.setattr(llm, "create_llm", lambda config, model_name=None: object())
    cfg = _cfg(tmp_path, embeddings=False)
    cfg.knowledge_contextual_enrichment = True
    store = server._build_knowledge_store(cfg)
    assert store._context_fn is not None


def test_store_no_context_fn_when_enrichment_off(tmp_path):
    store = server._build_knowledge_store(_cfg(tmp_path, embeddings=False))
    assert store._context_fn is None


# ── transcription (gateway STT for audio/video ingestion) ─────────────────────


def test_create_transcribe_fn_none_without_model():
    cfg = LangGraphConfig()
    cfg.transcribe_model = ""
    assert create_transcribe_fn(cfg) is None


def test_create_transcribe_fn_posts_audio_to_gateway(monkeypatch):
    import httpx

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "  hello from whisper  "}

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, data=None, files=None):
            captured.update(url=url, headers=headers, data=data, files=files)
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    cfg = LangGraphConfig()
    cfg.api_base, cfg.api_key, cfg.transcribe_model = "http://gw.test/v1", "k", "whisper-1"
    fn = create_transcribe_fn(cfg)
    out = fn(b"audio-bytes", "clip.mp3")
    assert out == "hello from whisper"                     # stripped
    assert captured["url"] == "http://gw.test/v1/audio/transcriptions"
    assert captured["data"] == {"model": "whisper-1"}
    assert captured["files"]["file"][0] == "clip.mp3"
    assert captured["headers"]["Authorization"] == "Bearer k"
