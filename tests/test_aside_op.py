"""Tests for graph.aside_op — the `/btw` side turn (#2180).

The load-bearing test is `test_aside_never_writes_the_main_thread`: the entire feature is
an isolation promise ("it can't change the main thread"), so we assert exactly that — the
side turn runs on a DIFFERENT thread id than the one it read, and never calls
`aupdate_state` on the main thread.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from graph.aside_op import run_aside


class _Snap:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    """Records which thread ids were read (aget_state) and written (aupdate_state), and
    which thread the turn actually ran on (ainvoke)."""

    def __init__(self, main_messages):
        self._main = list(main_messages)
        self.read_threads: list[str] = []
        self.written_threads: list[str] = []
        self.invoked_on: list[str] = []
        self.invoked_input = None

    async def aget_state(self, config):
        self.read_threads.append(config["configurable"]["thread_id"])
        return _Snap(self._main)

    async def aupdate_state(self, config, *_a, **_k):  # pragma: no cover - must never touch main
        self.written_threads.append(config["configurable"]["thread_id"])

    async def ainvoke(self, graph_input, config):
        self.invoked_on.append(config["configurable"]["thread_id"])
        self.invoked_input = graph_input
        # Echo an answer, mirroring a real turn's final state shape.
        return {"messages": [*graph_input["messages"], AIMessage(content="Here's the answer.")]}


MAIN = "a2a:s1"


def _run(graph, **kw):
    return asyncio.run(run_aside(graph, checkpointer=object(), thread_id=MAIN, question="what's X?", **kw))


def test_aside_answers_using_the_main_context():
    g = _FakeGraph([HumanMessage(content="we're discussing X"), AIMessage(content="X is a thing")])
    out = _run(g)
    assert out["found"] is True and out["reason"] == "ok"
    assert out["answer"] == "Here's the answer."
    # The side turn was SEEDED with the main thread's messages + the question.
    seeded = g.invoked_input["messages"]
    assert seeded[0].content == "we're discussing X"  # main context carried in
    assert seeded[-1].content == "what's X?"  # the aside question last


def test_aside_never_writes_the_main_thread():
    """THE guarantee: the main thread is read, never written, and the turn runs elsewhere."""
    g = _FakeGraph([HumanMessage(content="hi")])
    _run(g)
    assert g.read_threads == [MAIN]  # main thread read exactly once…
    assert MAIN not in g.written_threads  # …never written
    assert g.written_threads == []  # nothing written at all on the fake
    # The turn ran on a DISTINCT ephemeral thread derived from (but != ) the main id.
    assert len(g.invoked_on) == 1
    assert g.invoked_on[0] != MAIN
    assert g.invoked_on[0].startswith(f"{MAIN}::aside-")


def test_aside_runs_incognito():
    g = _FakeGraph([HumanMessage(content="hi")])
    _run(g)
    assert g.invoked_input["incognito"] is True


def test_aside_ephemeral_thread_is_unique_per_call():
    g = _FakeGraph([HumanMessage(content="hi")])
    _run(g)
    _run(g)
    assert g.invoked_on[0] != g.invoked_on[1]  # a fresh scratch thread each time


def test_aside_empty_question_is_a_noop():
    g = _FakeGraph([HumanMessage(content="hi")])
    out = asyncio.run(run_aside(g, object(), MAIN, "   "))
    assert out["found"] is False and out["reason"] == "empty_question"
    assert g.invoked_on == []  # no turn run


def test_aside_without_checkpointer():
    out = asyncio.run(run_aside(None, None, MAIN, "q"))
    assert out["found"] is False and out["reason"] == "no_checkpointer"
