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
