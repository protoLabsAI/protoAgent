"""Knowledge store — sqlite-backed chunk storage for memory tools and middleware.

The template ships this enabled by default so a fresh fork has a working
memory loop on day one (memory_ingest, memory_recall, daily_log) and the
eval harness can assert side effects against real DB state.

See ``knowledge.store.KnowledgeStore`` for the public API.
"""

from knowledge.store import KnowledgeStore, Chunk

__all__ = ["KnowledgeStore", "Chunk"]
