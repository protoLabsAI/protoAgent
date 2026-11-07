"""Memory forget shares a transaction with private chunks and history."""

import sqlite3
from contextlib import closing

import pytest

from graph.conversation_harvest import (
    abort_forget,
    begin_forget,
    conversation_forget_store,
    finish_forget,
    forget_conversation_memory,
    sweep_orphaned_forgets,
)
from knowledge.history import HistoryStore
from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    path = tmp_path / "agent.db"
    knowledge = KnowledgeStore(path)
    history = HistoryStore(path)
    memory = MemoryStore(path, embed_fn=lambda text: [1.0, 0.0])
    return knowledge, history, memory


def seed(memory):
    return [
        memory.add(f"memory token{i}", kind="fact", provenance="operator", delivery="always", **origin)
        for i, origin in enumerate(
            [
                {"source_session": "s1"},
                {"thread_id": "a2a:s1"},
                {"thread_id": "a2a:s1:goal-iter-2"},
                {"source_session": "a2a:s1:goal-iter-3"},
                {"thread_id": "s1"},
                {"source_session": "s2"},
            ]
        )
    ]


def begin(stores):
    combined = conversation_forget_store(*stores)
    return combined, begin_forget(combined, "s1", ["a2a:s1"])


def test_begin_filters_all_readers_and_retry_preserves_new_rows(stores):
    _, _, memory = stores
    ids = seed(memory)
    combined, marker = begin(stores)
    assert marker == "forget_pending:s1"
    assert [r.id for r in memory.list()] == [ids[-1]]
    assert [r.id for r in memory.search("memory", k=20)] == [ids[-1]]
    assert memory.always_on() == "memory token5"
    assert memory.count() == len(ids)
    fresh = memory.add("fresh", kind="fact", provenance="operator", source_session="s1")
    assert begin_forget(combined, "s1", ["a2a:s1"]) == marker
    finish_forget(combined, marker)
    assert memory.get(fresh) is not None


@pytest.mark.parametrize("reason", [None, "", "operator|prev:old"])
def test_abort_restores_exact_prior_state(stores, reason):
    _, _, memory = stores
    ids = seed(memory)
    with closing(sqlite3.connect(memory.path)) as db, db:
        db.execute(
            "UPDATE memories SET invalidated_at = '2020-01-01', invalidation_reason = ? WHERE id = ?", (reason, ids[0])
        )
    before = [memory.get(id) for id in ids]
    combined, marker = begin(stores)
    assert memory.get(ids[0]).invalidation_reason == marker + "|prev:" + (reason or "")
    abort_forget(combined, marker)
    assert [memory.get(id) for id in ids] == before
    assert combined.forget_pending_markers() == {}


def test_finish_cleans_fts_and_vectors(stores):
    _, _, memory = stores
    ids = seed(memory)
    combined, marker = begin(stores)
    assert finish_forget(combined, marker) == 5
    with closing(sqlite3.connect(memory.path)) as db:
        assert db.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'memory'").fetchall() == [(ids[-1],)]
        assert db.execute("SELECT memory_id FROM memory_vectors").fetchall() == [(ids[-1],)]
        assert db.execute("SELECT * FROM memory_forget_prior").fetchall() == []
    assert memory.count() == 1


@pytest.mark.parametrize("incomplete", [True, False])
def test_sweep_memory_only_orphans(stores, incomplete):
    _, _, memory = stores
    seed(memory)
    combined, _ = begin(stores)
    seen = []
    result = sweep_orphaned_forgets(
        combined, retirement_incomplete=lambda sid, at: seen.append((sid, at)) or incomplete
    )
    assert result == {"restored": int(incomplete), "finished": int(not incomplete)}
    assert seen[0][0] == "s1" and seen[0][1]
    assert len(memory.list()) == (6 if incomplete else 1)
    assert combined.forget_pending_markers() == {}


@pytest.mark.parametrize("phase", ["begin", "abort", "finish"])
def test_all_three_tables_roll_back_on_memory_failure(stores, phase):
    knowledge, history, memory = stores
    seed(memory)
    knowledge.add_chunk("fact", source="a2a:s1", source_type="harvest")
    history.add("archive", session_id="s1", kind="archive")
    combined = conversation_forget_store(*stores)
    marker = "forget_pending:s1"
    if phase != "begin":
        begin_forget(combined, "s1", ["a2a:s1"])
    with closing(sqlite3.connect(memory.path)) as db, db:
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in ("chunks", "history", "memories")}
        event = "DELETE" if phase == "finish" else "UPDATE"
        db.execute(f"CREATE TRIGGER fail_memory BEFORE {event} ON memories BEGIN SELECT RAISE(ABORT, 'boom'); END")
    if phase == "abort":
        assert abort_forget(combined, marker) == 0  # failure-path API deliberately swallows the error
    else:
        with pytest.raises(sqlite3.DatabaseError, match="boom"):
            if phase == "begin":
                begin_forget(combined, "s1", ["a2a:s1"])
            else:
                finish_forget(combined, marker)
    with closing(sqlite3.connect(memory.path)) as db:
        assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in before} == before


def test_none_keeps_existing_behavior(stores):
    knowledge, history, memory = stores
    seed(memory)
    assert conversation_forget_store(knowledge, history, None) is knowledge
    marker = begin_forget(knowledge, "s1", ["a2a:s1"])
    finish_forget(knowledge, marker)
    assert len(memory.list()) == 6


def test_one_step(stores):
    knowledge, _, memory = stores
    seed(memory)
    assert forget_conversation_memory(knowledge, "s1", ["a2a:s1"], memory_store=memory) == 5
    assert memory.count() == 1


async def test_runtime_sweep_memory_without_knowledge_or_history(stores, monkeypatch):
    from runtime.state import STATE
    from server import maintenance_loops

    _, _, memory = stores
    seed(memory)
    begin_forget(conversation_forget_store(None, None, memory), "s1", ["a2a:s1"])
    monkeypatch.setattr(STATE, "knowledge_store", None)
    monkeypatch.setattr(STATE, "history_store", None)
    monkeypatch.setattr(STATE, "memory_store", memory)
    monkeypatch.setattr(maintenance_loops, "_forget_retirement_incomplete", lambda sid, at: False)
    await maintenance_loops._sweep_orphaned_forgets()
    assert memory.count() == 1


def test_selection_treats_wildcards_literally(stores):
    memory = stores[2]
    kept = memory.add("keep", kind="fact", provenance="operator", thread_id="a2a:sX:goal-iter-1")
    removed = memory.add("remove", kind="fact", provenance="operator", thread_id="a2a:s_:goal-iter-1")
    combined = conversation_forget_store(*stores)
    marker = begin_forget(combined, "s_", ["a2a:s_"])
    finish_forget(combined, marker)
    assert memory.get(kept) is not None
    assert memory.get(removed) is None


def test_separate_knowledge_database_retry(stores, tmp_path, monkeypatch):
    _, history, memory = stores
    knowledge = KnowledgeStore(tmp_path / "commons.db")
    knowledge.add_chunk("old fact", source="a2a:s1", source_type="harvest")
    ids = seed(memory)
    history_id = history.add("archive", session_id="s1", kind="archive")
    combined = conversation_forget_store(knowledge, history, memory)
    marker = begin_forget(combined, "s1", ["a2a:s1"])
    fresh = memory.add("fresh", kind="fact", provenance="operator", source_session="s1")
    with closing(sqlite3.connect(memory.path)) as db, db:
        db.execute("CREATE TRIGGER fail_memory BEFORE DELETE ON memories BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.DatabaseError):
        finish_forget(combined, marker)
    assert knowledge.count_forget_pending(marker) == 0
    assert begin_forget(combined, "s1", ["a2a:s1"]) == marker
    with closing(sqlite3.connect(memory.path)) as db, db:
        db.execute("DROP TRIGGER fail_memory")
    finish_forget(combined, marker)
    assert memory.get(ids[0]) is None
    assert history.get(history_id) is None
    assert memory.get(fresh) is not None


def test_optional_memory_parameters_and_invalidated_only_orphan(stores):
    knowledge, _, memory = stores
    id = memory.add("old", kind="fact", provenance="operator", source_session="s1")
    with closing(sqlite3.connect(memory.path)) as db, db:
        db.execute(
            "UPDATE memories SET invalidated_at = '2020-01-01', invalidation_reason = 'old reason' WHERE id = ?", (id,)
        )
    marker = begin_forget(knowledge, "s1", [], memory_store=memory)
    abort_forget(knowledge, marker, memory_store=memory)
    assert memory.get(id).invalidation_reason == "old reason"
    begin_forget(knowledge, "s1", [], memory_store=memory)
    stamps = []
    result = sweep_orphaned_forgets(
        knowledge,
        memory_store=memory,
        retirement_incomplete=lambda sid, at: stamps.append(at) or False,
    )
    assert result == {"finished": 1, "restored": 0}
    assert stamps[0] > "2020-01-01"
    assert memory.get(id) is None


@pytest.mark.parametrize("missing_vectors", [False, True])
def test_hybrid_chunk_vector_cleanup_is_preserved(stores, missing_vectors):
    from knowledge.hybrid_store import HybridKnowledgeStore

    _, history, memory = stores
    knowledge = HybridKnowledgeStore(memory.path, embed_fn=lambda text: [1.0, 0.0])
    knowledge.add_chunk("gone", source="a2a:s1", source_type="extracted")
    knowledge.add_chunk("kept", source="a2a:s2", source_type="extracted")
    if missing_vectors:
        with closing(sqlite3.connect(memory.path)) as db, db:
            db.execute("DROP TABLE chunk_vectors")
    combined = conversation_forget_store(knowledge, history, memory)
    marker = begin_forget(combined, "s1", ["a2a:s1"])
    finish_forget(combined, marker)
    assert [row.content for row in knowledge.list_chunks()] == ["kept"]
    if not missing_vectors:
        with closing(sqlite3.connect(memory.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1


def test_empty_marker_never_deletes_memories(stores):
    _, _, memory = stores
    seed(memory)
    with closing(sqlite3.connect(memory.path)) as db, db:
        db.execute("UPDATE memories SET invalidation_reason = ''")
    assert finish_forget(stores[0], "", memory_store=memory) == 0
    assert memory.count() == 6
