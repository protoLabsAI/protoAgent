"""A resumed turn's recompose queries with the turn's own input and is injection-logged.

#3958 made a HITL resume (``Command(resume=…)``) recompose the turn's projection in
``KnowledgeMiddleware.before_model``. Two gaps closed here:

- The retrieval query was the NEWEST ``HumanMessage``. By the time a turn is resumed,
  the runtime may have written its own ``HumanMessage`` above the turn's input — a guard
  note (round governor / stall guard / completion guard) or a conversation summary — so
  the resumed calls were fed memory retrieved for the machinery's text. The query is
  now the newest OPERATOR input; a folded steer counts (it is the operator's own text).
- The resume compose ran with ``record=False``, so what the resumed calls actually
  received never reached the ADR 0069 D6 injection log. It now writes its own row.

Driven through a real ``create_agent`` graph with the real KnowledgeMiddleware and a
checkpointer; the knowledge store records every retrieval query.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from graph.context_frame import is_context_frame
from graph.middleware.guard_notes import guard_note
from graph.middleware.knowledge import KnowledgeMiddleware
from graph.middleware.steering import _INTERJECTION
from graph.state import ProtoAgentState


class _QueryLogStore:
    def __init__(self):
        self.queries: list[str] = []

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        return "HOT"

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        return [(1, "HOT")]

    def search(self, query: str, k: int = 5, **kwargs) -> list[dict]:
        self.queries.append(query)
        return [{"id": 7, "table": "chunks", "preview": f"RAG-HIT[{query.split()[0]}]", "source_type": "operator"}]


class _ScriptModel(BaseChatModel):
    """Call n (0-based, counted by AIMessages already in the thread) calls ``script[n]``;
    past the script it answers. Logs the projection frames each call received."""

    script: Any = None
    log: Any = None

    @property
    def _llm_type(self) -> str:
        return "resume-compose-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _reply(self, messages) -> AIMessage:
        self.log.append("\n".join(str(m.content) for m in messages if is_context_frame(m)))
        n = sum(1 for m in messages if isinstance(m, AIMessage))
        if n < len(self.script):
            return AIMessage(
                content="", tool_calls=[{"name": self.script[n], "args": {}, "id": f"c{n}", "type": "tool_call"}]
            )
        return AIMessage(content="done")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages)


class _InjectAfterFirstRound(AgentMiddleware):
    """Writes one runtime ``HumanMessage`` onto the thread before the second model
    call — the way a guard, the summarizer or the steering queue does mid-turn."""

    def __init__(self, message: HumanMessage):
        super().__init__()
        self._message = message

    def before_model(self, state, runtime):
        msgs = state["messages"]
        if sum(isinstance(m, AIMessage) for m in msgs) == 1 and not any(m.id == self._message.id for m in msgs):
            return {"messages": [self._message]}
        return None


def step() -> str:
    """A plain tool round."""
    return "ok"


def ask() -> str:
    """Ask the operator (HITL): parks the turn on an interrupt."""
    return str(interrupt({"question": "proceed?"}))


def _build(injected: HumanMessage | None, script=("step", "ask")):
    store = _QueryLogStore()
    knowledge = KnowledgeMiddleware(store)
    knowledge._prior_sessions_cache = ""  # no disk digest
    log: list[str] = []
    middleware: list = [knowledge]
    if injected is not None:
        injected.id = "injected-1"
        middleware.append(_InjectAfterFirstRound(injected))
    graph = create_agent(
        model=_ScriptModel(script=list(script), log=log),
        tools=[StructuredTool.from_function(step), StructuredTool.from_function(ask)],
        middleware=middleware,
        system_prompt="sys",
        state_schema=ProtoAgentState,
        checkpointer=InMemorySaver(),
    )
    return graph, store, log


def _summary(source: str) -> HumanMessage:
    return HumanMessage(content="SUMMARY of the earlier conversation", additional_kwargs={"lc_source": source})


_MACHINERY = {
    "round-governor note": lambda: guard_note("round-governor", "GUARDNOTE re-read the working state"),
    "stall-guard note": lambda: guard_note("stall-guard", "STALLNOTE you are repeating yourself"),
    "summarization summary": lambda: _summary("summarization"),
    "compaction summary": lambda: _summary("compaction"),
}


@pytest.mark.parametrize("kind", list(_MACHINERY))
def test_resume_compose_queries_with_the_turns_input_not_a_runtime_message(kind):
    graph, store, log = _build(_MACHINERY[kind]())
    cfg = {"configurable": {"thread_id": f"t-{kind}"}}
    out = graph.invoke({"messages": [HumanMessage(content="turn-Q original ask")], "session_id": "sess-q"}, cfg)
    assert out.get("__interrupt__"), out
    graph.invoke(Command(resume="yes"), cfg)

    # Turn entry, then the resume compose — both with the turn's own input.
    assert store.queries == ["turn-Q original ask", "turn-Q original ask"], store.queries
    assert "RAG-HIT[turn-Q]" in log[-1], log[-1]


async def test_resume_compose_query_async_path():
    graph, store, log = _build(_MACHINERY["round-governor note"]())
    cfg = {"configurable": {"thread_id": "t-async"}}
    await graph.ainvoke({"messages": [HumanMessage(content="turn-Q original ask")], "session_id": "sess-a"}, cfg)
    await graph.ainvoke(Command(resume="yes"), cfg)
    assert store.queries == ["turn-Q original ask", "turn-Q original ask"], store.queries
    assert "RAG-HIT[turn-Q]" in log[-1], log[-1]


def test_a_folded_steer_is_the_turns_input_on_resume():
    """A steer is the operator's own text, typed mid-turn to redirect it — it IS what
    the turn is now for, so the resume compose queries with it (minus the frame)."""
    graph, store, log = _build(HumanMessage(content=_INTERJECTION + "turn-S redirected ask"))
    cfg = {"configurable": {"thread_id": "t-steer"}}
    graph.invoke({"messages": [HumanMessage(content="turn-Q original ask")], "session_id": "sess-s"}, cfg)
    graph.invoke(Command(resume="yes"), cfg)
    assert store.queries == ["turn-Q original ask", "turn-S redirected ask"], store.queries
    assert "RAG-HIT[turn-S]" in log[-1], log[-1]


def test_a_top_entry_whose_newest_message_is_a_runtime_message_composes_nothing():
    """The before_agent "fresh input" guard uses the same rule: a run that enters at the
    top when the newest message is a guard note or a summary (a kicker retry after the
    run died right after one was written) has no fresh operator input."""
    for message in (_MACHINERY["stall-guard note"](), _MACHINERY["compaction summary"]()):
        store = _QueryLogStore()
        knowledge = KnowledgeMiddleware(store)
        knowledge._prior_sessions_cache = ""
        state = {"messages": [HumanMessage(content="turn-Q original ask"), AIMessage(content="x"), message]}
        assert knowledge.before_agent(state, None) == {"protoagent_turn_projection": {}}
        assert store.queries == []


def test_resume_compose_writes_one_injection_row_per_compose():
    """ADR 0069 D6: the resumed calls' projection is logged — entry row + one row per
    resume compose, each carrying what that compose injected."""
    from observability.injection_log import injection_log

    graph, _store, _log = _build(None, script=("ask", "step", "ask"))
    cfg = {"configurable": {"thread_id": "t-log"}}
    graph.invoke({"messages": [HumanMessage(content="turn-L ask")], "session_id": "sess-log"}, cfg)
    assert len(injection_log().recent(session_id="sess-log")) == 1
    graph.invoke(Command(resume="1"), cfg)  # resumes, then parks on the second ask
    assert len(injection_log().recent(session_id="sess-log")) == 2
    graph.invoke(Command(resume="2"), cfg)
    rows = injection_log().recent(session_id="sess-log")
    assert len(rows) == 3, rows
    for row in rows:
        assert row["rag_chunk_ids"] == [7]
        assert row["hot_chunk_ids"] == [1]


def test_is_turn_input_and_turn_query():
    from graph.middleware.knowledge import is_turn_input, turn_query

    op = HumanMessage(content="operator ask")
    steer = HumanMessage(content=_INTERJECTION + "steer text")
    assert is_turn_input(op) and is_turn_input(steer)
    for m in (*(f() for f in _MACHINERY.values()), AIMessage(content="a")):
        assert not is_turn_input(m), m
    # A room message (a delegate's reply written into a room thread) is not machinery
    # this rule knows about — left as input, as before.
    assert is_turn_input(HumanMessage(content="r", additional_kwargs={"lc_source": "room"}))
    assert turn_query([op, AIMessage(content="a"), _MACHINERY["compaction summary"]()]) == "operator ask"
    assert turn_query([op, steer, guard_note("round-governor", "n")]) == "steer text"
    assert turn_query([guard_note("round-governor", "n")]) == ""
