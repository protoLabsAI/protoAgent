"""The knowledge backend contract (ADR 0031).

Every consumer — the memory tools, ``KnowledgeMiddleware``, the knowledge routes,
the eval harness — uses ``STATE.knowledge_store`` **duck-typed**. This Protocol
formalizes that surface so a plugin can contribute an alternative backend
(pgvector, Qdrant, Chroma, a managed vector DB) via
``registry.register_knowledge_store(name, factory)`` and a fork can select it with
``knowledge.backend: "<name>"``.

The built-in ``knowledge.store.KnowledgeStore`` (SQLite + FTS5, and its
``HybridKnowledgeStore`` semantic subclass) is the default reference implementation
and satisfies this Protocol. It's a documentation + optional ``isinstance`` aid
(``runtime_checkable``); duck-typing still works, and a backend may implement more.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeBackend(Protocol):
    """The methods the agent calls on the knowledge store. Implement these for a
    custom backend; ``factory(config) -> KnowledgeBackend`` is what a plugin
    registers."""

    def add_chunk(self, content: str, domain: str = "general", **kwargs) -> int | None:
        """Store a chunk; return its id (or None if not stored)."""

    def search(self, query: str, k: int = 5, *, domain: str | None = None) -> list[dict]:
        """Return up to ``k`` matching chunks (highest-ranked first), each a dict."""

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        """Return the always-on context block injected by KnowledgeMiddleware."""

    def list_chunks(self, *args, **kwargs) -> list[dict]:
        """List stored chunks (for the knowledge console + tools)."""

    def stats(self) -> dict:
        """Return counts/health for the knowledge console + status."""

    def delete_by_id(self, chunk_id: int) -> bool:
        """Delete one chunk by id; return whether it existed."""

    def add_finding(self, *args, **kwargs):
        """Record a research finding (used by the curator / harvest paths)."""
