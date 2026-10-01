"""Tests for the checkpoint pruner (per-thread cap + age TTL)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.checkpoint_prune import delete_thread, prune_checkpoints, reclaim, uuidv6_unix_seconds
from graph.checkpointer import build_sqlite_checkpointer


def _graph(saver):
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="ok")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile(checkpointer=saver)


def _count(db, table, thread_id=None):
    conn = sqlite3.connect(db)
    try:
        if thread_id:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (thread_id,)).fetchone()[0]
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _seed(db, threads=("A", "B"), turns=3):
    async def main():
        app = _graph(build_sqlite_checkpointer(db))
        for t in threads:
            for i in range(turns):
                await app.ainvoke({"messages": [HumanMessage(content=f"{t}{i}")]}, {"configurable": {"thread_id": t}})

    asyncio.run(main())


def test_uuidv6_timestamp_decode_is_sane(tmp_path):
    # A real checkpoint id (LangGraph generates v6) should decode to ~now.
    db = str(tmp_path / "c.db")
    _seed(db, threads=("A",), turns=1)
    conn = sqlite3.connect(db)
    cid = conn.execute("SELECT checkpoint_id FROM checkpoints LIMIT 1").fetchone()[0]
    conn.close()
    ts = uuidv6_unix_seconds(cid)
    assert ts is not None and abs(ts - time.time()) < 30
    assert uuidv6_unix_seconds("not-a-uuid") is None


def test_per_thread_cap_keeps_latest(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db, threads=("A", "B"), turns=3)  # ~9 checkpoints/thread
    before = _count(db, "checkpoints", "A")
    assert before > 2
    res = prune_checkpoints(db, keep_per_thread=2, max_age_seconds=None)
    assert _count(db, "checkpoints", "A") == 2
    assert _count(db, "checkpoints", "B") == 2
    assert res["checkpoints_deleted"] == (before - 2) * 2  # both threads trimmed


def test_pruned_thread_can_still_resume(tmp_path):
    """Keeping the latest checkpoint must preserve resume — history continues."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("A",), turns=3)
    prune_checkpoints(db, keep_per_thread=1, max_age_seconds=None)

    async def resume_len():
        app = _graph(build_sqlite_checkpointer(db))
        cfg = {"configurable": {"thread_id": "A"}}
        before = await app.aget_state(cfg)
        await app.ainvoke({"messages": [HumanMessage(content="more")]}, cfg)
        after = await app.aget_state(cfg)
        return len(before.values["messages"]), len(after.values["messages"])

    b, a = asyncio.run(resume_len())
    assert b >= 1 and a > b  # state survived the prune and kept accumulating


def test_age_ttl_drops_old_threads_only(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db, threads=("recent",), turns=2)
    # Forge an "old" thread by inserting a checkpoint with a year-2000 v6 id.
    conn = sqlite3.connect(db)
    old_id = "1dc8b9f0-0000-6000-8000-000000000000"  # ~2000-era v6 timestamp
    assert uuidv6_unix_seconds(old_id) is not None
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("stale", "", old_id, None, "", b"{}", b"{}"),
    )
    conn.commit()
    conn.close()

    res = prune_checkpoints(db, keep_per_thread=50, max_age_seconds=86400)  # 1-day TTL
    assert res["threads_deleted"] == 1
    assert _count(db, "checkpoints", "stale") == 0  # old thread gone
    assert _count(db, "checkpoints", "recent") > 0  # recent thread kept


def test_background_keep_tighter_cap_for_background_threads(tmp_path):
    """a2a:background:* threads use background_keep instead of keep_per_thread."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("chat:user", "a2a:background:research"), turns=3)
    before_chat = _count(db, "checkpoints", "chat:user")
    before_bg = _count(db, "checkpoints", "a2a:background:research")
    assert before_chat > 2 and before_bg > 1

    res = prune_checkpoints(db, keep_per_thread=2, background_keep=1)
    assert _count(db, "checkpoints", "chat:user") == 2  # normal cap
    assert _count(db, "checkpoints", "a2a:background:research") == 1  # tighter cap
    assert res["checkpoints_deleted"] > 0


def test_background_keep_none_falls_back_to_keep_per_thread(tmp_path):
    """When background_keep is None, background threads use keep_per_thread."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("a2a:background:task",), turns=3)
    before = _count(db, "checkpoints", "a2a:background:task")
    assert before > 2

    prune_checkpoints(db, keep_per_thread=2, background_keep=None)
    assert _count(db, "checkpoints", "a2a:background:task") == 2  # same as keep_per_thread


def test_delete_thread_exact_only_without_cascade(tmp_path):
    """delete_thread(cascade=False) removes only the named thread, not sub-threads."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("a2a:X",), turns=2)
    # Insert synthetic goal-iter sub-thread rows for the same session
    conn = sqlite3.connect(db)
    for sub in ("a2a:X:goal-iter-1", "a2a:X:goal-iter-2"):
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (sub, "", f"00000000-0000-6000-8000-00000000000{sub[-1]}", None, "", b"{}", b"{}"),
        )
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sub, "", f"00000000-0000-6000-8000-00000000000{sub[-1]}", "", 0, "", "", b""),
        )
    conn.commit()
    conn.close()

    assert _count(db, "checkpoints", "a2a:X:goal-iter-1") == 1
    assert _count(db, "checkpoints", "a2a:X:goal-iter-2") == 1

    n = delete_thread(db, "a2a:X", cascade=False)
    assert n > 0
    assert _count(db, "checkpoints", "a2a:X") == 0
    assert _count(db, "checkpoints", "a2a:X:goal-iter-1") == 1  # untouched
    assert _count(db, "checkpoints", "a2a:X:goal-iter-2") == 1  # untouched
    assert _count(db, "writes", "a2a:X") == 0
    assert _count(db, "writes", "a2a:X:goal-iter-1") == 1
    assert _count(db, "writes", "a2a:X:goal-iter-2") == 1


def test_delete_thread_cascade_removes_subthreads(tmp_path):
    """delete_thread(cascade=True) removes the thread AND its :goal-iter-N sub-threads."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("a2a:X",), turns=2)
    # Insert synthetic goal-iter sub-thread + unrelated thread rows
    conn = sqlite3.connect(db)
    for sub in ("a2a:X:goal-iter-1", "a2a:X:goal-iter-2", "a2a:Y"):
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (sub, "", f"00000000-0000-6000-8000-00000000000{sub[-1]}", None, "", b"{}", b"{}"),
        )
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sub, "", f"00000000-0000-6000-8000-00000000000{sub[-1]}", "", 0, "", "", b""),
        )
    conn.commit()
    conn.close()

    assert _count(db, "checkpoints", "a2a:X:goal-iter-1") == 1
    assert _count(db, "checkpoints", "a2a:X:goal-iter-2") == 1
    assert _count(db, "checkpoints", "a2a:Y") == 1

    n = delete_thread(db, "a2a:X", cascade=True)
    assert n > 0
    # Parent + sub-threads gone
    assert _count(db, "checkpoints", "a2a:X") == 0
    assert _count(db, "checkpoints", "a2a:X:goal-iter-1") == 0
    assert _count(db, "checkpoints", "a2a:X:goal-iter-2") == 0
    assert _count(db, "writes", "a2a:X") == 0
    assert _count(db, "writes", "a2a:X:goal-iter-1") == 0
    assert _count(db, "writes", "a2a:X:goal-iter-2") == 0
    # Unrelated thread untouched
    assert _count(db, "checkpoints", "a2a:Y") == 1
    assert _count(db, "writes", "a2a:Y") == 1


# ── reclaim() tests ─────────────────────────────────────────────────────────


def test_reclaim_truncates_wal_and_frees_pages(tmp_path):
    """After deleting rows, reclaim truncates the WAL and reduces page_count."""
    db = str(tmp_path / "c.db")
    # Build a DB with auto_vacuum=INCREMENTAL (set before any table) and WAL.
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data BLOB)")
    conn.executemany("INSERT INTO t (data) VALUES (?)", [(b"x" * 4096,) for _ in range(30)])
    conn.commit()
    # Delete most rows so auto_vacuum tracks the freed pages on the freelist.
    conn.execute("DELETE FROM t WHERE id > 5")
    conn.commit()
    # Close first: a TRUNCATE checkpoint can only complete (busy == 0) when no
    # other connection holds the WAL back — which mirrors the restart-time reclaim.
    conn.close()

    res = reclaim(db)
    assert res["wal_truncated"] == 1
    assert res["pages_reclaimed"] > 0
    # TRUNCATE shrinks the -wal to zero bytes; it does NOT delete the file.
    wal = Path(db + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_reclaim_noop_on_fresh_db(tmp_path):
    """On a fresh DB with no deletions, reclaim changes nothing."""
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO t (data) VALUES ('hello')")
    conn.commit()
    conn.close()

    res = reclaim(db)
    assert res["pages_reclaimed"] == 0
    # wal_truncated may be 0 or 1 depending on whether WAL was checkpointed on
    # close — either is fine for a no-op test; we only care that nothing broke.


def test_reclaim_never_raises_on_bad_path():
    """reclaim must catch any error and return zero counts — never raise."""
    res = reclaim("/nonexistent_dir_xyz_12345/test.db")
    assert res == {"wal_truncated": 0, "pages_reclaimed": 0}


def _legacy_db_with_free_pages(tmp_path) -> str:
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db)
    # Deliberately do NOT set auto_vacuum — defaults to NONE.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data BLOB)")
    conn.executemany("INSERT INTO t (data) VALUES (?)", [(b"y" * 4096,) for _ in range(30)])
    conn.commit()
    conn.execute("DELETE FROM t WHERE id > 5")
    conn.commit()
    conn.close()
    return db


def _page_count(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()


def test_reclaim_skips_full_vacuum_on_a_legacy_db_by_default(tmp_path, monkeypatch):
    """#3973: on a legacy DB (auto_vacuum=NONE) the periodic reclaim must NOT run a full
    VACUUM — it rewrites the file under an exclusive lock, stalling live checkpoint
    writes. The freed pages stay on the freelist (reused), the file just doesn't shrink."""
    db = _legacy_db_with_free_pages(tmp_path)
    before = _page_count(db)
    statements: list[str] = []
    real_connect = sqlite3.connect

    def _traced(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _traced)

    res = reclaim(db)

    assert not [s for s in statements if s.strip().upper() == "VACUUM"], statements
    assert res["pages_reclaimed"] == 0
    assert _page_count(db) == before


def test_reclaim_full_vacuum_is_opt_in_and_migrates_to_incremental(tmp_path):
    """An offline / maintenance caller can opt in to the full VACUUM — and it MIGRATES the
    DB to auto_vacuum=INCREMENTAL (#3973), so every later reclaim shrinks it cheaply. A
    bare VACUUM shrank it once and left it NONE, never to shrink again."""
    db = _legacy_db_with_free_pages(tmp_path)
    res = reclaim(db, full_vacuum=True)
    assert res["pages_reclaimed"] > 0, "full VACUUM should reduce page_count after deletions on auto_vacuum=NONE"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
        conn.execute("DELETE FROM t WHERE id > 1")
        conn.commit()
    finally:
        conn.close()
    assert reclaim(db)["pages_reclaimed"] > 0  # the periodic (non-opt-in) reclaim now shrinks it


def test_reclaim_legacy_skip_hint_names_the_migrating_command(tmp_path, caplog):
    """The skip's hint must name the command that actually fixes it — the mode switch AND
    the VACUUM — not a bare VACUUM that leaves the DB unable to shrink (#3973)."""
    import logging

    db = _legacy_db_with_free_pages(tmp_path)
    with caplog.at_level(logging.INFO, logger="protoagent.checkpoint_prune"):
        reclaim(db)
    assert "PRAGMA auto_vacuum=INCREMENTAL; VACUUM;" in caplog.text


def test_vacuum_setting_help_does_not_promise_a_full_vacuum():
    from graph.settings_schema import FIELDS

    (field,) = [f for f in FIELDS if f.attr == "checkpoint_vacuum"]
    assert "PRAGMA auto_vacuum=INCREMENTAL; VACUUM;" in field.description


def _insert_cp(conn, thread_id, checkpoint_id, ns=""):
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        (thread_id, ns, checkpoint_id, None, "", b"{}", b"{}"),
    )
    conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, ns, checkpoint_id, "", 0, "", "", b""),
    )


def test_delete_thread_cascade_treats_like_wildcards_in_the_id_literally(tmp_path):
    """#3973: ``_`` / ``%`` in the thread id are literal in the cascade's LIKE — deleting
    ``a2a:s_1`` must not take ``a2a:sX1:goal-iter-*`` (or anything ``%`` would match)."""
    db = str(tmp_path / "c.db")
    _seed(db, threads=("seed",), turns=1)
    conn = sqlite3.connect(db)
    tids = (
        "a2a:s_1",
        "a2a:s_1:goal-iter-1",
        "a2a:sX1:goal-iter-1",
        "a2a:p%",
        "a2a:p%:goal-iter-1",
        "a2a:pZZ:goal-iter-1",
        # LIKE folds ASCII case: deleting chat-1 must not take CHAT-1's iterations.
        "chat-1",
        "chat-1:goal-iter-2",
        "CHAT-1:goal-iter-3",
        # A backslash is a literal too (the old escape char).
        "a2a:b\\x",
        "a2a:b\\x:goal-iter-1",
        "a2a:b\\\\x:goal-iter-1",
        "a2a:bx:goal-iter-1",
    )
    for i, tid in enumerate(tids):
        _insert_cp(conn, tid, f"00000000-0000-6000-8000-{i:012x}")
    conn.commit()
    conn.close()

    for tid in ("a2a:s_1", "a2a:p%", "chat-1", "a2a:b\\x"):
        delete_thread(db, tid, cascade=True)

    for gone in (
        "a2a:s_1",
        "a2a:s_1:goal-iter-1",
        "a2a:p%",
        "a2a:p%:goal-iter-1",
        "chat-1",
        "chat-1:goal-iter-2",
        "a2a:b\\x",
        "a2a:b\\x:goal-iter-1",
    ):
        assert _count(db, "checkpoints", gone) == 0 and _count(db, "writes", gone) == 0, gone
    for kept in (
        "a2a:sX1:goal-iter-1",
        "a2a:pZZ:goal-iter-1",
        "CHAT-1:goal-iter-3",
        "a2a:b\\\\x:goal-iter-1",
        "a2a:bx:goal-iter-1",
    ):
        assert _count(db, "checkpoints", kept) == 1 and _count(db, "writes", kept) == 1, kept


def _uuid6(unix_s: float, seq: int) -> str:
    ticks = int(unix_s * 1e7) + 0x01B21DD213814000
    th, tm, tl = (ticks >> 28) & 0xFFFFFFFF, (ticks >> 12) & 0xFFFF, ticks & 0x0FFF
    return f"{th:08x}-{tm:04x}-6{tl:03x}-8000-{seq:012x}"


def _trace_statements(monkeypatch) -> list[str]:
    statements: list[str] = []
    real_connect = sqlite3.connect

    def _traced(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _traced)
    return statements


def test_aged_lookup_and_prune_are_single_queries_not_n_plus_1(tmp_path, monkeypatch):
    """#3973: ``find_aged_threads`` and ``prune_checkpoints`` used a query per thread (and
    per namespace). Each lookup is now ONE query, whatever the thread count — with the
    same result: old threads TTL'd, each (thread, ns) capped, background threads tighter."""
    from graph.checkpoint_prune import find_aged_threads

    now = time.time()
    db = str(tmp_path / "c.db")
    _seed(db, threads=("seed",), turns=1)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM checkpoints")
    conn.execute("DELETE FROM writes")
    old, fresh = now - 40 * 86400, now - 60
    for t in range(8):
        for i in range(4):
            _insert_cp(conn, f"old{t}", _uuid6(old + i, i))
            _insert_cp(conn, f"live{t}", _uuid6(fresh + i, i))
            _insert_cp(conn, f"live{t}", _uuid6(fresh + i, i), ns="sub")
            _insert_cp(conn, f"a2a:background:{t}", _uuid6(fresh + i, i))
    _insert_cp(conn, "undatable", "not-a-uuid")  # never TTL'd
    conn.commit()
    conn.close()

    statements = _trace_statements(monkeypatch)
    aged = find_aged_threads(db, 30 * 86400, now=now)
    assert sorted(aged) == [f"old{t}" for t in range(8)]
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1, statements

    statements.clear()
    res = prune_checkpoints(db, keep_per_thread=2, max_age_seconds=30 * 86400, now=now, background_keep=1)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2, selects  # one aged-thread scan + one windowed cap query
    assert res == {"threads_deleted": 8, "checkpoints_deleted": 8 * (2 + 2 + 3)}
    for t in range(8):
        assert _count(db, "checkpoints", f"old{t}") == 0
        assert _count(db, "checkpoints", f"live{t}") == 4  # 2 per namespace
        assert _count(db, "checkpoints", f"a2a:background:{t}") == 1
        assert _count(db, "writes", f"live{t}") == 4
    assert _count(db, "checkpoints", "undatable") == 1


def test_age_ttl_scan_and_delete_are_one_write_transaction(tmp_path, monkeypatch):
    """#3973: the TTL scan holds the write lock through its delete. A chat reopened while
    the scan runs (a new turn on a thread the scan has judged idle) must either be held
    off until the prune commits, or survive it — never be written and then deleted."""
    from graph import checkpoint_prune as cp

    now = time.time()
    db = str(tmp_path / "c.db")
    _seed(db, threads=("seed",), turns=1)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM checkpoints")
    conn.execute("DELETE FROM writes")
    for i in range(3):
        _insert_cp(conn, "old", _uuid6(now - 40 * 86400 + i, i))
    conn.commit()
    conn.close()

    new_turn = _uuid6(now, 99)
    outcome: list[str] = []
    real = cp.uuidv6_unix_seconds

    def _reopen_mid_scan(cid):
        if not outcome:  # the live saver writes a new turn while the scan runs
            live = sqlite3.connect(db, timeout=0)
            try:
                _insert_cp(live, "old", new_turn)
                live.commit()
                outcome.append("written")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                outcome.append("held off")
            finally:
                live.close()
        return real(cid)

    monkeypatch.setattr(cp, "uuidv6_unix_seconds", _reopen_mid_scan)
    prune_checkpoints(db, keep_per_thread=10, max_age_seconds=30 * 86400, now=now)

    conn = sqlite3.connect(db)
    try:
        survived = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE checkpoint_id=?", (new_turn,)).fetchone()[0]
    finally:
        conn.close()
    assert outcome == ["held off"] or survived == 1, (outcome, survived)
