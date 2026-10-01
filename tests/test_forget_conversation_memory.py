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


def test_two_phase_forget_hides_restores_and_deletes_exactly_its_rows(tmp_path):
    """#3957 review: the chat delete hides the chat's rows before retirement (recall and
    fact dedupe skip them), un-hides them if retirement fails, and deletes exactly them
    once it succeeds — never a row written in between (the harvest's)."""
    from graph.conversation_harvest import abort_forget, begin_forget, finish_forget

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("archive", domain="conversation", namespace="chat-archive:s1", source="a2a:s1")
    store.add_chunk("summary", domain="conversation", source="a2a:s1", source_type="harvest")
    old = store.add_chunk("fact v1", domain="fact", source="a2a:s1:goal-iter-2", source_type="extracted")
    new = store.add_chunk("fact v2", domain="fact", source="a2a:s1:goal-iter-2", source_type="extracted")
    store.invalidate_chunk(old, superseded_by=new)  # supersession history: never recalled
    store.add_chunk("kept: user ingest", domain="general", source="a2a:s1", source_type="conversation")
    store.add_chunk("kept: other chat", domain="fact", source="a2a:s10", source_type="extracted")
    visible = sorted(c.content for c in store.list_chunks(limit=500))

    every_row = _contents(store)
    marker = begin_forget(store, "s1", ["a2a:s1"])
    assert sorted(c.content for c in store.list_chunks(limit=500)) == ["kept: other chat", "kept: user ingest"]
    assert _contents(store) == every_row  # phase 1 deletes NOTHING (review B2)
    assert abort_forget(store, marker) == 3
    assert sorted(c.content for c in store.list_chunks(limit=500)) == visible
    # The superseded row went back exactly as it was: still invalidated, its chain intact.
    (v1,) = [c for c in store.list_chunks(limit=500, include_invalidated=True) if c.content == "fact v1"]
    assert v1.invalidated_at is not None and v1.invalidation_reason == f"superseded_by:{new}"

    marker = begin_forget(store, "s1", ["a2a:s1"])
    store.add_chunk("harvest written during retirement", domain="conversation", source="a2a:s1", source_type="harvest")
    assert begin_forget(store, "s1", ["a2a:s1"]) == marker  # a retry hides nothing new
    assert finish_forget(store, marker) == 4  # the 3 hidden + the superseded row it held
    assert _contents(store) == sorted(["harvest written during retirement", "kept: other chat", "kept: user ingest"])


def test_two_phase_forget_drops_hybrid_vectors(tmp_path):
    from graph.conversation_harvest import begin_forget, finish_forget
    from knowledge.hybrid_store import HybridKnowledgeStore

    db = tmp_path / "kb.db"
    store = HybridKnowledgeStore(db, embed_fn=lambda t: [1.0, 0.0])
    store.add_chunk("gone", domain="fact", source="a2a:s1", source_type="extracted")
    store.add_chunk("kept", domain="fact", source="a2a:s2", source_type="extracted")

    finish_forget(store, begin_forget(store, "s1", ["a2a:s1"]))

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
    finally:
        conn.close()
    assert _contents(store) == ["kept"]


def test_forget_delete_degrades_to_chunk_only_on_a_vector_table_error(tmp_path):
    """#3973: a vector-table error in ``_drop_vectors`` (the forget-delete's vector
    cleanup) must degrade like the sibling cleanups — chunk-only delete — not abort."""
    from graph.conversation_harvest import begin_forget
    from knowledge.hybrid_store import HybridKnowledgeStore

    db = tmp_path / "kb.db"
    store = HybridKnowledgeStore(db, embed_fn=lambda t: [1.0, 0.0])
    store.add_chunk("gone", domain="fact", source="a2a:s1", source_type="extracted")
    store.add_chunk("kept", domain="fact", source="a2a:s2", source_type="extracted")
    marker = begin_forget(store, "s1", ["a2a:s1"])

    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE chunk_vectors")  # any vector-table DatabaseError
        conn.commit()
    finally:
        conn.close()

    store.delete_forget_pending(marker)
    assert _contents(store) == ["kept"]


def test_begin_forget_declines_a_store_without_the_primitives():
    from graph.conversation_harvest import begin_forget

    assert begin_forget(object(), "s1", ["a2a:s1"]) is None


def _seed(store):
    store.add_chunk("old summary", domain="conversation", source="a2a:s1", source_type="harvest")
    old = store.add_chunk("fact v1", domain="fact", source="a2a:s1", source_type="extracted")
    new = store.add_chunk("fact v2", domain="fact", source="a2a:s1", source_type="extracted")
    store.invalidate_chunk(old, superseded_by=new)


def test_sweep_finishes_a_forget_whose_session_was_retired(tmp_path):
    """Review B1: a crash after retirement (or a final delete nobody retried) used to
    leave hidden rows forever. A marker whose session has no checkpoint from before the
    forget began is a completed retirement: the sweep finishes the delete."""
    from graph.conversation_harvest import begin_forget, sweep_orphaned_forgets

    store = KnowledgeStore(tmp_path / "kb.db")
    _seed(store)
    begin_forget(store, "s1", ["a2a:s1"])
    seen = []

    res = sweep_orphaned_forgets(store, retirement_incomplete=lambda sid, at: seen.append((sid, at)) or False)

    assert res == {"finished": 1, "restored": 0}
    assert seen and seen[0][0] == "s1" and seen[0][1]
    assert _contents(store) == []
    assert store.forget_pending_markers() == {}


def test_sweep_restores_a_forget_whose_retirement_never_completed(tmp_path):
    from graph.conversation_harvest import begin_forget, sweep_orphaned_forgets

    store = KnowledgeStore(tmp_path / "kb.db")
    _seed(store)
    visible = sorted(c.content for c in store.list_chunks(limit=500))
    begin_forget(store, "s1", ["a2a:s1"])

    res = sweep_orphaned_forgets(store, retirement_incomplete=lambda sid, at: True)

    assert res == {"finished": 0, "restored": 1}
    assert sorted(c.content for c in store.list_chunks(limit=500)) == visible
    assert len(_contents(store)) == 3 and store.forget_pending_markers() == {}


def test_sweep_skips_in_flight_and_undecidable_forgets(tmp_path):
    from graph import conversation_harvest as ch

    store = KnowledgeStore(tmp_path / "kb.db")
    _seed(store)
    ch.begin_forget(store, "s1", ["a2a:s1"])

    ch.FORGETS_IN_FLIGHT.add("s1")
    try:
        assert ch.sweep_orphaned_forgets(store, retirement_incomplete=lambda s, a: False) == {"finished": 0, "restored": 0}
    finally:
        ch.FORGETS_IN_FLIGHT.discard("s1")

    def _cannot_tell(sid, at):
        raise RuntimeError("no checkpoint store")

    assert ch.sweep_orphaned_forgets(store, retirement_incomplete=_cannot_tell) == {"finished": 0, "restored": 0}
    assert list(store.forget_pending_markers()) == ["forget_pending:s1"]


def test_retirement_incomplete_reads_checkpoint_age(tmp_path, monkeypatch):
    """The live predicate: a checkpoint written BEFORE the forget began means retirement
    never completed; none — or only a newer one (a kept tab reusing the id) — means it did."""
    import sqlite3 as _sq
    import uuid
    from datetime import datetime, timezone

    import runtime.state as rs
    from server.maintenance_loops import _forget_retirement_incomplete

    db = tmp_path / "ck.db"
    conn = _sq.connect(db)
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
    conn.commit()
    monkeypatch.setattr(rs.STATE, "checkpoint_path", str(db), raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)

    def _uuid6(ts: float) -> str:
        # uuid.uuid6-style: 60-bit gregorian timestamp first.
        g = int((ts + 12219292800) * 1e7)
        hex_ = f"{g >> 12:012x}6{g & 0xFFF:03x}"
        return f"{hex_[:8]}-{hex_[8:12]}-{hex_[12:16]}-8000-{uuid.uuid4().hex[:12]}"

    marked = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert _forget_retirement_incomplete("s1", marked.isoformat()) is False  # no checkpoints
    conn.execute("INSERT INTO checkpoints VALUES ('a2a:s1', ?)", (_uuid6(marked.timestamp() + 60),))
    conn.commit()
    assert _forget_retirement_incomplete("s1", marked.isoformat()) is False  # only newer: retired, reused
    conn.execute("INSERT INTO checkpoints VALUES ('a2a:s1', ?)", (_uuid6(marked.timestamp() - 60),))
    conn.commit()
    assert _forget_retirement_incomplete("s1", marked.isoformat()) is True  # older: never retired
    conn.close()


def test_restore_is_one_transaction(tmp_path):
    """CodeRabbit (store): the restore's two UPDATEs ran on separate connections; if the
    second failed, the first had already un-hidden rows while the marker survived — a
    later forget resumed it without hiding them again, so they were never forgotten."""
    import pytest

    from graph.conversation_harvest import begin_forget

    store = KnowledgeStore(tmp_path / "kb.db")
    _seed(store)
    marker = begin_forget(store, "s1", ["a2a:s1"])
    held = store.count_forget_pending(marker)
    conn = sqlite3.connect(tmp_path / "kb.db")
    conn.execute(
        "CREATE TRIGGER fail_prev BEFORE UPDATE OF invalidation_reason ON chunks "
        "WHEN instr(old.invalidation_reason, '|prev:') > 0 BEGIN SELECT RAISE(ABORT, 'database is locked'); END"
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        store.restore_forget_pending(marker)

    assert store.count_forget_pending(marker) == held  # nothing half-restored
    assert store.list_chunks(limit=500) == []  # the hidden rows are still hidden


def test_settle_failed_retirement_keeps_the_marker_when_it_cannot_tell(tmp_path):
    from graph.conversation_harvest import begin_forget, settle_failed_retirement

    store = KnowledgeStore(tmp_path / "kb.db")
    _seed(store)
    marker = begin_forget(store, "s1", ["a2a:s1"])

    assert settle_failed_retirement(store, marker, thread_ids=["a2a:s1"]) == "kept"  # no checkpoint store
    assert store.forget_pending_markers()
