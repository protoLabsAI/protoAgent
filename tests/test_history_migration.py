"""ADR 0125 H1e: the boot migration draining legacy ``domain='conversation'``
chunks into the ``history`` table (same agent.db), and the readers that follow it —
``session_search`` picks the archive up, ``memory_recall`` no longer does.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from graph.session_search import search_sessions
from knowledge.history import HistoryStore
from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.store import (
    HISTORY_DOMAIN,
    KnowledgeStore,
    _migration_session_id,
    migrate_conversation_chunks_to_history,
)
from tools.lg_tools import _build_memory_tools


def _by_name(tools):
    return {t.name: t for t in tools}


# Keyword unique to the compaction archive, so a recall/search hit is unambiguous.
_ARCHIVE_TOKEN = "zorblaxrelease"
_FACT_TOKEN = "tabspreference"


def _seed_conversation_and_fact(store) -> None:
    """Three legacy history rows (one per source_type) plus one real memory fact."""
    store.add_chunk(
        f"Conversation archive: the operator shipped the {_ARCHIVE_TOKEN}.",
        domain=HISTORY_DOMAIN,
        heading="Conversation archive (sess-1)",
        source="thread-1",
        source_type="conversation",
        namespace="chat-archive:sess-1",
    )
    store.add_chunk(
        "Conversation summary: the thread covered deployment runbooks.",
        domain=HISTORY_DOMAIN,
        heading="Conversation summary (thread-x)",
        source="thread-x",
        source_type="harvest",
    )
    store.add_chunk(
        "Background report: the research job finished and was delivered.",
        domain=HISTORY_DOMAIN,
        heading="Background report (job-7)",
        source="sess-1",
        source_type="background_report",
    )
    # A genuine memory fact — NOT conversation history — that must survive untouched.
    store.add_chunk(
        f"Josh prefers {_FACT_TOKEN} over spaces.",
        domain="general",
        heading="editor",
        source="sess-1",
    )


def _history_count(path) -> int:
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    finally:
        db.close()


def test_migration_evidence_memory_recall_to_session_search(tmp_path):
    """The issue's 'Done when' evidence: before the migration ``memory_recall`` finds the
    archive; after it ``session_search`` finds the same archive, ``memory_recall`` does
    not, the fact survives, and a second boot moves nothing."""
    path = tmp_path / "agent.db"
    store = KnowledgeStore(path)
    history = HistoryStore(path)
    _seed_conversation_and_fact(store)
    tools = _by_name(_build_memory_tools(store, history_store=history))

    # Before: memory_recall surfaces the archive (history still lives in chunks).
    before = asyncio.run(tools["memory_recall"].ainvoke({"query": _ARCHIVE_TOKEN}))
    assert _ARCHIVE_TOKEN in before

    moved = migrate_conversation_chunks_to_history(path)
    assert moved == 3
    backup = path.with_name("agent.db.pre-0125")
    assert backup.exists(), "the pre-0125 backup must be written before anything moves"

    # After: session_search finds the archive, labelled with its kind.
    mem = tmp_path / "mem"
    mem.mkdir()
    hits = search_sessions(_ARCHIVE_TOKEN, history_store=history, memory_dir=str(mem))
    assert hits and any(h["kind"] == "archive" and h["session_id"] == "sess-1" for h in hits)

    # After: memory_recall no longer carries it; the fact is still recallable.
    after = asyncio.run(tools["memory_recall"].ainvoke({"query": _ARCHIVE_TOKEN}))
    assert after == "No matches."
    fact = asyncio.run(tools["memory_recall"].ainvoke({"query": _FACT_TOKEN}))
    assert _FACT_TOKEN in fact

    # Idempotent: a second boot copies and moves nothing.
    backup.unlink()
    assert migrate_conversation_chunks_to_history(path) == 0
    assert not backup.exists(), "a stamped DB must not be re-copied"
    assert _history_count(path) == 3


def test_migration_maps_kinds_sessions_and_leaves_other_chunks(tmp_path):
    """source_type → kind and session_id derivation, and non-conversation chunks and
    their FTS entries are untouched (r2)."""
    path = tmp_path / "agent.db"
    store = KnowledgeStore(path)
    history = HistoryStore(path)
    _seed_conversation_and_fact(store)

    assert migrate_conversation_chunks_to_history(path) == 3

    by_kind = {e.kind: e for e in history.list_for_session("sess-1")}
    by_kind.update({e.kind: e for e in history.list_for_session("thread-x")})
    assert set(by_kind) == {"archive", "summary", "report"}
    # session_id: chat-archive namespace for the archive, source for summary/report.
    assert by_kind["archive"].session_id == "sess-1"
    assert by_kind["archive"].thread_id == "thread-1"
    assert by_kind["summary"].session_id == "thread-x"
    assert by_kind["report"].session_id == "sess-1"

    # The moved rows left chunks (and its FTS index); the fact stayed.
    assert not store.search(_ARCHIVE_TOKEN)
    remaining = store.list_chunks(limit=100)
    assert [c.domain for c in remaining] == ["general"]
    assert store.search(_FACT_TOKEN), "the non-conversation fact must still be searchable"

    # The pre-0125 backup is a real snapshot: it still holds the conversation rows.
    backup = sqlite3.connect(str(path.with_name("agent.db.pre-0125")))
    try:
        n = backup.execute(
            "SELECT COUNT(*) FROM chunks WHERE domain = ?", (HISTORY_DOMAIN,)
        ).fetchone()[0]
    finally:
        backup.close()
    assert n == 3


def test_migration_deletes_vector_entries_in_one_transaction(tmp_path):
    """On a hybrid store, the moved chunks' ``chunk_vectors`` rows go too; only the
    surviving fact keeps a vector (r2)."""
    path = tmp_path / "agent.db"
    store = HybridKnowledgeStore(path, embed_fn=lambda text: [1.0, 0.0])
    HistoryStore(path)  # create the `history` table in the same file
    _seed_conversation_and_fact(store)

    def vector_count() -> int:
        db = sqlite3.connect(str(path))
        try:
            return db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
        finally:
            db.close()

    assert vector_count() == 4
    assert migrate_conversation_chunks_to_history(path) == 3
    assert vector_count() == 1  # only the surviving fact


def test_migration_idempotent_marker_stamped_on_empty_db(tmp_path):
    """A DB with no conversation rows still stamps the marker, so a second boot does and
    copies nothing (r3)."""
    path = tmp_path / "agent.db"
    store = KnowledgeStore(path)
    HistoryStore(path)  # create the `history` table in the same file
    store.add_chunk("just a fact", domain="general")

    assert migrate_conversation_chunks_to_history(path) == 0
    # No rows to move → no backup is written.
    assert not path.with_name("agent.db.pre-0125").exists()
    # Second run is a pure no-op (marker present).
    assert migrate_conversation_chunks_to_history(path) == 0


def test_migration_noop_without_chunks_table(tmp_path):
    """A core agent.db that holds only ``history`` (a plugin KnowledgeBackend serves
    chunks elsewhere) has nothing to migrate and is left alone."""
    path = tmp_path / "agent.db"
    HistoryStore(path)  # creates `history` but never a `chunks` table
    assert migrate_conversation_chunks_to_history(path) == 0
    assert not path.with_name("agent.db.pre-0125").exists()


def test_migration_missing_file_is_noop(tmp_path):
    assert migrate_conversation_chunks_to_history(tmp_path / "nope.db") == 0
    assert migrate_conversation_chunks_to_history(None) == 0


@pytest.mark.parametrize(
    "namespace,source,expected",
    [
        ("chat-archive:sess-9", "thread-1", "sess-9"),  # archive: namespace wins
        (None, "thread-x", "thread-x"),  # harvest: falls back to source
        ("", "sess-1", "sess-1"),  # report: no namespace, source
        ("chat-archive:", "sess-1", "sess-1"),  # empty suffix → source
        (None, None, ""),  # neither → '' (schema requires NOT NULL)
    ],
)
def test_migration_session_id_derivation(namespace, source, expected):
    assert _migration_session_id(namespace, source) == expected
