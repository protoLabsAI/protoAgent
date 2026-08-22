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

    def add_chunk(self, content, domain=None, heading=None, *, source=None, source_type=None, namespace=None, **kw):
        self.chunks.append(
            {
                "content": content,
                "domain": domain,
                "heading": heading,
                "source": source,
                "source_type": source_type,
                "namespace": namespace,
            }
        )
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
    # Provenance (ADR 0069 D5): the row links back to the retired thread.
    assert kb.chunks[0]["source"] == "a2a:chat-1"
    # Trust tier (ADR 0069 D8): harvest rows rank agent-derived.
    assert kb.chunks[0]["source_type"] == "harvest"


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
    summaries = store.list_chunks(domain="conversation", namespace="proj-x")
    assert len(summaries) == 1
    facts = store.list_chunks(domain="fact", namespace="proj-x")
    assert len(facts) == 1 and "teal" in facts[0].content
    # Provenance (ADR 0069 D5): both rows carry the originating thread id.
    assert summaries[0].source == "a2a:chat-1"
    assert facts[0].source == "a2a:chat-1"


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


def test_harvest_skips_incognito_thread(tmp_path):
    """ADR 0069 D3b: incognito means NO memory trail — the retire sweep
    (harvest_enabled defaults ON) must not summarize the thread into the
    knowledge store, where RAG would re-inject it into later prompts."""
    from graph.state import ProtoAgentState

    db = str(tmp_path / "c.db")
    g = StateGraph(ProtoAgentState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="noted")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke(
            {"messages": [HumanMessage(content="my secret color is teal")], "incognito": True},
            {"configurable": {"thread_id": "a2a:incog"}},
        )

    asyncio.run(main())
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def _boom(transcript, config):
        raise AssertionError("incognito thread must not be summarized")

    out = asyncio.run(
        harvest_thread("a2a:incog", checkpointer=saver, knowledge_store=kb, config=object(), summarizer=_boom)
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


# ── #2946: a FAILED harvest must not cost the conversation its knowledge ────────


def _retire_env(monkeypatch, tmp_path, harvest_behavior):
    """Point server.agent_init.STATE at a minimal retire-able world and record
    deletions. ``harvest_behavior`` is the fake harvest_thread."""
    from types import SimpleNamespace

    import graph.conversation_harvest as ch
    import server.agent_init as ai
    from graph import checkpoint_prune

    monkeypatch.setattr(ai.STATE, "graph_config", SimpleNamespace(checkpoint_harvest_enabled=True), raising=False)
    monkeypatch.setattr(ai.STATE, "checkpointer", object(), raising=False)
    monkeypatch.setattr(ai.STATE, "knowledge_store", object(), raising=False)
    monkeypatch.setattr(ai.STATE, "checkpoint_path", str(tmp_path / "ck.db"), raising=False)
    monkeypatch.setattr(ch, "harvest_thread", harvest_behavior)
    deleted = []
    monkeypatch.setattr(checkpoint_prune, "delete_thread", lambda path, tid, cascade=True: deleted.append(tid))
    ai._HARVEST_FAILURES.clear()
    return ai, deleted


async def test_sweep_retire_keeps_the_thread_when_harvest_fails(monkeypatch, tmp_path):
    """The TTL sweep path (#2946): a transient harvest failure (the shared-account
    429 burst) keeps the thread for the next sweep instead of deleting it — the old
    flow deleted anyway, permanently skipping knowledge capture."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("429 burst")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    assert await ai._retire_thread("t-1") is None
    assert deleted == []  # kept for retry
    assert ai._HARVEST_FAILURES["t-1"] == 1


async def test_sweep_retire_deletes_after_the_failure_cap(monkeypatch, tmp_path):
    """A permanently-broken harvest can't pin checkpoints forever: at the cap the
    thread deletes anyway (loudly)."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("still broken")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    for _ in range(ai._HARVEST_FAILURE_CAP - 1):
        await ai._retire_thread("t-1")
    assert deleted == []
    await ai._retire_thread("t-1")  # cap reached → delete proceeds
    assert deleted == ["t-1"]
    assert "t-1" not in ai._HARVEST_FAILURES  # counter cleared with the thread


async def test_explicit_delete_still_deletes_when_harvest_fails(monkeypatch, tmp_path):
    """The delete-chat dialog path (explicit harvest bool): the operator asked for
    deletion — a failed harvest is logged loudly but must not block it."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("boom")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    await ai._retire_thread("t-x", harvest=True)
    assert deleted == ["t-x"]
    assert "t-x" not in ai._HARVEST_FAILURES


async def test_successful_harvest_clears_the_failure_counter(monkeypatch, tmp_path):
    """A success between failures resets the retry budget — the cap is for
    CONSECUTIVE failures, not lifetime ones."""
    calls = {"n": 0}

    async def _flaky(thread_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "chunk-1"

    ai, deleted = _retire_env(monkeypatch, tmp_path, _flaky)

    await ai._retire_thread("t-2")  # fails → kept
    assert deleted == [] and ai._HARVEST_FAILURES["t-2"] == 1
    assert await ai._retire_thread("t-2") == "chunk-1"  # succeeds → harvested + deleted
    assert deleted == ["t-2"]
    assert "t-2" not in ai._HARVEST_FAILURES


async def test_harvest_thread_raise_on_error_reraises(tmp_path):
    """The flag that lets the retire path tell FAILURE from legitimately-nothing:
    default swallows (None), raise_on_error re-raises."""
    import pytest as _pytest

    from graph.conversation_harvest import harvest_thread

    class _BoomCheckpointer:
        async def aget_tuple(self, cfg):
            raise RuntimeError("checkpointer exploded")

    kwargs = dict(
        checkpointer=_BoomCheckpointer(),
        knowledge_store=object(),
        config=object(),
    )
    assert await harvest_thread("t-e", **kwargs) is None  # default: swallowed
    with _pytest.raises(RuntimeError):
        await harvest_thread("t-e", raise_on_error=True, **kwargs)
