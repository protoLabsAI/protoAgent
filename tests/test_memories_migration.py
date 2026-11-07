"""ADR 0125 P2-B (D6): the boot migration draining durable memories out of ``chunks``
into the ``memories`` table (same agent.db), routed by ``source_type`` — the memory
sibling of the H1e history migration. Mirrors tests/test_history_migration.py's rules.
"""

from __future__ import annotations

import json
import sqlite3

import knowledge.memories_migration as mem_mig
from knowledge.history import HistoryStore
from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.memories import MemoryStore
from knowledge.memories_migration import migrate_memory_chunks_to_memories
from knowledge.store import KnowledgeStore, migrate_conversation_chunks_to_history

_TS = "2026-01-02T03:04:05.000000+00:00"


def _insert_raw_chunk(path, *, content, domain, source_type, review_state=None):
    """Insert a chunk via raw SQL so a NULL ``source_type`` (claude-import) or a NULL
    ``review_state`` can be seeded — ``add_chunk`` always stamps both."""
    db = sqlite3.connect(str(path))
    try:
        db.execute(
            "INSERT INTO chunks (content, domain, source_type, review_state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, domain, source_type, review_state, _TS, _TS),
        )
        db.commit()
    finally:
        db.close()


def _mark_superseded(path, old_id, new_id):
    """Stamp OLD as superseded by NEW the ADR 0108 D7 way (``superseded_by:<chunk id>``)."""
    db = sqlite3.connect(str(path))
    try:
        db.execute(
            "UPDATE chunks SET invalidated_at = ?, invalidation_reason = ? WHERE id = ?",
            (_TS, f"superseded_by:{new_id}", old_id),
        )
        db.commit()
    finally:
        db.close()


def _rows_by_content(path, table):
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        return {r["content"]: dict(r) for r in db.execute(f"SELECT * FROM {table}")}
    finally:
        db.close()


def _count(path, table):
    db = sqlite3.connect(str(path))
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def _seed_fleet(store):
    """A fixture modelled on the live fleet audit (2026-11-07)."""
    # → memories: agent-written freeform domains, a hot row, an extracted fact, an operator row.
    store.add_chunk("app note", domain="applications", source_type="conversation")
    store.add_chunk("research note", domain="research", source_type="conversation")
    store.add_chunk("loop lesson", domain="loop-lessons", source_type="conversation")
    store.add_chunk("hot pinned", domain="hot", source_type="conversation")
    store.add_chunk("extracted fact", domain="finding", source_type="extracted")
    store.add_chunk("operator pref", domain="preferences", source_type="operator")
    # stays in chunks (collection = the old domain): pdf, youtube, text, and a NULL-source_type row.
    store.add_chunk("a pdf chunk", domain="docs", source_type="pdf")
    store.add_chunk("a yt transcript", domain="media", source_type="youtube")
    store.add_chunk("a text blob", domain="notes", source_type="text")
    _insert_raw_chunk(store.path, content="claude import", domain="imported", source_type=None)
    # untouched: the history domain (the H1e migration owns these).
    store.add_chunk("conv archive", domain="conversation", source_type="conversation", namespace="chat-archive:s1")
    store.add_chunk("conv summary", domain="conversation", source_type="harvest")


def test_routes_live_fleet_fixture_with_exact_counts(tmp_path):
    """r1-r4: exact counts per destination; stays-rows gain ``collection=domain``;
    history-domain rows are left untouched."""
    path = tmp_path / "agent.db"
    store = KnowledgeStore(path)
    MemoryStore(store.path)  # create the memories table in the same file
    _seed_fleet(store)

    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 6, "chunks": 4}

    # Six durable memories moved; their old domains are preserved as legacy_domain.
    memories = _rows_by_content(store.path, "memories")
    assert {m["legacy_domain"] for m in memories.values()} == {
        "applications",
        "research",
        "loop-lessons",
        "hot",
        "finding",
        "preferences",
    }
    assert _count(store.path, "memories") == 6

    # Four rows stayed with collection = old domain; the two history rows are untouched.
    chunks = _rows_by_content(store.path, "chunks")
    assert set(chunks) == {
        "a pdf chunk",
        "a yt transcript",
        "a text blob",
        "claude import",
        "conv archive",
        "conv summary",
    }
    for content, domain in (
        ("a pdf chunk", "docs"),
        ("a yt transcript", "media"),
        ("a text blob", "notes"),
        ("claude import", "imported"),
    ):
        assert chunks[content]["collection"] == domain
    # domain='conversation' rows are untouched — no collection tag, content intact.
    assert chunks["conv archive"]["collection"] is None
    assert chunks["conv summary"]["collection"] is None
    assert chunks["conv archive"]["domain"] == "conversation"


def test_field_mapping_supersede_chain_and_vectors(tmp_path):
    """r5: every mapped field, the supersede rewrite to new ids, and verbatim vector copy."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=lambda text: [1.0, 0.0])
    MemoryStore(store.path)

    old_id = store.add_chunk(
        "old pref value", domain="preferences", heading="editor", source="sess-old",
        namespace="ns-pref", source_type="operator",
    )
    new_id = store.add_chunk(
        "new pref value", domain="preferences", heading="editor", source="sess-new",
        namespace="ns-pref", source_type="operator", expires_at="2099-01-01T00:00:00+00:00",
    )
    _mark_superseded(store.path, old_id, new_id)
    store.add_chunk("decision row", domain="decisions", source_type="operator")
    store.add_chunk("on demand note", domain="notes", source_type="conversation", delivery_policy="on_demand")
    store.add_chunk("hot note", domain="hot", source_type="conversation")
    _insert_raw_chunk(store.path, content="null rs operator", domain="general", source_type="operator")
    _insert_raw_chunk(store.path, content="null rs agent", domain="general", source_type="conversation")

    # A sentinel vector proves the row is COPIED, not re-embedded (embed_fn returns [1,0]).
    sentinel = json.dumps([9.0, 9.0])
    db = sqlite3.connect(str(store.path))
    db.execute("UPDATE chunk_vectors SET vec = ? WHERE chunk_id = ?", (sentinel, new_id))
    db.commit()
    db.close()

    pre = _rows_by_content(store.path, "chunks")
    pre_vecs = {r[0]: r[1] for r in sqlite3.connect(str(store.path)).execute("SELECT chunk_id, vec FROM chunk_vectors")}
    assert migrate_memory_chunks_to_memories(store.path)["memories"] == 7

    mem = _rows_by_content(store.path, "memories")
    new = mem["new pref value"]
    assert new["kind"] == "profile"  # domain='preferences'
    assert new["provenance"] == "operator"
    assert new["review_state"] == "confirmed"  # carried from the operator write
    assert new["scope"] == "ns-pref"  # namespace → scope
    assert new["source_session"] == "sess-new"  # source → source_session
    assert new["legacy_domain"] == "preferences"
    assert new["delivery"] == "retrieved"
    assert new["expires_at"] == pre["new pref value"]["expires_at"]
    assert new["created_at"] == pre["new pref value"]["created_at"]
    assert new["updated_at"] == pre["new pref value"]["updated_at"]

    # Supersede chain rewritten to the NEW memories ids; invalidated_at carried.
    old = mem["old pref value"]
    assert old["superseded_by"] == new["id"]
    assert old["invalidation_reason"] == f"superseded_by:{new['id']}"
    assert old["invalidated_at"] == _TS

    # kind / delivery variants.
    assert mem["decision row"]["kind"] == "decision"  # domain='decisions'
    assert mem["on demand note"]["delivery"] == "on_demand"
    assert mem["hot note"]["delivery"] == "always" and mem["hot note"]["kind"] == "standing"
    # NULL review_state → the trust-tier rule.
    assert mem["null rs operator"]["review_state"] == "confirmed"
    assert mem["null rs agent"]["review_state"] == "pending" and mem["null rs agent"]["provenance"] == "agent"

    # Vectors copied verbatim into memory_vectors (sentinel survives → no re-embed).
    vecs = {r[0]: r[1] for r in sqlite3.connect(str(store.path)).execute("SELECT memory_id, vec FROM memory_vectors")}
    assert vecs[new["id"]] == sentinel == pre_vecs[new_id]
    assert vecs[old["id"]] == pre_vecs[old_id]
    # Five rows were added through the embedding store; the two raw-inserted rows get none.
    assert len(vecs) == 5


def test_idempotent_backup_taken_once_and_phase1_untouched(tmp_path):
    """r6: re-run moves nothing; a row written after the stamp moves next run; the
    pre-0125-p2 backup is taken once and never overwritten; pre-0125 is never touched."""
    path = tmp_path / "agent.db"
    store = KnowledgeStore(path)
    MemoryStore(store.path)
    store.add_chunk("mem one", domain="applications", source_type="conversation")
    store.add_chunk("a pdf", domain="docs", source_type="pdf")

    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 1, "chunks": 1}
    backup = store.path.with_name("agent.db.pre-0125-p2")
    assert backup.exists()
    original = backup.read_bytes()
    assert not store.path.with_name("agent.db.pre-0125").exists()  # Phase-1 backup untouched

    # Re-run is a pure no-op; the backup is not re-taken.
    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 0, "chunks": 0}
    assert backup.read_bytes() == original

    # Rows written after the stamp move / get tagged on the next run; backup stays original.
    store.add_chunk("mem two", domain="research", source_type="conversation")
    store.add_chunk("another pdf", domain="docs", source_type="pdf")
    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 1, "chunks": 1}
    assert backup.read_bytes() == original
    assert not store.path.with_name("agent.db.pre-0125").exists()


def test_mid_transaction_failure_rolls_back_everything(tmp_path, monkeypatch):
    """r7: a failure after the first insert leaves chunks, memories, FTS and vectors unchanged."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=lambda text: [1.0, 0.0])
    MemoryStore(store.path)
    store.add_chunk("mem a", domain="applications", source_type="conversation")
    store.add_chunk("mem b", domain="research", source_type="conversation")
    store.add_chunk("a pdf", domain="docs", source_type="pdf")

    real = mem_mig._insert_memory
    calls = {"n": 0}

    def flaky(db, row):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.IntegrityError("injected mid-transaction failure")
        return real(db, row)

    monkeypatch.setattr(mem_mig, "_insert_memory", flaky)

    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 0, "chunks": 0}
    assert calls["n"] == 2  # the failure really fired mid-transaction

    assert _count(store.path, "chunks") == 3
    assert _count(store.path, "chunks_fts") == 3
    assert _count(store.path, "chunk_vectors") == 3
    assert _count(store.path, "memories") == 0
    assert _count(store.path, "memories_fts") == 0
    assert _count(store.path, "memory_vectors") == 0


def test_history_then_memory_back_to_back_keeps_integrity(tmp_path):
    """r8: history then memory migration on a pre-Phase-1 DB — integrity ok, FTS = table rows."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=lambda text: [1.0, 0.0])
    HistoryStore(store.path)
    MemoryStore(store.path)
    store.add_chunk("conv one", domain="conversation", source_type="conversation",
                    namespace="chat-archive:s1", source="t1")
    store.add_chunk("conv two", domain="conversation", source_type="harvest", source="t2")
    store.add_chunk("mem one", domain="applications", source_type="conversation")
    store.add_chunk("mem two", domain="finding", source_type="extracted")
    store.add_chunk("mem three", domain="preferences", source_type="operator")
    store.add_chunk("a pdf", domain="docs", source_type="pdf")
    _insert_raw_chunk(store.path, content="claude import", domain="imported", source_type=None)

    assert migrate_conversation_chunks_to_history(store.path) == 2
    assert migrate_memory_chunks_to_memories(store.path) == {"memories": 3, "chunks": 2}

    db = sqlite3.connect(str(store.path))
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for tbl in ("chunks", "memories", "history"):
            rows = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            fts = db.execute(f"SELECT COUNT(*) FROM {tbl}_fts").fetchone()[0]
            assert rows == fts, f"{tbl} vs {tbl}_fts: {rows} != {fts}"
    finally:
        db.close()


def test_missing_and_tableless_dbs_are_noops(tmp_path):
    """No file, no chunks table, and no memories table are each a clean no-op."""
    assert migrate_memory_chunks_to_memories(None) == {"memories": 0, "chunks": 0}
    assert migrate_memory_chunks_to_memories(tmp_path / "nope.db") == {"memories": 0, "chunks": 0}

    # chunks present, memories absent → nothing to migrate into.
    only_chunks = tmp_path / "chunks-only.db"
    KnowledgeStore(only_chunks)
    assert migrate_memory_chunks_to_memories(only_chunks) == {"memories": 0, "chunks": 0}

    # memories present, chunks absent (a plugin KnowledgeBackend serves chunks elsewhere).
    only_mem = tmp_path / "mem-only.db"
    MemoryStore(only_mem)
    assert migrate_memory_chunks_to_memories(only_mem) == {"memories": 0, "chunks": 0}
    assert not only_mem.with_name("mem-only.db.pre-0125-p2").exists()
