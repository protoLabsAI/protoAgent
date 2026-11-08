"""Orphan ``chunk_vectors`` rows (stress test 2026-11-07: the hub agent.db held 43).

``chunk_vectors`` has no FK to ``chunks``; ``delete_by_id`` (the console's knowledge and
memory DELETE routes), ``delete_by_content`` and ``delete_by_heading`` never touched it, so
each delete left the vector behind. A delete trigger now drops the vector with the row on
every path, and boot prunes orphans an older delete path left.
"""

from __future__ import annotations

import sqlite3

import pytest

from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.store import KnowledgeStore


def _embed(text):
    return [1.0, 0.0]


def _vector_ids(path):
    db = sqlite3.connect(str(path))
    try:
        return {r[0] for r in db.execute("SELECT chunk_id FROM chunk_vectors")}
    finally:
        db.close()


@pytest.mark.parametrize(
    "delete",
    [
        lambda s, cid: s.delete_by_id(cid),
        lambda s, cid: s.delete_by_content("doomed body"),
        lambda s, cid: s.delete_by_heading("docs", "doomed"),
    ],
    ids=["delete_by_id", "delete_by_content", "delete_by_heading"],
)
def test_every_delete_path_drops_the_vector(tmp_path, delete):
    store = HybridKnowledgeStore(tmp_path / "agent.db", embed_fn=_embed)
    keep = store.add_chunk("kept body", domain="docs", heading="kept")
    doomed = store.add_chunk("doomed body", domain="docs", heading="doomed")
    assert _vector_ids(store.path) == {keep, doomed}

    assert delete(store, doomed)
    assert _vector_ids(store.path) == {keep}


def test_boot_prunes_orphan_vectors_left_by_an_older_delete(tmp_path):
    """A DB an older core left with orphans (a raw delete, no trigger yet) is cleaned on the
    next open — by a hybrid store and by a plain store alike — and live vectors stay."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=_embed)
    keep = store.add_chunk("kept body", domain="docs")
    gone = store.add_chunk("gone body", domain="docs")
    db = sqlite3.connect(str(path))
    db.execute("DROP TRIGGER IF EXISTS chunks_vectors_ad")  # the pre-fix schema
    db.execute("DELETE FROM chunks WHERE id = ?", (gone,))
    db.commit()
    db.close()
    assert _vector_ids(path) == {keep, gone}  # the orphan, reproduced

    KnowledgeStore(path)  # a vector-less boot (embeddings off) still prunes
    assert _vector_ids(path) == {keep}

    # …and re-installs the trigger, so a later raw delete cannot orphan either.
    db = sqlite3.connect(str(path))
    db.execute("DELETE FROM chunks WHERE id = ?", (keep,))
    db.commit()
    db.close()
    assert _vector_ids(path) == set()


def test_plain_store_without_vectors_table_deletes_fine(tmp_path):
    """No ``chunk_vectors`` → no trigger (it would name a missing table) and deletes work."""
    store = KnowledgeStore(tmp_path / "agent.db")
    cid = store.add_chunk("body", domain="docs")
    assert store.delete_by_id(cid)
    db = sqlite3.connect(str(store.path))
    try:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vectors_ad'").fetchone() is None
    finally:
        db.close()
