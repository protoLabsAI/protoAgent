"""Tests for the checkpoint pruner (per-thread cap + age TTL)."""

from __future__ import annotations

import asyncio
import sqlite3
import time

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.checkpoint_prune import delete_thread, prune_checkpoints, uuidv6_unix_seconds
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
