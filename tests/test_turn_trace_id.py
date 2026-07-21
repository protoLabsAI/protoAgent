"""The Langfuse trace id reaches the telemetry row — and the timing that makes it work.

A telemetry row you can't pivot to its trace is a dead end: the console shows a
slow/expensive turn and there's no way to get from it to the trace tree that
explains why. Closing that needs the trace id on ``TurnOutcome``, and the ONLY
place it can be read is inside the executor's event loop.

Why: ``observability.tracing.trace_session`` sets a contextvar in the *stream
generator's* body. Async generators share the caller's Context (unlike Tasks), so
the value is visible to the executor while the generator is suspended
mid-iteration — but the generator's ``finally`` resets it the moment the loop
ends. Read it from the terminal hook, or from the post-loop ``_outcome`` calls,
and you reliably get "".

``test_trace_id_is_empty_when_read_after_the_stream_closes`` pins that failure
mode directly, so a future refactor that moves the capture out of the loop fails
here with an explanation instead of silently writing blank trace ids.
"""

from __future__ import annotations

import contextvars

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook

_TRACE = "trace-abc123"


@pytest.fixture(autouse=True)
def _clear_hook():
    set_terminal_hook(None)
    yield
    set_terminal_hook(None)


def _request_context(text: str = "hi") -> RequestContext:
    req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text=text)]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id="t-1", context_id="c-1")


async def _run(stream_fn) -> list[TurnOutcome]:
    """Drive one turn through the real executor, collecting terminal outcomes."""
    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    executor = ProtoAgentExecutor(stream_fn)
    await executor.execute(_request_context(), EventQueue())
    return seen


@pytest.mark.asyncio
async def test_trace_id_captured_from_the_stream_reaches_the_outcome(monkeypatch):
    """The realistic shape: the stream sets the trace contextvar and resets it on exit."""
    cv: contextvars.ContextVar[str] = contextvars.ContextVar("trace", default="")

    monkeypatch.setattr("observability.tracing.current_trace_id", cv.get, raising=False)

    async def stream(text, ctx, **kwargs):
        token = cv.set(_TRACE)  # what trace_session does
        try:
            yield ("text", "hello")
            yield ("done", "hello")
        finally:
            cv.reset(token)  # ...and undoes on exit

    outcomes = await _run(stream)

    assert len(outcomes) == 1
    assert outcomes[0].trace_id == _TRACE, "trace id must survive to the terminal hook"


@pytest.mark.asyncio
async def test_trace_id_is_empty_when_read_after_the_stream_closes(monkeypatch):
    """Pins WHY the capture lives inside the loop.

    Same generator, but we read the contextvar the way a post-loop capture would
    — after iteration finishes. It is already reset. If someone "simplifies" the
    executor by reading the trace id in the terminal hook, this is the behavior
    they'd get, and the test above is what would break.
    """
    cv: contextvars.ContextVar[str] = contextvars.ContextVar("trace2", default="")

    async def stream():
        token = cv.set(_TRACE)
        try:
            yield "a"
        finally:
            cv.reset(token)

    during: list[str] = []
    async for _ev in stream():
        during.append(cv.get())
    after = cv.get()

    assert during == [_TRACE]  # visible mid-iteration
    assert after == ""  # gone once the generator closed


@pytest.mark.asyncio
async def test_trace_id_absent_when_tracing_is_unconfigured(monkeypatch):
    """Langfuse off is the default for most forks — the turn must be unaffected."""
    monkeypatch.setattr("observability.tracing.current_trace_id", lambda: None, raising=False)

    async def stream(text, ctx, **kwargs):
        yield ("text", "hello")
        yield ("done", "hello")

    outcomes = await _run(stream)

    assert len(outcomes) == 1
    assert outcomes[0].trace_id == ""
    assert outcomes[0].state == "completed"  # and the turn still succeeded


@pytest.mark.asyncio
async def test_capture_survives_a_tracing_failure(monkeypatch):
    """Telemetry must never break a turn — a raising trace lookup is swallowed."""

    def _boom():
        raise RuntimeError("langfuse exploded")

    monkeypatch.setattr("observability.tracing.current_trace_id", _boom, raising=False)

    async def stream(text, ctx, **kwargs):
        yield ("text", "hello")
        yield ("done", "hello")

    outcomes = await _run(stream)

    assert len(outcomes) == 1
    assert outcomes[0].trace_id == ""
    assert outcomes[0].state == "completed"


def test_telemetry_store_persists_and_migrates_trace_id(tmp_path):
    """The column round-trips, and an OLD db (no column) gains it on open."""
    import sqlite3

    from observability.telemetry_store import TelemetryStore

    path = str(tmp_path / "telemetry.db")
    store = TelemetryStore(path)
    store.record({"task_id": "t-1", "session_id": "s-1", "state": "completed", "trace_id": _TRACE})
    assert store.recent(1)[0]["trace_id"] == _TRACE

    # A store created before the column existed: drop it, reopen, and the
    # lightweight migration must add it back rather than raising.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE turns DROP COLUMN trace_id")
    db.commit()
    db.close()

    reopened = TelemetryStore(path)
    reopened.record({"task_id": "t-2", "session_id": "s-2", "state": "completed", "trace_id": "t2"})
    assert reopened.recent(1)[0]["trace_id"] == "t2"
