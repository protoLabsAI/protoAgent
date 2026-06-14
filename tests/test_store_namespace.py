"""ADR 0021: the chunks table carries a namespace dimension + delete_by_id.

namespace makes per-project/owner scoping (ADR 0007) a later filter, not a
migration. delete_by_id backs fact consolidation.
"""

from __future__ import annotations

import sqlite3

from knowledge.store import KnowledgeStore


def test_add_and_list_with_namespace(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("scoped fact", domain="fact", namespace="proj-a")
    store.add_chunk("other scoped fact", domain="fact", namespace="proj-b")
    store.add_chunk("global fact", domain="fact")  # namespace None

    assert len(store.list_chunks(domain="fact")) == 3  # no filter = all
    assert [c.content for c in store.list_chunks(domain="fact", namespace="proj-a")] == ["scoped fact"]
    assert len(store.list_chunks(domain="fact", namespace="proj-b")) == 1


def test_namespace_persists_on_the_chunk(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_finding("a fact", finding_type="fact", namespace="owner-1")
    c = store.list_chunks(domain="finding", limit=1)[0]
    assert c.namespace == "owner-1"


def test_delete_by_id(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    rid = store.add_chunk("delete me", domain="general")
    assert store.delete_by_id(rid) is True
    assert store.list_chunks(limit=10) == []
    assert store.delete_by_id(rid) is False  # already gone


def test_namespace_migration_on_preexisting_db(tmp_path):
    """A DB created without the namespace column gets it added on next open."""
    path = tmp_path / "old.db"
    # Simulate a pre-ADR-0021 schema: chunks table with no namespace column.
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, source_type TEXT, "
        "finding_type TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO chunks (content, domain, created_at, updated_at) VALUES ('old row', 'general', 'x', 'x')"
    )
    db.commit()
    db.close()

    # Opening through KnowledgeStore runs the migration; old + new rows coexist.
    store = KnowledgeStore(path)
    store.add_chunk("new row", domain="general", namespace="ns")
    rows = {c.content: c.namespace for c in store.list_chunks(limit=10)}
    assert rows["old row"] is None
    assert rows["new row"] == "ns"


def test_delete_by_namespace(tmp_path):
    """delete_by_namespace drops exactly the namespace's chunks (ephemeral chat
    attachments are cleaned this way), leaving other namespaces + globals."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("attach one", domain="attachment", namespace="attach:s1")
    store.add_chunk("attach two", domain="attachment", namespace="attach:s1")
    store.add_chunk("other session", domain="attachment", namespace="attach:s2")
    store.add_chunk("a global fact", domain="fact")

    removed = store.delete_by_namespace("attach:s1")
    assert removed == 2
    remaining = {c.as_dict()["content"] for c in store.list_chunks()}
    assert "attach one" not in remaining and "attach two" not in remaining
    assert "other session" in remaining and "a global fact" in remaining
    assert store.delete_by_namespace("attach:s1") == 0   # idempotent
    assert store.delete_by_namespace("") == 0            # guard


def test_hybrid_delete_by_namespace_drops_vectors(tmp_path):
    """The hybrid override also clears the side chunk_vectors table (no FK cascade)."""
    import sqlite3 as _sql

    from knowledge.hybrid_store import HybridKnowledgeStore

    db = str(tmp_path / "kb.db")
    store = HybridKnowledgeStore(db, embed_fn=lambda t: [1.0, 0.0])
    store.add_chunk("attach one", domain="attachment", namespace="attach:s1")
    store.add_chunk("keep me", domain="fact")
    conn = _sql.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 2
    conn.close()

    store.delete_by_namespace("attach:s1")
    conn = _sql.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1  # only the kept one
    conn.close()
