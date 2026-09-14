"""Forgetting what a chat saved to memory (#3493): the store's delete-by-source
primitive, and the delete-dialog helper that decides exactly which rows are the chat's."""

from __future__ import annotations

import sqlite3

from graph.conversation_harvest import forget_conversation_memory
from knowledge.store import KnowledgeStore


def _contents(store) -> list[str]:
    return sorted(c.content for c in store.list_chunks(limit=500, include_invalidated=True))


def test_delete_by_source_exact_prefix_and_type_filter(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("summary A", domain="conversation", source="a2a:s1", source_type="harvest")
    store.add_chunk("fact A", domain="fact", source="a2a:s1", source_type="extracted")
    store.add_chunk("ingested A", domain="general", source="a2a:s1", source_type="conversation")
    store.add_chunk("goal fact", domain="fact", source="a2a:s1:goal-iter-2", source_type="extracted")
    store.add_chunk("other", domain="fact", source="a2a:s10", source_type="extracted")
    assert store.delete_by_source("a2a:s1", source_types=("harvest", "extracted")) == 2
    assert _contents(store) == sorted(["ingested A", "goal fact", "other"])
    assert store.delete_by_source("a2a:s1:goal-iter-", source_types=("extracted",), prefix=True) == 1
    assert _contents(store) == sorted(["ingested A", "other"])


def test_delete_by_source_prefix_escapes_like_wildcards(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("mine", domain="fact", source="a2a:s_1:goal-iter-1", source_type="extracted")
    store.add_chunk("not mine", domain="fact", source="a2a:sX1:goal-iter-1", source_type="extracted")
    assert store.delete_by_source("a2a:s_1:goal-iter-", prefix=True) == 1
    assert _contents(store) == ["not mine"]


def test_delete_by_source_includes_superseded_rows(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    old = store.add_chunk("fact v1", domain="fact", source="a2a:s1", source_type="extracted")
    new = store.add_chunk("fact v2", domain="fact", source="a2a:s1", source_type="extracted")
    assert store.invalidate_chunk(old, superseded_by=new)
    assert store.delete_by_source("a2a:s1", source_types=("extracted",)) == 2
    assert _contents(store) == []


def test_delete_by_source_never_widens_to_everything(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("keep", domain="fact", source="a2a:s1", source_type="extracted")
    assert store.delete_by_source("") == 0
    assert store.delete_by_source("   ") == 0
    assert store.delete_by_source("", prefix=True) == 0
    assert store.delete_by_source("a2a:s1", source_types=()) == 0  # an empty type filter matches nothing
    assert _contents(store) == ["keep"]


def test_hybrid_delete_by_source_drops_vectors(tmp_path):
    """The hybrid override also clears the side chunk_vectors table (no FK cascade)."""
    from knowledge.hybrid_store import HybridKnowledgeStore

    db = tmp_path / "kb.db"
    store = HybridKnowledgeStore(db, embed_fn=lambda t: [1.0, 0.0])
    store.add_chunk("gone", domain="fact", source="a2a:s1", source_type="extracted")
    store.add_chunk("kept", domain="fact", source="a2a:s2", source_type="extracted")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 2
        assert store.delete_by_source("a2a:s1", source_types=("extracted",)) == 1
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
    finally:
        conn.close()
    assert _contents(store) == ["kept"]


def test_forget_conversation_memory_removes_exactly_the_chats_rows(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    # The chat's own writes: compaction archives (with and without the new source),
    # harvested summaries and facts — including a legacy `chat:` thread and a goal
    # iteration sub-thread the TTL sweep harvested on its own. All go.
    store.add_chunk(
        "archive", domain="conversation", namespace="chat-archive:s1", source="a2a:s1", source_type="conversation"
    )
    store.add_chunk("pre-provenance archive", domain="conversation", namespace="chat-archive:s1", source_type="conversation")
    store.add_chunk("summary", domain="conversation", source="a2a:s1", source_type="harvest")
    store.add_chunk("fact", domain="fact", source="a2a:s1", source_type="extracted")
    store.add_chunk("legacy-thread summary", domain="conversation", source="chat:s1", source_type="harvest")
    store.add_chunk("goal-iteration fact", domain="fact", source="a2a:s1:goal-iter-3", source_type="extracted")
    # Not the chat's to forget. All stay.
    keep = [
        ("remembered on request", {"domain": "general", "source": "s1", "source_type": "conversation"}),  # memory_ingest
        ("background report", {"domain": "conversation", "source": "s1", "source_type": "background_report"}),
        ("pre-provenance fact", {"domain": "fact", "source": "harvest", "source_type": "extracted"}),
        (
            "another chat's archive",
            {"domain": "conversation", "namespace": "chat-archive:s10", "source": "a2a:s10", "source_type": "conversation"},
        ),
        ("another chat's fact", {"domain": "fact", "source": "a2a:s10", "source_type": "extracted"}),
        ("attachment", {"domain": "general", "namespace": "attach:s1"}),  # the route's own cleanup, not this helper's
    ]
    for content, kw in keep:
        store.add_chunk(content, **kw)

    # The third id is the thread-id resolver's answer. A fork's resolver may return the
    # bare session id, which is also the `source` memory_ingest and background reports
    # write, so it is the source_type filter that keeps those rows.
    assert forget_conversation_memory(store, "s1", ["a2a:s1", "chat:s1", "s1"]) == 6
    assert _contents(store) == sorted(content for content, _ in keep)


def test_forget_degrades_on_a_backend_without_the_delete_methods():
    calls: list[str] = []

    class _PluginBackend:
        def delete_by_namespace(self, namespace):
            calls.append(namespace)
            return 2

    assert forget_conversation_memory(_PluginBackend(), "s1", ["a2a:s1"]) == 2
    assert calls == ["chat-archive:s1"]
    assert forget_conversation_memory(object(), "s1", ["a2a:s1"]) == 0
    assert forget_conversation_memory(None, "s1", ["a2a:s1"]) == 0
    assert forget_conversation_memory(_PluginBackend(), "", ["a2a:s1"]) == 0  # never a bare "chat-archive:"
