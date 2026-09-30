"""Turn-scoped projected context: overlapping turns each deliver their own.

One compiled graph — and so one instance of every middleware — serves every
concurrent turn (A2A callers, the console, background jobs, goal loops, the
scheduler). The composed per-turn projection (ADR 0108 D2) and the one-shot
toolset notice must therefore travel with the RUN, not sit on the shared
middleware instance, or a turn's later model calls deliver the context of
whichever turn started most recently.

These tests drive two interleaved turns on different threads through a real
``create_agent`` graph with the real KnowledgeMiddleware, ToolDeltaMiddleware
and PromptCaptureMiddleware and a checkpointer: turn A makes its first model
call, parks in a tool until turn B has run to completion, then makes its second
model call. Every model call must see only its own turn's projection; the
prompt-capture rows must record only their own turn's projection; and nothing
projected may reach the checkpointer. Both the sync (``invoke`` → ``before_agent``
/ ``wrap_model_call``) and async (``ainvoke`` → ``abefore_agent`` /
``awrap_model_call``) paths are covered.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from graph import tool_delta
from graph.context_frame import is_context_frame
from graph.middleware.knowledge import TURN_PROJECTION_KEY, KnowledgeMiddleware
from graph.middleware.prompt_capture import PromptCaptureMiddleware
from graph.middleware.tool_delta import TOOL_DELTA_NOTE_KEY, ToolDeltaMiddleware
from graph.state import ProtoAgentState

HOT_MARKER = "HOT-MARKER-q7"


class _QueryEchoStore:
    """A knowledge store whose RAG hit names the turn's query — so each turn's
    projection is distinguishable, and only appears in a projection (never in
    the operator's own message)."""

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        return HOT_MARKER

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        return [(1, HOT_MARKER)]

    def search(self, query: str, k: int = 5, **kwargs) -> list[dict]:
        token = query.split()[0]
        return [{"id": 7, "table": "chunks", "preview": f"RAG-HIT[{token}]", "source_type": "operator"}]


def _turn_of(messages) -> str:
    for m in messages:
        if isinstance(m, HumanMessage) and not is_context_frame(m):
            return str(m.content).split()[0]
    return "?"


class _TurnModel(BaseChatModel):
    """Records what each call was delivered; turn-A's first call parks in ``hold``."""

    log: Any = None  # list[(turn, [frame texts])]
    first_call_a: Any = None  # threading.Event

    @property
    def _llm_type(self) -> str:
        return "turn-scoped-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _reply(self, messages) -> AIMessage:
        turn = _turn_of(messages)
        self.log.append((turn, [str(m.content) for m in messages if is_context_frame(m)]))
        if turn == "turn-A" and not any(isinstance(m, AIMessage) for m in messages):
            self.first_call_a.set()
            return AIMessage(content="", tool_calls=[{"name": "hold", "args": {}, "id": "call-hold", "type": "tool_call"}])
        return AIMessage(content=f"done {turn}")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])


class _SnapshotRecorder:
    def __init__(self):
        self.rows: list[dict] = []
        self.retention_days = 0
        self.max_calls = 0

    def record(self, **kw):
        self.rows.append(kw)


def _build(hold_tool, *, model_log, first_call_a):
    knowledge = KnowledgeMiddleware(_QueryEchoStore())
    knowledge._prior_sessions_cache = ""  # no disk digest — the store is the only source
    capture = PromptCaptureMiddleware()
    recorder = _SnapshotRecorder()
    capture._store = lambda: recorder  # type: ignore[method-assign]
    saver = InMemorySaver()
    graph = create_agent(
        model=_TurnModel(log=model_log, first_call_a=first_call_a),
        tools=[hold_tool],
        # Production order: PromptCapture outer, then Knowledge, then ToolDelta.
        middleware=[capture, knowledge, ToolDeltaMiddleware()],
        system_prompt="stable system prompt",
        state_schema=ProtoAgentState,
        checkpointer=saver,
    )
    return graph, recorder, saver


def _frame_text(frames: list[str]) -> str:
    return "\n".join(frames)


def _assert_turn_scoped(model_log, recorder, saver, graph):
    turns = [t for t, _ in model_log]
    # A's first call, all of B, then A's second call — the interleave happened.
    assert turns == ["turn-A", "turn-B", "turn-A"], turns

    for turn, frames in model_log:
        text = _frame_text(frames)
        other = "turn-B" if turn == "turn-A" else "turn-A"
        assert f"RAG-HIT[{turn}]" in text, (turn, text)  # its own projection…
        assert f"RAG-HIT[{other}]" not in text, (turn, text)  # …and only its own
        assert text.count("<injected_context>") == len(frames)

    # The toolset notice belongs to the turn that took it (A) on EVERY one of its
    # calls, and never reaches the other turn.
    a_calls = [_frame_text(f) for t, f in model_log if t == "turn-A"]
    b_calls = [_frame_text(f) for t, f in model_log if t == "turn-B"]
    assert all("tool-q7" in c for c in a_calls), a_calls
    assert not any("tool-q7" in c for c in b_calls), b_calls

    # Prompt capture records each call's OWN projection (rows land in call order).
    assert len(recorder.rows) == len(model_log)
    for (turn, _frames), row in zip(model_log, recorder.rows, strict=True):
        other = "turn-B" if turn == "turn-A" else "turn-A"
        projected = row["projected_context"] or ""
        assert f"RAG-HIT[{turn}]" in projected, (turn, projected)
        assert f"RAG-HIT[{other}]" not in projected, (turn, projected)

    # ADR 0108 D2: nothing projected ever reaches the checkpointer — not the
    # checkpoint blobs, not the pending writes, not the readable state.
    blob = repr(saver.storage) + repr(saver.writes) + repr(dict(saver.blobs))
    assert HOT_MARKER not in blob
    assert "RAG-HIT[" not in blob
    assert "tool-q7" not in blob
    for thread in ("A", "B"):
        values = graph.get_state({"configurable": {"thread_id": thread}}).values
        assert TURN_PROJECTION_KEY not in values
        assert TOOL_DELTA_NOTE_KEY not in values


@pytest.fixture(autouse=True)
def _pending_toolset_notice():
    tool_delta.reset_for_tests()
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "tool-q7"])  # one pending notice — turn A takes it
    yield
    tool_delta.reset_for_tests()


def test_interleaved_sync_turns_each_deliver_their_own_projection():
    model_log: list = []
    first_call_a = threading.Event()
    b_done = threading.Event()

    def hold() -> str:
        """Park turn A until turn B has run end to end."""
        assert b_done.wait(20), "turn B never finished"
        return "held"

    graph, recorder, saver = _build(
        StructuredTool.from_function(hold), model_log=model_log, first_call_a=first_call_a
    )
    errors: list[BaseException] = []

    def run_a():
        try:
            graph.invoke(
                {"messages": [HumanMessage(content="turn-A question")], "session_id": "sess-A"},
                {"configurable": {"thread_id": "A"}},
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    t = threading.Thread(target=run_a)
    t.start()
    assert first_call_a.wait(20), "turn A never reached its first model call"
    graph.invoke(
        {"messages": [HumanMessage(content="turn-B question")], "session_id": "sess-B"},
        {"configurable": {"thread_id": "B"}},
    )
    b_done.set()
    t.join(20)
    assert not t.is_alive()
    assert not errors, errors

    _assert_turn_scoped(model_log, recorder, saver, graph)


async def test_interleaved_async_turns_each_deliver_their_own_projection():
    model_log: list = []
    first_call_a = threading.Event()
    b_done = asyncio.Event()

    async def hold() -> str:
        """Park turn A until turn B has run end to end."""
        await asyncio.wait_for(b_done.wait(), 20)
        return "held"

    graph, recorder, saver = _build(
        StructuredTool.from_function(coroutine=hold, name="hold", description="Park turn A."),
        model_log=model_log,
        first_call_a=first_call_a,
    )

    task_a = asyncio.create_task(
        graph.ainvoke(
            {"messages": [HumanMessage(content="turn-A question")], "session_id": "sess-A"},
            {"configurable": {"thread_id": "A"}},
        )
    )
    for _ in range(2000):
        if first_call_a.is_set():
            break
        await asyncio.sleep(0.01)
    assert first_call_a.is_set(), "turn A never reached its first model call"
    await graph.ainvoke(
        {"messages": [HumanMessage(content="turn-B question")], "session_id": "sess-B"},
        {"configurable": {"thread_id": "B"}},
    )
    b_done.set()
    await asyncio.wait_for(task_a, 20)

    _assert_turn_scoped(model_log, recorder, saver, graph)


def test_a_resumed_run_starts_without_a_projection():
    """The channel is one run long: a later run on the same thread whose newest
    message is not fresh input (a resume / kicker re-entry) delivers nothing —
    the previous run's projection is not replayed from anywhere."""
    model_log: list = []
    graph, _recorder, _saver = _build(
        StructuredTool.from_function(lambda: "x", name="hold", description="unused"),
        model_log=model_log,
        first_call_a=threading.Event(),
    )
    cfg = {"configurable": {"thread_id": "C"}}
    graph.invoke({"messages": [HumanMessage(content="turn-C question")]}, cfg)
    assert "RAG-HIT[turn-C]" in _frame_text(model_log[-1][1])
    # Re-enter with no fresh human input (newest message is the AI reply).
    graph.invoke({"messages": []}, cfg)
    assert model_log[-1][1] == []


def test_a_nested_run_on_the_same_middleware_keeps_the_outer_projection():
    """A nested run (a delegation that reuses the same compiled stack, invoked
    from inside a tool of the outer turn) composes its own projection — and the
    outer turn's next model call still delivers the OUTER turn's projection."""
    model_log: list = []
    holder: dict = {}

    def hold() -> str:
        """Run a nested turn on the same graph (and so the same middleware)."""
        holder["graph"].invoke(
            {"messages": [HumanMessage(content="turn-N nested question")]},
            {"configurable": {"thread_id": "N"}},
        )
        return "nested done"

    graph, recorder, _saver = _build(
        StructuredTool.from_function(hold), model_log=model_log, first_call_a=threading.Event()
    )
    holder["graph"] = graph
    graph.invoke(
        {"messages": [HumanMessage(content="turn-A question")]},
        {"configurable": {"thread_id": "A"}},
    )
    assert [t for t, _ in model_log] == ["turn-A", "turn-N", "turn-A"]
    for turn, frames in model_log:
        text = _frame_text(frames)
        assert f"RAG-HIT[{turn}]" in text, (turn, text)
        assert text.count("RAG-HIT[") == 1, (turn, text)
    for (turn, _f), row in zip(model_log, recorder.rows, strict=True):
        assert (row["projected_context"] or "").count("RAG-HIT[") == 1
        assert f"RAG-HIT[{turn}]" in (row["projected_context"] or "")
