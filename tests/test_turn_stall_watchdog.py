"""A wedged turn must END, not sit in TASK_STATE_WORKING forever (#2344).

The reported failure: a turn stopped producing anything and nothing anywhere ever
finished it. Server-side there was no bound on a turn at all, and the console's own
watchdog deliberately keeps waiting while the durable task still reports WORKING —
correctly, since it cannot distinguish a slow turn from a stuck one either. So the
spinner ran until reload, telemetry recorded no turn, and the failure was invisible.

The guard is a STALL window, not a wall-clock cap, and these tests exist mostly to pin
that distinction: a turn that keeps streaming must never be cut off however long it
runs, while a turn that goes silent must be failed with an attributable message.
"""

from __future__ import annotations

import asyncio

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook


@pytest.fixture(autouse=True)
def _clear_hook():
    set_terminal_hook(None)
    yield
    set_terminal_hook(None)


def _request_context(text: str = "hi") -> RequestContext:
    req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text=text)]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id="t-1", context_id="c-1")


async def _run(stream_fn, stall: float) -> list[TurnOutcome]:
    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    executor = ProtoAgentExecutor(stream_fn, stall_timeout_provider=lambda: stall)
    await executor.execute(_request_context(), EventQueue())
    return seen


@pytest.mark.asyncio
async def test_a_silent_turn_is_failed_not_left_working():
    """The bug: the stream goes quiet mid-turn and never speaks again."""
    entered = asyncio.Event()

    async def stream(text, ctx, **kwargs):
        yield ("text", "on it, let me look…")
        entered.set()
        await asyncio.sleep(3600)  # the wedged step — a tool call that never returns
        yield ("done", "never")

    outcomes = await asyncio.wait_for(_run(stream, stall=0.4), 15)
    assert entered.is_set()
    assert [o.state for o in outcomes] == ["failed"]  # NOT stuck in working


@pytest.mark.asyncio
async def test_the_failure_names_the_step_that_wedged():
    """A bare "stalled" sends you back to the databases. Name the last thing seen."""

    async def stream(text, ctx, **kwargs):
        yield ("tool_start", {"id": "c1", "name": "filesystem__search_files", "input": "{}"})
        await asyncio.sleep(3600)
        yield ("done", "never")

    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    executor = ProtoAgentExecutor(stream, stall_timeout_provider=lambda: 0.4)
    queue = EventQueue()
    await asyncio.wait_for(executor.execute(_request_context(), queue), 15)

    assert [o.state for o in seen] == ["failed"]
    # The operator-facing text must say what was running and that it was stopped.
    failed_text = " ".join(
        p.text
        for ev in _drain(queue)
        for p in getattr(getattr(getattr(ev, "status", None), "message", None), "parts", []) or []
        if getattr(p, "text", "")
    )
    assert "filesystem__search_files" in failed_text
    assert "stalled" in failed_text.lower()


@pytest.mark.asyncio
async def test_a_slow_but_streaming_turn_is_never_cut_off():
    """The property that makes a stall window the right primitive.

    Total runtime here far exceeds the stall window; every individual GAP stays under
    it. A wall-clock cap would kill this turn — a long research turn making dozens of
    tool calls is healthy work, not a hang.
    """

    async def stream(text, ctx, **kwargs):
        for i in range(12):
            await asyncio.sleep(0.1)  # 1.2s total, vs a 0.4s stall window
            yield ("tool_start", {"id": f"c{i}", "name": "web_search", "input": "{}"})
            yield ("tool_end", {"id": f"c{i}", "name": "web_search", "output": "ok"})
        yield ("done", "finished after a long but healthy run")

    outcomes = await asyncio.wait_for(_run(stream, stall=0.4), 20)
    assert [o.state for o in outcomes] == ["completed"]
    assert outcomes[0].tool_calls == 12


@pytest.mark.asyncio
async def test_zero_disables_the_guard():
    """The escape hatch has to actually pass the stream straight through."""

    async def stream(text, ctx, **kwargs):
        await asyncio.sleep(0.5)  # would trip any positive window used in these tests
        yield ("done", "slow but allowed")

    outcomes = await asyncio.wait_for(_run(stream, stall=0), 20)
    assert [o.state for o in outcomes] == ["completed"]


@pytest.mark.asyncio
async def test_no_provider_means_no_guard():
    """Every other construction site (tests, plugins, forks) omits the provider —
    they must keep the previous unbounded behavior rather than inherit a default."""

    async def stream(text, ctx, **kwargs):
        await asyncio.sleep(0.3)
        yield ("done", "ok")

    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    await asyncio.wait_for(ProtoAgentExecutor(stream).execute(_request_context(), EventQueue()), 20)
    assert [o.state for o in seen] == ["completed"]


@pytest.mark.asyncio
async def test_a_broken_provider_disables_the_guard_instead_of_failing_the_turn():
    """A watchdog that can itself break a turn is worse than the hang it catches."""

    def boom() -> float:
        raise RuntimeError("config went away")

    async def stream(text, ctx, **kwargs):
        yield ("done", "ok")

    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    executor = ProtoAgentExecutor(stream, stall_timeout_provider=boom)
    await asyncio.wait_for(executor.execute(_request_context(), EventQueue()), 20)
    assert [o.state for o in seen] == ["completed"]


@pytest.mark.asyncio
async def test_the_wedged_step_is_actually_cancelled():
    """Failing the task is only half of it — the work behind it has to be released,
    or the subprocess/socket it was waiting on leaks for the life of the process."""
    cancelled = asyncio.Event()

    async def stream(text, ctx, **kwargs):
        try:
            yield ("text", "working…")
            await asyncio.sleep(3600)
            yield ("done", "never")
        except (GeneratorExit, asyncio.CancelledError):
            cancelled.set()
            raise

    await asyncio.wait_for(_run(stream, stall=0.4), 15)
    assert cancelled.is_set()


def _drain(queue) -> list:
    """Best-effort read of everything the executor enqueued."""
    events = []
    inner = getattr(queue, "queue", None)
    while inner is not None and not inner.empty():
        events.append(inner.get_nowait())
    return events
