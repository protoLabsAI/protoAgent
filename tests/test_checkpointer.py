"""Tests for the durable SQLite conversation checkpointer."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.checkpointer import build_sqlite_checkpointer


def _toy_graph(checkpointer):
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="ok")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile(checkpointer=checkpointer)


def test_threaded_sqlite_saver_persists_across_restart(tmp_path):
    """A new saver opened on the same DB file sees the prior session's history —
    this is the durability the in-memory saver lacked (history survives a
    process restart)."""
    db = str(tmp_path / "ckpt.db")
    cfg = {"configurable": {"thread_id": "chat-1"}}

    async def turn(text: str) -> int:
        saver = build_sqlite_checkpointer(db)  # fresh saver each call = "restart"
        app = _toy_graph(saver)
        await app.ainvoke({"messages": [HumanMessage(content=text)]}, cfg)
        state = await app.aget_state(cfg)
        return len(state.values["messages"])

    n1 = asyncio.run(turn("hello"))  # 1 human + 1 ai
    n2 = asyncio.run(turn("again"))  # reopened DB → accumulates on top
    assert n1 == 2
    assert n2 == 4  # history persisted across the simulated restart


def test_threaded_sqlite_saver_isolates_threads(tmp_path):
    """Different thread_ids (chat tabs) keep independent histories."""
    db = str(tmp_path / "ckpt.db")
    saver = build_sqlite_checkpointer(db)
    app = _toy_graph(saver)

    async def main():
        await app.ainvoke({"messages": [HumanMessage(content="tab A")]}, {"configurable": {"thread_id": "A"}})
        b = await app.aget_state({"configurable": {"thread_id": "B"}})
        return b

    state_b = asyncio.run(main())
    # Thread B was never written → empty (A's history doesn't bleed in).
    assert not state_b.values  # no checkpoint for B


def test_build_checkpointer_in_memory_when_path_blank():
    """server._build_checkpointer falls back to an in-memory saver when no path
    is configured (opt-out of durable history)."""
    import server
    from langgraph.checkpoint.memory import MemorySaver

    class _Cfg:
        checkpoint_db_path = ""

    assert isinstance(server._build_checkpointer(_Cfg()), MemorySaver)


@pytest.mark.asyncio
async def test_threaded_saver_async_methods_work(tmp_path):
    """The async methods (delegated to threads) are usable directly."""
    saver = build_sqlite_checkpointer(str(tmp_path / "c.db"))
    # No checkpoint yet for this thread → aget_tuple returns None, doesn't raise.
    assert await saver.aget_tuple({"configurable": {"thread_id": "none"}}) is None


# --- #1738: a checkpoint write that hits "database is locked" retries, not dies ---


def test_retry_on_locked_recovers_transient_lock(monkeypatch):
    import graph.checkpointer as ckpt

    monkeypatch.setattr(ckpt.time, "sleep", lambda _s: None)  # no real backoff in tests
    calls = {"n": 0}

    def flaky(x):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return x

    assert ckpt._retry_on_locked(flaky, "ok") == "ok"
    assert calls["n"] == 3  # two locked attempts, then success — the turn survives


def test_retry_on_locked_gives_up_after_bounded_retries(monkeypatch):
    import graph.checkpointer as ckpt

    monkeypatch.setattr(ckpt.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_locked(*_a):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        ckpt._retry_on_locked(always_locked)
    assert calls["n"] == ckpt._LOCK_WRITE_RETRIES  # bounded — no infinite retry loop


def test_retry_on_locked_does_not_retry_other_errors(monkeypatch):
    import graph.checkpointer as ckpt

    monkeypatch.setattr(ckpt.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def other(*_a):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: checkpoints")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        ckpt._retry_on_locked(other)
    assert calls["n"] == 1  # a non-lock error propagates immediately
