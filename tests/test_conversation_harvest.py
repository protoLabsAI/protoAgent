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
        AIMessage(content="<scratch_pad>add them</scratch_pad>It's 4."),
        AIMessage(content="   "),  # empty → skipped
    ]
    t = render_transcript(msgs)
    assert "User: what is 2+2?" in t
    assert "Assistant: It's 4." in t  # leaked scratch_pad dropped, native answer kept
    assert "scratch_pad" not in t


class _FakeKnowledge:
    def __init__(self):
        self.chunks = []

    def add_chunk(self, content, domain=None, heading=None, *, namespace=None, **kw):
        self.chunks.append({"content": content, "domain": domain, "heading": heading, "namespace": namespace})
        return f"chunk-{len(self.chunks)}"


def _seed(db, thread="a2a:chat-1"):
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="<output>noted</output>")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke(
            {"messages": [HumanMessage(content="my favorite color is teal")]}, {"configurable": {"thread_id": thread}}
        )

    asyncio.run(main())


def test_harvest_thread_summarizes_into_knowledge(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def fake_summarizer(transcript, config):
        assert "teal" in transcript  # got the real conversation
        return "User prefers teal."

    cid = asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=kb,
            config=object(),
            summarizer=fake_summarizer,
        )
    )
    assert cid == "chunk-1"
    assert kb.chunks[0]["domain"] == "conversation"
    assert "teal" in kb.chunks[0]["content"]


def test_harvest_extracts_facts_when_enabled(tmp_path):
    """ADR 0021: the session-end pass also distils facts (gated on
    knowledge_facts), stamped with the namespace, into a real store."""
    from types import SimpleNamespace

    from knowledge.store import KnowledgeStore

    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    store = KnowledgeStore(tmp_path / "kb.db")
    cfg = SimpleNamespace(knowledge_facts=True)

    async def fake_summarizer(transcript, config):
        return "User prefers teal."

    async def fake_facts(transcript, config):
        return ["The user's favorite color is teal"]

    cid = asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=store,
            config=cfg,
            summarizer=fake_summarizer,
            namespace="proj-x",
            fact_extractor=fake_facts,
        )
    )
    assert cid is not None
    # Episodic summary (conversation) + semantic fact (fact), both namespaced.
    assert len(store.list_chunks(domain="conversation", namespace="proj-x")) == 1
    facts = store.list_chunks(domain="fact", namespace="proj-x")
    assert len(facts) == 1 and "teal" in facts[0].content


def test_harvest_skips_facts_when_disabled(tmp_path):
    from types import SimpleNamespace

    from knowledge.store import KnowledgeStore

    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    store = KnowledgeStore(tmp_path / "kb.db")

    async def fake_summarizer(transcript, config):
        return "summary"

    async def boom_facts(transcript, config):
        raise AssertionError("fact extractor must not run when disabled")

    asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=store,
            config=SimpleNamespace(knowledge_facts=False),
            summarizer=fake_summarizer,
            fact_extractor=boom_facts,
        )
    )
    assert store.list_chunks(domain="fact") == []


def test_harvest_noop_without_knowledge_store(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    assert asyncio.run(harvest_thread("a2a:chat-1", checkpointer=saver, knowledge_store=None, config=object())) is None


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
    conn.commit()
    conn.close()

    aged = find_aged_threads(db, max_age_seconds=86400)
    assert aged == ["stale"]
    assert delete_thread(db, "stale") == 1
    assert find_aged_threads(db, max_age_seconds=86400) == []
