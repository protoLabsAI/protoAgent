"""Tests for harvesting retired conversations into the knowledge base."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.checkpoint_prune import delete_thread, find_aged_threads
from graph.checkpointer import build_sqlite_checkpointer
from graph.conversation_harvest import harvest_thread, render_transcript


def test_render_transcript_cleans_and_skips_noise():
    msgs = [
        HumanMessage(content="what is 2+2?"),
        AIMessage(content="<scratch_pad>add them</scratch_pad><output>It's 4.</output>"),
        AIMessage(content="   "),  # empty → skipped
    ]
    t = render_transcript(msgs)
    assert "User: what is 2+2?" in t
    assert "Assistant: It's 4." in t       # extracted from <output>, scratch dropped
    assert "scratch_pad" not in t


class _FakeKnowledge:
    def __init__(self):
        self.chunks = []

    def add_chunk(self, content, domain=None, heading=None):
        self.chunks.append({"content": content, "domain": domain, "heading": heading})
        return f"chunk-{len(self.chunks)}"


def _seed(db, thread="a2a:chat-1"):
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="<output>noted</output>")]})
    g.add_edge(START, "n"); g.add_edge("n", END)

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke({"messages": [HumanMessage(content="my favorite color is teal")]},
                          {"configurable": {"thread_id": thread}})
    asyncio.run(main())


def test_harvest_thread_summarizes_into_knowledge(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def fake_summarizer(transcript, config):
        assert "teal" in transcript  # got the real conversation
        return "User prefers teal."

    cid = asyncio.run(harvest_thread(
        "a2a:chat-1", checkpointer=saver, knowledge_store=kb, config=object(),
        summarizer=fake_summarizer,
    ))
    assert cid == "chunk-1"
    assert kb.chunks[0]["domain"] == "conversation"
    assert "teal" in kb.chunks[0]["content"]


def test_harvest_noop_without_knowledge_store(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    assert asyncio.run(
        harvest_thread("a2a:chat-1", checkpointer=saver, knowledge_store=None, config=object())
    ) is None


def test_harvest_noop_on_unknown_thread(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def _boom(transcript, config):
        raise AssertionError("should not summarize an empty/unknown thread")

    out = asyncio.run(
        harvest_thread("a2a:nope", checkpointer=saver, knowledge_store=kb, config=object(), summarizer=_boom)
    )
    assert out is None and kb.chunks == []


def test_find_aged_threads_and_delete(tmp_path):
    import sqlite3
    db = str(tmp_path / "c.db")
    _seed(db, thread="recent")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("stale", "", "1dc8b9f0-0000-6000-8000-000000000000", None, "", b"{}", b"{}"),
    )
    conn.commit(); conn.close()

    aged = find_aged_threads(db, max_age_seconds=86400)
    assert aged == ["stale"]
    assert delete_thread(db, "stale") == 1
    assert find_aged_threads(db, max_age_seconds=86400) == []
