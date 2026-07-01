"""Tests for on-demand conversation compaction (the /compact gesture, #1527)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from graph.checkpointer import build_sqlite_checkpointer
from graph.compaction_op import compact_thread


class _FakeGraph:
    """Records aupdate_state calls; serves seeded messages from aget_state."""

    def __init__(self, messages):
        self._messages = messages
        self.updates: list = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)})

    async def aupdate_state(self, config, update):
        self.updates.append((config, update))


class _FakeKnowledge:
    def __init__(self, *, yields=True):
        self.docs: list = []
        self._yields = yields

    def add_document(self, content, *, domain=None, heading=None, namespace=None, **kw):
        self.docs.append({"content": content, "domain": domain, "heading": heading, "namespace": namespace})
        return [1, 2] if self._yields else []


async def _summ(transcript, config):
    return "SUMMARY: user likes teal"


async def _boom(transcript, config):
    raise AssertionError("summarizer must not run on this path")


async def _raise_summ(transcript, config):
    raise RuntimeError("gateway down")


def _cfg(keep):
    return SimpleNamespace(compaction_keep_messages=keep)


def test_compact_archives_and_rewrites_checkpoint():
    msgs = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="hello", id="a1"),
        HumanMessage(content="favorite color?", id="h2"),
        AIMessage(content="teal", id="a2"),
    ]
    g = _FakeGraph(msgs)
    kb = _FakeKnowledge()
    res = asyncio.run(compact_thread(g, object(), kb, _cfg(2), "a2a:s1", "s1", summarizer=_summ))

    # Raw transcript archived into the conversation domain, session-scoped namespace.
    assert kb.docs[0]["domain"] == "conversation"
    assert kb.docs[0]["namespace"] == "chat-archive:s1"
    assert "teal" in kb.docs[0]["content"] and "favorite color" in kb.docs[0]["content"]

    assert res["summary"] == "SUMMARY: user likes teal"
    assert res["archived"] is True and res["refused"] is False
    assert res["removed"] == 2 and res["kept"] == 2 and res["archived_chunks"] == 2

    # Checkpoint rewritten to [REMOVE_ALL, summary, *keep_recent].
    assert len(g.updates) == 1
    _config, update = g.updates[0]
    out = update["messages"]
    assert isinstance(out[0], RemoveMessage) and out[0].id == REMOVE_ALL_MESSAGES
    assert isinstance(out[1], HumanMessage) and "SUMMARY: user likes teal" in out[1].content
    assert out[1].additional_kwargs.get("lc_source") == "compaction"
    assert [m.id for m in out[2:]] == ["h2", "a2"]  # the recent tail, untouched


def test_compact_preserves_tool_call_pairing():
    # Naive cutoff at keep=2 lands on the 2nd ToolMessage — the safe-cut must walk
    # back to the AIMessage that spawned it so the pair isn't orphaned.
    msgs = [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
        ToolMessage(content="res1", id="tm1", tool_call_id="t1"),
        AIMessage(content="answer1", id="a2"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="", id="a3", tool_calls=[{"id": "t2", "name": "search", "args": {}}]),
        ToolMessage(content="res2", id="tm2", tool_call_id="t2"),
        AIMessage(content="answer2", id="a4"),
    ]
    g = _FakeGraph(msgs)
    res = asyncio.run(compact_thread(g, object(), _FakeKnowledge(), _cfg(2), "a2a:s1", "s1", summarizer=_summ))

    _config, update = g.updates[0]
    tail = update["messages"][2:]
    # The tail starts on the AIMessage(tool_calls) — NOT the orphaned ToolMessage.
    assert [m.id for m in tail] == ["a3", "tm2", "a4"]
    assert isinstance(tail[0], AIMessage) and tail[0].tool_calls
    assert isinstance(tail[1], ToolMessage) and tail[1].tool_call_id == "t2"
    assert res["removed"] == 5 and res["kept"] == 3


def test_compact_refuses_without_store():
    # Never-lossy: no archive target ⇒ the checkpoint is left untouched.
    msgs = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(6)]
    g = _FakeGraph(msgs)
    res = asyncio.run(compact_thread(g, object(), None, _cfg(2), "a2a:s1", "s1", summarizer=_boom))
    assert res["refused"] is True and res["archived"] is False and res["reason"] == "no_store"
    assert g.updates == []  # NO rewrite


def test_compact_refuses_when_archive_yields_nothing():
    # Never-lossy: archive wrote no chunks ⇒ don't rewrite (and don't even summarize).
    msgs = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(6)]
    g = _FakeGraph(msgs)
    kb = _FakeKnowledge(yields=False)
    res = asyncio.run(compact_thread(g, object(), kb, _cfg(2), "a2a:s1", "s1", summarizer=_boom))
    assert res["refused"] is True and res["archived"] is False and res["reason"] == "empty_archive"
    assert g.updates == []


def test_compact_refuses_when_summarizer_raises():
    # Never-lossy: the archive already succeeded, but a summarizer exception must NOT
    # 500 or half-rewrite the checkpoint — refuse and leave the live context intact.
    msgs = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(6)]
    g = _FakeGraph(msgs)
    kb = _FakeKnowledge()
    res = asyncio.run(compact_thread(g, object(), kb, _cfg(2), "a2a:s1", "s1", summarizer=_raise_summ))
    assert res["refused"] is True and res["reason"] == "summary_error"
    assert res["archived"] is True and res["archived_chunks"] == 2  # the archive stands
    assert g.updates == []  # NO checkpoint rewrite


def test_compact_noop_when_already_short():
    msgs = [HumanMessage(content="hi", id="h1"), AIMessage(content="yo", id="a1")]
    g = _FakeGraph(msgs)
    kb = _FakeKnowledge()
    res = asyncio.run(compact_thread(g, object(), kb, _cfg(20), "a2a:s1", "s1", summarizer=_boom))
    assert res["refused"] is False and res["reason"] == "too_short" and res["removed"] == 0
    assert g.updates == [] and kb.docs == []  # nothing archived, nothing rewritten


def test_compact_refuses_without_checkpointer():
    res = asyncio.run(compact_thread(_FakeGraph([]), None, _FakeKnowledge(), _cfg(2), "a2a:s1", "s1", summarizer=_boom))
    assert res["refused"] is True and res["reason"] == "no_checkpointer"


def test_compact_rewrites_real_sqlite_checkpoint(tmp_path):
    """End-to-end against a real compiled graph + SQLite checkpointer: the rewrite
    actually lands (proves aupdate_state applies REMOVE_ALL + reducer, no as_node
    ambiguity) and the resulting checkpoint is [summary, *recent_tail]."""
    db = str(tmp_path / "c.db")
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": []})  # no-op — we seed via the input
    g.add_edge(START, "n")
    g.add_edge("n", END)
    saver = build_sqlite_checkpointer(db)
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "a2a:s1"}}
    seed = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
        ToolMessage(content="res1", tool_call_id="t1"),
        AIMessage(content="answer1"),
        HumanMessage(content="q2"),
        AIMessage(content="answer2"),
    ]
    kb = _FakeKnowledge()

    async def run():
        await app.ainvoke({"messages": seed}, cfg)
        res = await compact_thread(app, saver, kb, _cfg(2), "a2a:s1", "s1", summarizer=_summ)
        snap = await app.aget_state(cfg)
        return res, snap.values["messages"]

    res, final = asyncio.run(run())
    assert res["archived"] is True and res["removed"] == 4 and res["kept"] == 2
    # [summary(Human), q2, answer2] — everything before the recent tail collapsed.
    assert len(final) == 3
    assert "SUMMARY" in final[0].content and final[0].additional_kwargs.get("lc_source") == "compaction"
    assert final[1].content == "q2" and final[2].content == "answer2"


def test_compact_archive_is_searchable_in_a_real_store(tmp_path):
    """End-to-end 'searchable full-text save': the FULL raw transcript — including
    the head that compaction REMOVES from live context — is archived into a REAL
    KnowledgeStore (FTS5, no gateway) and retrievable by search. Only the
    summarizer is stubbed; the checkpoint rewrite + archive are real."""
    from knowledge.store import KnowledgeStore

    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": []})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    saver = build_sqlite_checkpointer(str(tmp_path / "c.db"))
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "a2a:s1"}}
    seed = [
        HumanMessage(content="my favorite bread is pumpernickel"),
        AIMessage(content="noted, pumpernickel"),
        HumanMessage(content="old filler two"),
        AIMessage(content="ok two"),
        HumanMessage(content="what is the capital of France"),
        AIMessage(content="Paris"),
    ]
    store = KnowledgeStore(db_path=str(tmp_path / "kb.db"))

    async def run():
        await app.ainvoke({"messages": seed}, cfg)
        res = await compact_thread(app, saver, store, _cfg(2), "a2a:s1", "s1", summarizer=_summ)
        snap = await app.aget_state(cfg)
        return res, snap.values["messages"]

    res, final = asyncio.run(run())

    # (1) Actually compacted: archived a chunk, removed the head, kept the tail.
    assert res["archived"] is True and res["refused"] is False
    assert res["archived_chunks"] >= 1 and res["removed"] == 4 and res["kept"] == 2
    assert res["summary"]

    # (2) Live context collapsed to [summary, capital-of-France, Paris] — head GONE.
    assert len(final) == 3
    assert all("pumpernickel" not in (m.content or "") for m in final)

    # (3) …but the removed head is preserved AND SEARCHABLE in the real store.
    hits = store.search("pumpernickel", domain="conversation")
    assert hits, "archived transcript is NOT searchable"
    blob = " ".join(str(h.get("content") or h.get("preview") or "") for h in hits)
    assert "pumpernickel" in blob
