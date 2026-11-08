"""Orphan ``chunk_vectors`` rows (stress test 2026-11-07: the hub agent.db held 43).

``chunk_vectors`` has no FK to ``chunks``; ``delete_by_id`` (the console's knowledge and
memory DELETE routes), ``delete_by_content`` and ``delete_by_heading`` never touched it, so
each delete left the vector behind. The hybrid store now drops the vector on those paths
too, and every store open prunes the orphans an older delete path left.
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
    """A DB an older core left with orphans (a delete that skipped the vector) is cleaned
    on the next open — by a hybrid store and by a plain store alike — and live vectors stay."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=_embed)
    keep = store.add_chunk("kept body", domain="docs")
    gone = store.add_chunk("gone body", domain="docs")
    gone2 = store.add_chunk("gone too", domain="docs")
    db = sqlite3.connect(str(path))
    db.execute("DELETE FROM chunks WHERE id IN (?, ?)", (gone, gone2))  # the pre-fix delete
    db.commit()
    db.close()
    assert _vector_ids(path) == {keep, gone, gone2}  # the orphans, reproduced

    KnowledgeStore(path)  # a vector-less boot (embeddings off) still prunes
    assert _vector_ids(path) == {keep}

    db = sqlite3.connect(str(path))
    db.execute("DELETE FROM chunks WHERE id = ?", (keep,))
    db.commit()
    db.close()
    HybridKnowledgeStore(path, embed_fn=_embed)  # and so does a hybrid boot
    assert _vector_ids(path) == set()


def test_delete_still_works_when_the_vector_table_is_missing(tmp_path):
    """No ``chunk_vectors`` (dropped, or a store that never embedded) → the vector step is
    skipped and the chunk delete still happens, as the forget path expects (#3973)."""
    store = HybridKnowledgeStore(tmp_path / "agent.db", embed_fn=_embed)
    cid = store.add_chunk("body", domain="docs", heading="h")
    other = store.add_chunk("other body", domain="docs", heading="h2")
    db = sqlite3.connect(str(store.path))
    db.execute("DROP TABLE chunk_vectors")
    db.commit()
    db.close()
    assert store.delete_by_id(cid)
    assert store.delete_by_heading("docs", "h2") == 1
    assert store.get_chunk(other) is None
    KnowledgeStore(store.path)  # the boot prune is a no-op without the table
