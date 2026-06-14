"""Knowledge store — sqlite-backed chunk storage for memory tools and middleware.

The template ships this enabled by default so a fresh fork has a working
memory loop on day one (memory_ingest, memory_recall, daily_log) and the
eval harness can assert side effects against real DB state.

See ``knowledge.store.KnowledgeStore`` for the public API.
"""

from knowledge.backend import KnowledgeBackend
from knowledge.store import KnowledgeStore, Chunk

# Chunk-only knobs that add_chunk doesn't accept — stripped on the fallback path.
_CHUNK_ONLY_KW = ("max_chars", "overlap_chars", "min_chars")


def add_document(store, content: str, **kwargs) -> list[int]:
    """Chunk-and-store a document, degrading safely across backends (ADR 0021/0031).

    Uses the store's ``add_document`` when present (the built-in
    ``KnowledgeStore`` / ``HybridKnowledgeStore`` split a large body into
    per-passage embeddings). A plugin backend that only implements the ADR 0031
    surface (``add_chunk``) gets a single un-chunked write instead — correct, if
    coarser. Returns the created chunk ids."""
    fn = getattr(store, "add_document", None)
    if callable(fn):
        return fn(content, **kwargs)
    cid = store.add_chunk(content, **{k: v for k, v in kwargs.items() if k not in _CHUNK_ONLY_KW})
    return [cid] if cid is not None else []


__all__ = ["KnowledgeStore", "Chunk", "KnowledgeBackend", "add_document"]
