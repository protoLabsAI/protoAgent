"""The chat-delete forget covers the ADR 0125 ``history`` table in the SAME transaction
as the ``chunks`` facts (#4556 H1b).

``history`` shares the knowledge store's ``agent.db``, so one connection spans both tables:
begin hides the chat's archives/summaries (session, thread and ``:goal-iter-N``), abort
un-hides them, finish hard-deletes them, the orphan sweep settles a crash between the
phases, and a failure mid-transaction rolls BOTH tables back. Background-job reports
(``kind='report'``) stay out of reach, exactly as ``source_type='background_report'`` chunks
do. See :mod:`tests.test_forget_conversation_memory` for the chunks-only contract."""

from __future__ import annotations

import sqlite3

import pytest

from graph.conversation_harvest import (
    abort_forget,
    begin_forget,
    finish_forget,
    forget_conversation_memory,
    sweep_orphaned_forgets,
)
from knowledge.history import HistoryStore
from knowledge.store import KnowledgeStore

SESSION = "s1"
THREADS = ["a2a:s1", "chat:s1", "s1"]


@pytest.fixture(autouse=True)
def _no_db_env(monkeypatch):
    # _resolve_path honours KNOWLEDGE_DB_PATH over the explicit arg; drop it so both
    # stores land on the one tmp agent.db and tests stay isolated from each other.
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)


def _stores(tmp_path):
    """A KnowledgeStore and a HistoryStore on ONE agent.db — the production arrangement."""
    db = tmp_path / "agent.db"
    return KnowledgeStore(db), HistoryStore(db), db


def _seed_history(hist: HistoryStore) -> dict[str, int]:
    """Five rows: three the forget of session ``s1`` must hide (by session, by thread, by a
    ``:goal-iter-N`` thread), plus a background report and another chat's archive it must
    keep. The thread/goal rows carry a DIFFERENT session_id so they can only match via the
    thread arms."""
    return {
        "archive": hist.add("archive of s1", session_id=SESSION, thread_id="a2a:s1", kind="archive"),
        "summary": hist.add("summary of a thread", session_id="other-sess", thread_id="a2a:s1", kind="summary"),
        "goal": hist.add("goal-iteration summary", session_id="other-sess", thread_id="a2a:s1:goal-iter-2", kind="summary"),
        "report": hist.add("background report", session_id=SESSION, thread_id="a2a:s1", kind="report"),
        "other": hist.add("another chat's archive", session_id="s2", thread_id="a2a:s2", kind="archive"),
    }


def _row_count(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])
    finally:
        conn.close()


def test_begin_hides_history_rows_session_thread_and_goal_iter(tmp_path):
    store, hist, _ = _stores(tmp_path)
    ids = _seed_history(hist)
    # A chunk under the same chat proves both tables move under the one marker.
    store.add_chunk("archive chunk", domain="conversation", namespace="chat-archive:s1", source="a2a:s1")

    marker = begin_forget(store, SESSION, THREADS)
    assert marker == "forget_pending:s1"

    # The three the forget owns are now invisible to every reader (search/list/get all
    # filter invalidated_at); the report and the other chat survive.
    assert hist.get(ids["archive"]) is None
    assert hist.get(ids["summary"]) is None
    assert hist.get(ids["goal"]) is None
    assert hist.get(ids["report"]) is not None and hist.get(ids["other"]) is not None
    assert [e.content for e in hist.list_for_session(SESSION)] == ["background report"]
    assert hist.list_for_session("other-sess") == []
    assert [e.content for e in hist.list_for_session("s2")] == ["another chat's archive"]
    # Hidden, not deleted: every row is still on disk during the pending forget.
    assert _row_count(tmp_path / "agent.db") == 5
    # The chunk rode the same transaction.
    assert [c.content for c in store.list_chunks(limit=50)] == []


def test_abort_restores_history_rows(tmp_path):
    store, hist, _ = _stores(tmp_path)
    ids = _seed_history(hist)

    marker = begin_forget(store, SESSION, THREADS)
    restored = abort_forget(store, marker)

    assert restored == 3  # the three hidden history rows came back
    for key in ("archive", "summary", "goal", "report", "other"):
        assert hist.get(ids[key]) is not None
    assert store.forget_pending_markers() == {}


def test_finish_deletes_history_rows(tmp_path):
    store, hist, db = _stores(tmp_path)
    ids = _seed_history(hist)

    marker = begin_forget(store, SESSION, THREADS)
    removed = finish_forget(store, marker)

    assert removed == 3  # archive + summary + goal-iter, hard-deleted
    assert _row_count(db) == 2  # only the report and the other chat remain
    assert hist.get(ids["report"]) is not None and hist.get(ids["other"]) is not None
    assert store.forget_pending_markers() == {}


def test_sweep_finishes_a_history_only_orphan(tmp_path):
    """A crash after retirement leaves rows hidden under a marker no running delete owns.
    forget_pending_markers finds the marker in ``history`` (there are no chunks here at
    all), and the sweep finishes the delete."""
    store, hist, db = _stores(tmp_path)
    ids = _seed_history(hist)

    begin_forget(store, SESSION, THREADS)
    assert list(store.forget_pending_markers()) == ["forget_pending:s1"]
    seen: list = []

    res = sweep_orphaned_forgets(store, retirement_incomplete=lambda sid, at: seen.append((sid, at)) or False)

    assert res == {"finished": 1, "restored": 0}
    assert seen and seen[0][0] == SESSION and seen[0][1]  # the history-derived marker stamp
    assert _row_count(db) == 2  # the forgotten three are gone
    assert hist.get(ids["report"]) is not None
    assert store.forget_pending_markers() == {}


def test_sweep_restores_a_history_only_orphan(tmp_path):
    store, hist, db = _stores(tmp_path)
    ids = _seed_history(hist)

    begin_forget(store, SESSION, THREADS)
    res = sweep_orphaned_forgets(store, retirement_incomplete=lambda sid, at: True)

    assert res == {"finished": 0, "restored": 1}
    assert _row_count(db) == 5
    for key in ("archive", "summary", "goal"):
        assert hist.get(ids[key]) is not None
    assert store.forget_pending_markers() == {}


def test_mid_transaction_failure_leaves_both_tables_untouched(tmp_path):
    """One connection spans chunks and history: if hiding the history rows fails, the whole
    transaction rolls back and the chunk hide goes with it — neither table is changed."""
    store, hist, db = _stores(tmp_path)
    ids = _seed_history(hist)
    store.add_chunk("archive chunk", domain="conversation", namespace="chat-archive:s1", source="a2a:s1")

    # Make the history hide (phase 1) blow up AFTER the chunk hide has already run.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TRIGGER fail_history BEFORE UPDATE OF invalidated_at ON history "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        begin_forget(store, SESSION, THREADS)

    # The chunk was NOT left hidden, and every history row is still visible.
    assert [c.content for c in store.list_chunks(limit=50)] == ["archive chunk"]
    for key in ids:
        assert hist.get(ids[key]) is not None
    assert store.forget_pending_markers() == {}


def test_one_step_forget_also_deletes_history(tmp_path):
    """The fallback path (a store that can't hide rows two-phase) deletes the chat's
    history too — archives/summaries go, the report stays."""
    store, hist, db = _stores(tmp_path)
    ids = _seed_history(hist)

    removed = forget_conversation_memory(store, SESSION, THREADS)

    assert removed == 3  # three history rows (no chunks seeded)
    assert _row_count(db) == 2
    assert hist.get(ids["report"]) is not None and hist.get(ids["other"]) is not None
