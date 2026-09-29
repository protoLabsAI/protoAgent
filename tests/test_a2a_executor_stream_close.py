"""The A2A executor closes the turn stream before ``execute`` returns (#3876).

``ProtoAgentExecutor.execute`` returns from inside its ``async for`` on every terminal
frame (``done`` / ``input_required`` / ``error``), leaving the stream suspended at that
frame's ``yield``. A bare ``async for`` does not close it, so the stream's ``finally`` —
the chat driver's per-thread lock release and trace flush — used to wait for the event
loop's async-generator finalizer. These tests pin that the cleanup has run by the time
``execute`` returns (or raises), with no ``gc.collect()`` and no polling, and that the
terminal frame is still enqueued BEFORE that cleanup runs.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest, TaskArtifactUpdateEvent, TaskState

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, _stall_guarded, set_terminal_hook
from graph.config import LangGraphConfig
from tests._turn_driver_fakes import Raise, ScriptedGraph, TraceSpy, set_interrupt, text

chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")


@pytest.fixture(autouse=True)
def _outcomes():
    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    yield seen
    set_terminal_hook(None)


def _request_context(task_id: str = "t-1", context_id: str = "c-1") -> RequestContext:
    req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text="hi")]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id=task_id, context_id=context_id)


_TERMINAL = (TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_FAILED)


class _RecordingQueue(EventQueue):
    """Records every terminal status the executor enqueues, in order."""

    def __init__(self):
        super().__init__()
        self.terminal: list[int] = []

    async def enqueue_event(self, event):
        status = getattr(event, "status", None)
        if status is not None and status.state in _TERMINAL:
            self.terminal.append(status.state)
        await super().enqueue_event(event)


# ── a stand-in for the chat driver: holds a lock across the turn, flushes on exit ──


class _Turn:
    """A stream factory shaped like ``_chat_langgraph_stream``: the frames are yielded
    while a per-thread lock is held, and the ``finally`` records a trace flush — plus
    whether the terminal frame had already been enqueued when the cleanup ran."""

    def __init__(self, frames, queue: _RecordingQueue | None = None):
        self.frames = frames
        self.queue = queue
        self.lock = asyncio.Lock()
        self.flushes = 0
        self.closes = 0
        self.terminal_before_cleanup: bool | None = None

    async def __call__(self, text, context_id, **kwargs):
        try:
            async with self.lock:
                for frame in self.frames:
                    if isinstance(frame, BaseException):
                        raise frame
                    yield frame
        finally:
            self.closes += 1
            self.flushes += 1
            if self.queue is not None:
                self.terminal_before_cleanup = bool(self.queue.terminal)


_TERMINALS = [
    pytest.param([("text", "hello"), ("done", "hello")], "completed", id="done"),
    pytest.param([("text", "which env?"), ("input_required", {"question": "Which env?"})], "input_required", id="hitl"),
    pytest.param([("text", "partial"), ("error", "invalid api key")], "failed", id="error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [0.0, 30.0], ids=["guard-off", "guard-on"])
@pytest.mark.parametrize(("frames", "state"), _TERMINALS)
async def test_terminal_frame_cleanup_runs_before_execute_returns(frames, state, stall, _outcomes):
    queue = _RecordingQueue()
    turn = _Turn(frames, queue)
    executor = ProtoAgentExecutor(turn, stall_timeout_provider=lambda: stall)

    await executor.execute(_request_context(), queue)

    # No gc.collect(), no loop ticks: the stream's `finally` has already run.
    assert not turn.lock.locked()
    assert turn.flushes == 1 and turn.closes == 1
    # ...and it ran AFTER the terminal frame was enqueued, so cleanup never delays it.
    assert turn.terminal_before_cleanup is True
    assert [o.state for o in _outcomes] == [state]
    assert len(queue.terminal) == 1


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_a_terminal_frame_is_still_closed_once(_outcomes):
    turn = _Turn([("text", "just text")])
    await ProtoAgentExecutor(turn, stall_timeout_provider=lambda: 30.0).execute(_request_context(), EventQueue())
    assert not turn.lock.locked() and turn.closes == 1
    assert [o.state for o in _outcomes] == ["completed"]


@pytest.mark.asyncio
async def test_a_stream_that_raises_is_failed_and_closed_once(_outcomes):
    turn = _Turn([("text", "partial"), RuntimeError("boom")])
    await ProtoAgentExecutor(turn, stall_timeout_provider=lambda: 30.0).execute(_request_context(), EventQueue())
    assert not turn.lock.locked() and turn.closes == 1
    assert [(o.state, o.error) for o in _outcomes] == [("failed", "boom")]


@pytest.mark.asyncio
async def test_cleanup_that_raises_does_not_break_a_completed_turn(_outcomes, caplog):
    async def stream(text, context_id, **kwargs):
        try:
            yield ("done", "fine")
        finally:
            raise RuntimeError("cleanup blew up")

    queue = _RecordingQueue()
    await ProtoAgentExecutor(stream).execute(_request_context(), queue)

    assert [o.state for o in _outcomes] == ["completed"]
    assert queue.terminal == [TaskState.TASK_STATE_COMPLETED]  # not re-failed
    assert "stream cleanup raised" in caplog.text


# ── cancellation ──────────────────────────────────────────────────────────────


class _GatedQueue(EventQueue):
    """Blocks the first artifact (answer-text) frame until the test cancels the turn, so
    the cancel lands while ``execute`` is awaiting the queue and the stream is suspended
    at a ``yield`` — the case where nothing else would ever close it."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()

    async def enqueue_event(self, event):
        if isinstance(event, TaskArtifactUpdateEvent) and not self.blocked.is_set():
            self.blocked.set()
            await asyncio.sleep(3600)
        await super().enqueue_event(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [0.0, 30.0], ids=["guard-off", "guard-on"])
async def test_cancelling_execute_mid_stream_closes_the_chain(stall, _outcomes):
    turn = _Turn([("text", "streaming…"), ("text", " more"), ("done", "never reached")])
    queue = _GatedQueue()
    task = asyncio.create_task(
        ProtoAgentExecutor(turn, stall_timeout_provider=lambda: stall).execute(_request_context(), queue)
    )
    await asyncio.wait_for(queue.blocked.wait(), 5)
    assert turn.lock.locked()  # the turn is live, suspended at its first `yield`

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not turn.lock.locked()
    assert turn.closes == 1
    assert [o.state for o in _outcomes] == ["canceled"]


# ── _stall_guarded on its own ─────────────────────────────────────────────────


class _CountingStream:
    """An async iterator (not a generator) that counts ``aclose()`` calls, so a double
    close is visible — a generator's second ``aclose()`` is a silent no-op."""

    def __init__(self, items, *, wedge_after: int | None = None):
        self.items = list(items)
        self.wedge_after = wedge_after
        self.served = 0
        self.aclose_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.wedge_after is not None and self.served >= self.wedge_after:
            await asyncio.sleep(3600)
        if not self.items:
            raise StopAsyncIteration
        self.served += 1
        return self.items.pop(0)

    async def aclose(self):
        self.aclose_calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [0.0, 30.0], ids=["guard-off", "guard-on"])
async def test_closing_the_guard_closes_the_inner_stream(stall):
    inner = _CountingStream([("text", "a"), ("text", "b")])
    guard = _stall_guarded(inner, stall, ["starting up"])
    assert await guard.__anext__() == ("text", "a")

    await guard.aclose()

    assert inner.aclose_calls == 1


@pytest.mark.asyncio
async def test_a_stall_still_raises_and_closes_the_inner_stream_exactly_once():
    inner = _CountingStream([("text", "a"), ("text", "never")], wedge_after=1)
    guard = _stall_guarded(inner, 0.2, ["running the `slow` tool"])
    assert await guard.__anext__() == ("text", "a")

    with pytest.raises(Exception) as exc:  # noqa: PT011 — TurnStalled, checked by name below
        await asyncio.wait_for(guard.__anext__(), 10)
    assert type(exc.value).__name__ == "TurnStalled"
    assert "running the `slow` tool" in str(exc.value)
    await guard.aclose()  # the executor's `finally` does this too: must not re-close

    assert inner.aclose_calls == 1


@pytest.mark.asyncio
async def test_a_stalled_turn_is_failed_and_its_stream_closed_once(_outcomes):
    entered = asyncio.Event()
    closes = []

    async def stream(text, context_id, **kwargs):
        try:
            yield ("tool_start", {"id": "c1", "name": "slow_tool", "input": "{}"})
            entered.set()
            await asyncio.sleep(3600)
            yield ("done", "never")
        finally:
            closes.append(1)

    queue = _RecordingQueue()
    await asyncio.wait_for(
        ProtoAgentExecutor(stream, stall_timeout_provider=lambda: 0.2).execute(_request_context(), queue), 10
    )

    assert entered.is_set()
    assert closes == [1]
    assert [o.state for o in _outcomes] == ["failed"]
    assert "slow_tool" in _outcomes[0].error and "stalled" in _outcomes[0].error.lower()
    assert queue.terminal == [TaskState.TASK_STATE_FAILED]


# ── the real chat driver behind the executor ─────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    """The real streaming turn driver over a scripted graph (see _turn_driver_fakes)."""
    from observability import metrics, pricing

    import runtime.state as rs

    class Env:
        pass

    e = Env()
    monkeypatch.setattr(metrics, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "record_overflow_recovery", lambda: None)
    monkeypatch.setattr(pricing, "cost_usd", lambda model, usage: 0.0)
    e.trace = TraceSpy().install(monkeypatch)
    for attr, val in {
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def install(streams):
        e.graph = ScriptedGraph(streams)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    yield e
    assert not getattr(getattr(e, "graph", None), "overrun", False), "driver made an unscripted graph call"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "state"),
    [
        pytest.param([text("r1", "the answer")], "completed", id="done"),
        pytest.param(
            [text("r1", "Let me ask."), set_interrupt({"question": "Which env?"})], "input_required", id="hitl"
        ),
        pytest.param([text("r1", "partial"), Raise(ValueError("invalid api key"))], "failed", id="error"),
    ],
)
async def test_real_driver_releases_thread_lock_and_flushes_trace_by_return(env, script, state, _outcomes):
    # The factory is the driver's impl generator, not the `_chat_langgraph_stream` wrapper:
    # the wrapper's own bare `async for` over the impl is the same bug one level down
    # (#3870, fixed separately). This pins the executor's half — it closes what it's given.
    env.install([script])
    ctx = _request_context(context_id="s-3876")
    executor = ProtoAgentExecutor(chat_mod._chat_langgraph_stream_impl, stall_timeout_provider=lambda: 30.0)

    await executor.execute(ctx, EventQueue())

    assert [o.state for o in _outcomes] == [state]
    # No gc.collect(), no polling: both happened inside execute().
    assert env.trace.flushes == 1
    assert not turn_control._thread_lock("a2a:s-3876").locked()
