"""Tests for on-demand conversation rewind (the "Rewind to here" gesture, #1535)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from graph.checkpointer import build_sqlite_checkpointer
from graph.rewind_op import _open_tool_calls, _safe_cut_end, rewind_thread


class _FakeGraph:
    """Records aupdate_state calls; serves seeded messages from aget_state."""

    def __init__(self, messages):
        self._messages = messages
        self.updates: list = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)})

    async def aupdate_state(self, config, update):
        self.updates.append((config, update))


def _tool_thread():
    # A two-turn thread where each turn is [AI(tool_calls), Tool, AI(answer)].
    return [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
        ToolMessage(content="res1", id="tm1", tool_call_id="t1"),
        AIMessage(content="answer1", id="a2"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="", id="a3", tool_calls=[{"id": "t2", "name": "search", "args": {}}]),
        ToolMessage(content="res2", id="tm2", tool_call_id="t2"),
        AIMessage(content="answer2", id="a4"),
    ]


def test_rewind_truncates_by_index():
    msgs = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="hello", id="a1"),
        HumanMessage(content="favorite color?", id="h2"),
        AIMessage(content="teal", id="a2"),
    ]
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_index=1))
    assert res == {"found": True, "kept": 2, "removed": 2, "reason": ""}

    # Checkpoint rewritten to [REMOVE_ALL, *kept_prefix].
    assert len(g.updates) == 1
    _config, update = g.updates[0]
    out = update["messages"]
    assert isinstance(out[0], RemoveMessage) and out[0].id == REMOVE_ALL_MESSAGES
    assert [m.id for m in out[1:]] == ["h1", "a1"]  # everything after index 1 discarded


def test_rewind_by_message_id():
    msgs = _tool_thread()
    g = _FakeGraph(msgs)
    # Rewind to the first turn's final answer — keeps the whole first turn.
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="a2"))
    _config, update = g.updates[0]
    assert [m.id for m in update["messages"][1:]] == ["h1", "a1", "tm1", "a2"]
    assert res["removed"] == 4 and res["kept"] == 4


def test_rewind_by_content_matches_last_occurrence():
    # The console path: the client sends the visible bubble's text (its client-side
    # message id is NOT in the checkpoint). Last-occurrence disambiguates repeats.
    msgs = [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="done", id="a1"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="done", id="a2"),
        HumanMessage(content="q3", id="h3"),
        AIMessage(content="final", id="a3"),
    ]
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="client-xyz", target_content="done"))
    # "client-xyz" isn't a checkpoint id → falls through to content, last "done" = a2.
    _config, update = g.updates[0]
    assert [m.id for m in update["messages"][1:]] == ["h1", "a1", "h2", "a2"]
    assert res["removed"] == 2 and res["kept"] == 4


def test_rewind_by_content_honors_occurrence():
    # Duplicate replies: the client sends WHICH occurrence it clicked, so the server keeps
    # through the RIGHT "done" — not the last one (which would silently retain turns the user
    # meant to discard). Clicking the FIRST "done" (occurrence 0) keeps only [h1, a1].
    msgs = [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="done", id="a1"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="done", id="a2"),
        HumanMessage(content="q3", id="h3"),
        AIMessage(content="final", id="a3"),
    ]
    g = _FakeGraph(msgs)
    res = asyncio.run(
        rewind_thread(g, object(), "a2a:s1", target_content="done", occurrence=0)
    )
    _config, update = g.updates[0]
    assert [m.id for m in update["messages"][1:]] == ["h1", "a1"]  # the FIRST "done", not a2
    assert res["removed"] == 4 and res["kept"] == 2


def test_rewind_preserves_tool_call_pairing():
    # Rewind lands ON the AIMessage(tool_calls) — the safe-cut must extend FORWARD to
    # pull in its ToolMessage so the request/response pair isn't orphaned.
    msgs = _tool_thread()
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="a3"))
    _config, update = g.updates[0]
    kept = update["messages"][1:]
    # Kept through a3's ToolMessage (tm2) — a4 (answer2) discarded, tm2 NOT orphaned.
    assert [m.id for m in kept] == ["h1", "a1", "tm1", "a2", "h2", "a3", "tm2"]
    assert isinstance(kept[-1], ToolMessage) and kept[-1].tool_call_id == "t2"
    assert _open_tool_calls(kept) == set()  # no orphaned tool_call
    assert res["removed"] == 1 and res["kept"] == 7


def test_rewind_falls_back_before_toolcall_when_response_missing():
    # Malformed/partial turn: the AIMessage(tool_calls) has NO answering ToolMessage.
    # Forward can't balance, so safe-cut retreats to before the requesting AIMessage.
    msgs = [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
        ToolMessage(content="res1", id="tm1", tool_call_id="t1"),
        AIMessage(content="answer1", id="a2"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="", id="a3", tool_calls=[{"id": "t2", "name": "search", "args": {}}]),
        AIMessage(content="answer2", id="a4"),  # no ToolMessage for t2
    ]
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="a3"))
    _config, update = g.updates[0]
    kept = update["messages"][1:]
    assert [m.id for m in kept] == ["h1", "a1", "tm1", "a2", "h2"]  # a3 dropped, no orphan
    assert _open_tool_calls(kept) == set()
    assert res["removed"] == 2 and res["kept"] == 5


def test_rewind_noop_when_target_is_last():
    msgs = [HumanMessage(content="hi", id="h1"), AIMessage(content="yo", id="a1")]
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="a1"))
    assert res["found"] is True and res["removed"] == 0 and res["reason"] == "noop"
    assert g.updates == []  # nothing after the target — no rewrite


def test_rewind_not_found():
    msgs = [HumanMessage(content="hi", id="h1"), AIMessage(content="yo", id="a1")]
    g = _FakeGraph(msgs)
    res = asyncio.run(rewind_thread(g, object(), "a2a:s1", target_id="nope", target_content="nomatch"))
    assert res["found"] is False and res["reason"] == "not_found"
    assert g.updates == []


def test_rewind_refuses_without_checkpointer():
    res = asyncio.run(rewind_thread(_FakeGraph([]), None, "a2a:s1", target_index=0))
    assert res["found"] is False and res["reason"] == "no_checkpointer"


def test_safe_cut_end_keeps_balanced_ends():
    msgs = _tool_thread()
    assert _safe_cut_end(msgs, 0) == 0  # keep-nothing can't orphan
    assert _safe_cut_end(msgs, len(msgs)) == len(msgs)  # keep-all can't orphan
    assert _safe_cut_end(msgs, 4) == 4  # already a clean turn boundary


def test_rewind_rewrites_real_sqlite_checkpoint(tmp_path):
    """End-to-end against a real compiled graph + SQLite checkpointer: the rewrite
    actually lands (REMOVE_ALL + reducer, no as_node ambiguity) and the resulting
    checkpoint is the kept prefix, with tool pairing intact."""
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

    async def run():
        await app.ainvoke({"messages": seed}, cfg)
        snap0 = await app.aget_state(cfg)
        # Rewind to the first turn's final answer (content "answer1").
        res = await rewind_thread(app, saver, "a2a:s1", target_content="answer1")
        snap = await app.aget_state(cfg)
        return res, snap0.values["messages"], snap.values["messages"]

    res, before, final = asyncio.run(run())
    assert len(before) == 6  # sanity: the seed landed
    assert res["found"] is True and res["removed"] == 2 and res["kept"] == 4
    # Live context rolled back to [q1, AI(tool_calls), tool, answer1] — turn 2 gone.
    assert len(final) == 4
    assert final[-1].content == "answer1"
    assert all("answer2" not in (m.content or "") for m in final)
    assert _open_tool_calls(final) == set()  # tool pairing preserved
