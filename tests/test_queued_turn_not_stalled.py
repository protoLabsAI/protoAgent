"""A turn queued behind another on the same thread is not "stalled" (#5106).

Seen on a production agent running a five-hour job in the Activity thread: scheduled
fires and inbox items that landed there were failed with "The turn stalled: no progress
for 900s while starting up". They had not stalled. They were waiting for the thread's
lock behind the running turn, and a turn waiting for the lock emits nothing — which is
exactly what the stall guard (``a2a_impl.executor._stall_guarded``) measures.

These drive the REAL A2A executor over the REAL chat driver
(``server.chat._chat_langgraph_stream``, which takes the per-thread lock and records the
holder); only the graph — the model boundary — is a scripted fake, with real sleeps and
a shrunk stall window. They pin both sides:

* a turn queued for longer than the window survives and runs once the thread frees;
* a head-of-line turn that really is wedged is still failed (by its own guard), and the
  turn queued behind it then runs;
* a turn that holds the thread and goes silent is still failed — queueing is no blanket
  exemption.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook
from graph.config import LangGraphConfig
from tests._turn_driver_fakes import ScriptedGraph, text

chat_mod = importlib.import_module("server.chat")

# The stall window. Every healthy gap below is at most a sixth of it, so a loaded CI box
# has wide slack before a healthy gap could read as a stall.
_STALL = 0.3
_GAP = _STALL / 6


class _Sleep:
    """Script step: the model call takes ``seconds`` (real time) before its next event."""

    def __init__(self, seconds: float):
        self.seconds = seconds


class _Signal:
    """Script step: set ``event`` (the test learns the turn now holds the thread)."""

    def __init__(self, event: asyncio.Event):
        self.event = event


class _TimedGraph(ScriptedGraph):
    """``ScriptedGraph`` whose scripts can also take real time or signal the test."""

    async def astream_events(self, graph_input, config=None, version=None):
        self.stream_calls.append((graph_input, config))
        script = self.streams.pop(0)
        for item in script:
            if isinstance(item, _Sleep):
                await asyncio.sleep(item.seconds)
            elif isinstance(item, _Signal):
                item.event.set()
            else:
                yield item


@pytest.fixture
def env(monkeypatch):
    """The real chat driver with only the graph and tracing faked (as tests/test_turn_error_4726.py)."""
    from observability import tracing

    import runtime.state as rs

    cfg = LangGraphConfig()
    for attr, val in {
        "telemetry_store": None,
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": cfg,
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    @contextlib.asynccontextmanager
    async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
        yield None

    monkeypatch.setattr(tracing, "trace_session", trace_session)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)

    def install(*streams):
        graph = _TimedGraph(streams=streams)
        monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
        return graph

    outcomes: list[TurnOutcome] = []
    set_terminal_hook(outcomes.append)
    yield install, outcomes
    set_terminal_hook(None)


def _ctx(task_id: str, context_id: str = "system:activity") -> RequestContext:
    req = SendMessageRequest(message=Message(message_id=f"m-{task_id}", role=Role.ROLE_USER, parts=[Part(text="go")]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id=task_id, context_id=context_id)


def _executor() -> ProtoAgentExecutor:
    return ProtoAgentExecutor(chat_mod._chat_langgraph_stream, stall_timeout_provider=lambda: _STALL)


def _by_task(outcomes: list[TurnOutcome]) -> dict[str, TurnOutcome]:
    return {o.task_id: o for o in outcomes}


@pytest.mark.asyncio
async def test_a_turn_queued_longer_than_the_stall_window_is_not_failed(env):
    install, outcomes = env
    holding = asyncio.Event()
    # The running turn: healthy, streaming every _GAP, for ~4 stall windows in all.
    running = [_Signal(holding)]
    for i in range(24):
        running += [_Sleep(_GAP), text("r1", f"step {i} ")]
    install(running, [text("r2", "the queued turn ran")])

    first = asyncio.ensure_future(_executor().execute(_ctx("t-running"), EventQueue()))
    await asyncio.wait_for(holding.wait(), 5)
    queued = asyncio.ensure_future(_executor().execute(_ctx("t-queued"), EventQueue()))
    await asyncio.wait_for(asyncio.gather(first, queued), 30)

    got = _by_task(outcomes)
    assert got["t-running"].state == "completed"
    # Queued behind it for ~4 windows, then ran — NOT failed as stalled while starting up.
    assert got["t-queued"].state == "completed", got["t-queued"].error
    assert "the queued turn ran" in got["t-queued"].text


@pytest.mark.asyncio
async def test_a_dead_head_of_line_turn_is_still_failed_and_the_queue_moves(env):
    install, outcomes = env
    holding = asyncio.Event()
    # The head-of-line turn says one thing, then wedges inside its next step.
    install([_Signal(holding), text("r1", "on it"), _Sleep(3600)], [text("r2", "the queued turn ran")])

    first = asyncio.ensure_future(_executor().execute(_ctx("t-dead"), EventQueue()))
    await asyncio.wait_for(holding.wait(), 5)
    queued = asyncio.ensure_future(_executor().execute(_ctx("t-queued"), EventQueue()))
    await asyncio.wait_for(asyncio.gather(first, queued), 30)

    got = _by_task(outcomes)
    assert got["t-dead"].state == "failed"
    assert got["t-dead"].error.startswith(f"The turn stalled: no progress for {_STALL:g}s")
    assert got["t-queued"].state == "completed", got["t-queued"].error


@pytest.mark.asyncio
async def test_a_turn_that_holds_the_thread_and_goes_silent_still_stalls(env):
    install, outcomes = env
    install([_Sleep(3600)])

    await asyncio.wait_for(_executor().execute(_ctx("t-silent"), EventQueue()), 30)

    (only,) = outcomes
    assert only.state == "failed"
    assert "while starting up" in only.error


@pytest.mark.asyncio
async def test_turns_on_other_threads_do_not_count_as_queueing(env):
    """A busy thread elsewhere is no excuse: only THIS thread's holder defers the stall."""
    install, outcomes = env
    holding = asyncio.Event()
    busy = [_Signal(holding)]
    for i in range(18):
        busy += [_Sleep(_GAP), text("r1", f"step {i} ")]
    install(busy, [_Sleep(3600)])

    other = asyncio.ensure_future(_executor().execute(_ctx("t-other", "chat-elsewhere"), EventQueue()))
    await asyncio.wait_for(holding.wait(), 5)
    silent = asyncio.ensure_future(_executor().execute(_ctx("t-silent"), EventQueue()))
    await asyncio.wait_for(asyncio.gather(other, silent), 30)

    got = _by_task(outcomes)
    assert got["t-other"].state == "completed"
    assert got["t-silent"].state == "failed"


# ── a scheduled fire into a busy thread (#5106) ───────────────────────────────


@pytest.mark.asyncio
async def test_a_fire_into_a_busy_thread_runs_exactly_once_after_it_frees(env, tmp_path, monkeypatch, caplog):
    """The scheduler's own wait (``_fire_timeout_s``) runs out while its turn is still
    queued behind a long job on the Activity thread. The fire is not dropped, not failed
    and not resent: it stays unresolved, runs when the thread frees, and settles once.

    Real scheduler, real a2a-sdk app over the real executor and chat driver; the
    scheduler's HTTP goes to that app in-process. Only the model is faked."""
    import logging
    from datetime import UTC, datetime, timedelta

    import httpx

    from scheduler.local import LocalScheduler
    from tests._a2a_app import A2A_HEADERS, drain_handlers
    from tests._a2a_app import build_app as build_a2a_app

    install, outcomes = env
    holding = asyncio.Event()
    job_turn = [_Signal(holding)]
    for i in range(30):  # ~5 stall windows of healthy work, far past the fire timeout
        job_turn += [_Sleep(_GAP), text("r1", f"merge step {i} ")]
    graph = install(job_turn, [text("r2", "the scheduled check ran")])

    app = build_a2a_app(chat_mod._chat_langgraph_stream, stall_timeout_provider=lambda: _STALL)
    real_client = httpx.AsyncClient
    sends: list[str] = []

    def client(**kw):
        c = real_client(transport=httpx.ASGITransport(app=app), base_url="http://agent", **kw)
        post = c.post

        async def counted(url, *a, **k):
            if (k.get("json") or {}).get("method") == "SendMessage":
                sends.append(k["json"]["params"]["message"]["messageId"])
            return await post(url, *a, **k)

        c.post = counted
        return c

    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c:
        r = await c.post(
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "job",
                "method": "SendMessage",
                "params": {
                    "configuration": {"returnImmediately": True},
                    "message": {
                        "messageId": "long-job",
                        "role": "ROLE_USER",
                        "parts": [{"text": "run the merge queue"}],
                        "contextId": "system:activity",
                    },
                },
            },
        )
        assert r.status_code == 200
    await asyncio.wait_for(holding.wait(), 5)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    s = LocalScheduler(agent_name="t", invoke_url="http://agent", api_key="", bearer_token="", db_dir=tmp_path)
    s._fire_timeout_s = _STALL  # the fire's own wait runs out long before the job ends
    job = s.add_job("check the queue", (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job_id="wait:system:activity")

    with caplog.at_level(logging.INFO, logger="scheduler.local"):
        await s._tick()
        await asyncio.gather(*s._fire_tasks)
        # The fire's wait ran out while it was queued: unresolved, visibly pending, not lost.
        assert s.get_job(job.id) is not None
        assert "queued or running" in (s.get_job(job.id).last_error or "")
        assert "delivery unknown" not in caplog.text
        assert s._claim_due_jobs(datetime.now(UTC)) == []  # cannot fire a second time meanwhile

        for _ in range(400):
            await s._tick()
            await asyncio.gather(*s._fire_tasks)
            if s.get_job(job.id) is None:
                break
            await asyncio.sleep(0.02)

    assert s.get_job(job.id) is None  # the one-shot settled (and was removed) exactly once
    assert len(sends) == 1  # never resent
    assert len(graph.stream_calls) == 2  # the job turn, then the fire — once
    states = sorted(o.state for o in outcomes)
    assert states == ["completed", "completed"], [o.error for o in outcomes]
    assert "fired but the turn FAILED" not in caplog.text
    await drain_handlers()


# ── the window restarts when the queued turn takes the thread (#5117 review) ──────
#
# The holder lets go 0.8 of a window into the queued turn's first window. Before the fix
# that window then ran out 0.2 windows after the queued turn took the thread, and it was
# failed "while starting up" — the false stall #5106 reports, just shorter. A wider window
# here than above keeps the 0.3-window margins clear of CI scheduling noise.
_WIDE = 1.0


def _wide_executor() -> ProtoAgentExecutor:
    return ProtoAgentExecutor(chat_mod._chat_langgraph_stream, stall_timeout_provider=lambda: _WIDE)


def _holder_releasing_after(fraction: float, signal: asyncio.Event) -> list:
    steps = 4
    script = [_Signal(signal)]
    for i in range(steps):
        script += [_Sleep(_WIDE * fraction / steps), text("r1", f"step {i} ")]
    return script


@pytest.mark.asyncio
async def test_a_turn_that_gets_the_thread_late_in_a_window_gets_a_full_window(env):
    install, outcomes = env
    holding = asyncio.Event()
    # Its first frame comes 0.5 window after it takes the thread: past what was left of
    # the window it was queued in (0.2), well inside a fresh one.
    install(_holder_releasing_after(0.8, holding), [_Sleep(_WIDE * 0.5), text("r2", "the queued turn ran")])

    first = asyncio.ensure_future(_wide_executor().execute(_ctx("t-running"), EventQueue()))
    await asyncio.wait_for(holding.wait(), 5)
    queued = asyncio.ensure_future(_wide_executor().execute(_ctx("t-queued"), EventQueue()))
    await asyncio.wait_for(asyncio.gather(first, queued), 30)

    got = _by_task(outcomes)
    assert got["t-running"].state == "completed"
    assert got["t-queued"].state == "completed", got["t-queued"].error


@pytest.mark.asyncio
async def test_a_turn_that_gets_the_thread_and_then_says_nothing_still_stalls(env):
    install, outcomes = env
    holding = asyncio.Event()
    install(_holder_releasing_after(0.8, holding), [_Sleep(3600)])

    loop = asyncio.get_running_loop()
    first = asyncio.ensure_future(_wide_executor().execute(_ctx("t-running"), EventQueue()))
    await asyncio.wait_for(holding.wait(), 5)
    started = loop.time()
    queued = asyncio.ensure_future(_wide_executor().execute(_ctx("t-silent"), EventQueue()))
    await asyncio.wait_for(asyncio.gather(first, queued), 30)
    elapsed = loop.time() - started

    got = _by_task(outcomes)
    assert got["t-silent"].state == "failed"
    assert "while starting up" in got["t-silent"].error
    # Failed a full window after it took the thread (~0.8 + 1.0), not when its first
    # queued window ran out (1.0).
    assert elapsed >= _WIDE * 1.6, elapsed
