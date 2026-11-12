"""Interrupted turns D7 S6 (ADR 0129, #5149): classify from checkpoint on boot and
auto-resume the safe cases.

Restart-interruption acceptance over the PRODUCTION paths and temporary persistent
stores. Real: ``LocalScheduler`` over a temp ``jobs.db`` (claim / serialize /
reconcile + the new D7 boot recovery), the a2a-sdk handler + ``ProtoAgentExecutor``
+ the production ``_chat_langgraph_stream`` turn driver, the DURABLE sqlite task
store and ``initialize_a2a_stores`` boot reconciliation, a REAL compiled LangGraph
over a real checkpointer shared across the simulated restart, and the production
``InterruptedTurnRecovery`` classifier/resume driver. Faked: only the model
(deterministic graph nodes) and the process death itself — at the "SIGTERM" moment
the checkpointer and the durable task store drop writes, which is exactly what a
killed process can no longer persist, so the zombie turn's error paths cannot
retro-write state the real crash would have lost.

The scenarios map 1:1 onto the card's acceptance criteria: a job session killed
during a slow model call resumes from its checkpoint and completes once; a ``wait:``
continuation killed during a tool call resumes with the dangling call reported as
interrupted and its loop continues; any other one-shot killed mid-tool-call goes to
``interrupted_needs_operator`` (never ``failed``, never resent); a recurring job's
interrupted occurrence is skipped (``interrupted``) and the next slot fires; a second
interruption of the same occurrence is never auto-resumed; interactive chat turns
keep today's fail-on-boot behavior.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, TypedDict

import httpx
import pytest
from langgraph.graph.message import add_messages

import scheduler.local as scheduler_local
from a2a_impl import hitl_routing
from a2a_impl.executor import set_progress_hook, set_terminal_hook
from a2a_impl.stores import (
    RESTART_INTERRUPTED_TEXT,
    ReasoningCoalescingTaskStore,
    ValidatingPushNotificationConfigStore,
    initialize_a2a_stores,
    make_sqlite_engine,
)
from a2a_impl.wire import task_id_from_response
from runtime.state import STATE
from scheduler.local import LocalScheduler
from server.interrupted_turns import INTERRUPTED_TOOL_RESULT, InterruptedTurnRecovery
from tests._a2a_app import build_app, drain_handlers

chat_mod = importlib.import_module("server.chat")

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_TEST_POLL_S = 0.05


def _reset_module_globals():
    import server.a2a as a2a
    from graph import steering

    set_terminal_hook(None)
    set_progress_hook(None)
    hitl_routing._ROUTER[0] = None
    steering._reset()
    for store in (a2a._INBOX_TURN_WATCH, a2a._INBOX_EARLY_TERMINALS, a2a._INBOX_RESTORE_OWED):
        store.clear()


@pytest.fixture(autouse=True)
async def _harness_hygiene():
    """Reset every module-global this suite touches, before AND after each test."""
    _reset_module_globals()
    yield
    await drain_handlers()
    _reset_module_globals()


def _route_httpx_to(app, monkeypatch) -> None:
    """Point every httpx.AsyncClient (the scheduler's fire + GetTask reconciliation) at
    the in-process ASGI app — the network edge is all that changes."""

    class _InProc(_REAL_ASYNC_CLIENT):
        def __init__(self, **kw):
            kw.pop("transport", None)
            kw.pop("base_url", None)
            super().__init__(transport=httpx.ASGITransport(app=app), base_url="http://in-proc", **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _InProc)


async def _wait_until(pred, *, timeout: float = 10.0, interval: float = 0.02, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {message}")


def _past_iso(seconds: float = 5.0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _query(path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _execute(path, sql: str, params: tuple = ()) -> None:
    db = sqlite3.connect(str(path))
    try:
        db.execute(sql, params)
        db.commit()
    finally:
        db.close()


def _occurrence(path, job_id: str, next_fire: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM occurrences WHERE job_id = ? AND (? IS NULL OR next_fire = ?)"
    found = _query(path, sql, (job_id, next_fire, next_fire))
    return found[0] if found else None


def _task_status(tmp_path, task_id: str) -> tuple[str, str]:
    """(state, message text) of a durable task row, read straight off the sqlite file."""
    rows = _query(tmp_path / "a2a-tasks.db", "SELECT status FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        return "", ""
    raw = rows[0]["status"]
    blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
    message = blob.get("message") or {}
    text = " ".join(p.get("text", "") for p in message.get("parts") or [])
    return str(blob.get("state") or ""), text


# ── the deterministic model boundary + the process-death boundary ──────────────────

_BLOCKING_TOOLS = {"merge_pr", "risky_step", "cron_step"}
_TOOL_FOR = {
    "SLOW-MODEL": "step_one",
    "WAIT-TOOL": "merge_pr",
    "ONESHOT-TOOL": "risky_step",
    "CRON-TOOL": "cron_step",
}
_MARKERS = (*_TOOL_FOR, "CHAT-HOLD")


class _Coordinator:
    """Deterministic model behavior + the SIGTERM trigger, shared across generations."""

    def __init__(self):
        self.release = False  # True ⇒ nothing blocks: turns run to completion
        self.die = asyncio.Event()  # armed per generation; set ⇒ blocked nodes die
        self.tool_runs: dict[str, int] = {}
        self.views: dict[str, list[list[tuple]]] = {}  # marker -> every model pass's input view

    def rearm(self):
        self.die = asyncio.Event()

    async def killed(self):
        """Block like a slow model/tool call until the simulated SIGTERM, then die
        the way a killed runner does — abruptly, with nothing else persisted."""
        await self.die.wait()
        raise RuntimeError("simulated SIGTERM")


class _KillableSaver:
    """A real ``InMemorySaver`` playing the durable checkpoint store across the
    simulated restart; after the SIGTERM moment it drops writes (a dead process
    persists nothing), so the surviving checkpoint is exactly the committed steps."""

    def __new__(cls):  # build dynamically so the class statement needs no import
        from langgraph.checkpoint.memory import InMemorySaver

        class _Saver(InMemorySaver):
            dead = False

            def put(self, config, *a, **k):
                if self.dead:
                    return config
                return super().put(config, *a, **k)

            def put_writes(self, *a, **k):
                if self.dead:
                    return None
                return super().put_writes(*a, **k)

        return _Saver()


class _KillableTaskStore(ReasoningCoalescingTaskStore):
    """The production durable task store; drops saves after the SIGTERM moment."""

    dead = False

    async def save(self, task, context=None):
        if self.dead:
            return
        await super().save(task, context)


def _build_graph(coord: _Coordinator, saver):
    """A REAL compiled LangGraph over the shared checkpointer. The agent node emits one
    tool call per marker on a fresh prompt and finishes after the tool result; the
    blocking points (a slow model call, a tool call in flight) die on ``coord.die``
    exactly like a SIGTERMed runner. The tools node is named "tools" — the same name
    the production graph uses, which the D7 resume's ``as_node`` seal relies on."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        messages: Annotated[list, add_messages]
        session_id: str

    def marker_of(msgs) -> str:
        text = " ".join(str(getattr(m, "content", "")) for m in msgs if isinstance(m, HumanMessage))
        return next((m for m in _MARKERS if m in text), "")

    def describe(m) -> tuple:
        if isinstance(m, HumanMessage):
            return ("human",)
        if isinstance(m, AIMessage):
            return ("ai", tuple(c["name"] for c in (m.tool_calls or [])))
        if isinstance(m, ToolMessage):
            return ("tool", m.name, str(m.content)[:60])
        return (type(m).__name__,)

    async def agent(state):
        msgs = state["messages"]
        marker = marker_of(msgs)
        coord.views.setdefault(marker, []).append([describe(m) for m in msgs])
        if marker == "CHAT-HOLD":
            await coord.killed()
        last = msgs[-1]
        if isinstance(last, HumanMessage):
            tool = _TOOL_FOR[marker]
            call_id = f"call-{tool}-{len(coord.views[marker])}"
            return {"messages": [AIMessage(content="", tool_calls=[{"name": tool, "args": {}, "id": call_id}])]}
        if marker == "SLOW-MODEL" and not coord.release:
            await coord.killed()  # the slow model call the restart cuts down
        if (
            marker == "WAIT-TOOL"
            and "interrupted by a restart" in str(getattr(last, "content", ""))
            and not coord.tool_runs.get("verify_state")
        ):
            # The loop continues past the interruption report: verify, then finish.
            return {
                "messages": [
                    AIMessage(content="", tool_calls=[{"name": "verify_state", "args": {}, "id": "call-verify-1"}])
                ]
            }
        return {"messages": [AIMessage(content=f"done {marker or 'turn'}")]}

    async def tools(state):
        last = state["messages"][-1]
        out = []
        for call in last.tool_calls:
            coord.tool_runs[call["name"]] = coord.tool_runs.get(call["name"], 0) + 1
            if call["name"] in _BLOCKING_TOOLS and not coord.release:
                await coord.killed()  # the tool call in flight the restart cuts down
            out.append(ToolMessage(content=f"{call['name']} ok", tool_call_id=call["id"], name=call["name"]))
        return {"messages": out}

    def route(state):
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(S)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=saver)


# ── one simulated server-process generation over the shared durable files ──────────


def _wire_state(monkeypatch, *, graph, tmp_path, app, scheduler, task_engine):
    from graph.config import LangGraphConfig
    from graph.decisions import DecisionStore

    wired = {
        "graph": graph,
        "graph_config": LangGraphConfig(),
        "goal_controller": None,
        "decision_store": DecisionStore(tmp_path / "decisions.db"),
        "scheduler": scheduler,
        "inbox_store": None,
        "fastapi_app": app,
        "active_port": 7870,
        "a2a_task_engine": task_engine,
    }
    for name, value in wired.items():
        monkeypatch.setattr(STATE, name, value, raising=False)


def _boot_generation(tmp_path, monkeypatch, coord: _Coordinator, saver, agent: str) -> SimpleNamespace:
    """Fresh server-side objects (store engine, compiled graph, a2a app, scheduler)
    over the SAME durable files — what a process restart actually rebuilds."""
    store = _KillableTaskStore(make_sqlite_engine(str(tmp_path / "a2a-tasks.db")))
    graph = _build_graph(coord, saver)
    app = build_app(chat_mod._chat_langgraph_stream, task_store=store)
    sched = LocalScheduler(
        agent_name=agent, invoke_url="http://127.0.0.1:7870", api_key="k", bearer_token="b", db_dir=tmp_path
    )
    sched._fire_timeout_s = 0.5  # a blocked turn's claim goes 'unknown' fast, handle retained
    _wire_state(monkeypatch, graph=graph, tmp_path=tmp_path, app=app, scheduler=sched, task_engine=store.engine)
    _route_httpx_to(app, monkeypatch)
    return SimpleNamespace(store=store, graph=graph, app=app, sched=sched, interrupted=set())


async def _sigterm(coord: _Coordinator, saver, gen) -> None:
    """The process dies: nothing persists from here on, every runner is torn down."""
    saver.dead = True
    gen.store.dead = True
    coord.die.set()
    await asyncio.sleep(0.1)  # let the zombie turns unwind through their error paths
    await gen.sched.stop()
    await drain_handlers()
    await gen.store.engine.dispose()
    _reset_module_globals()


async def _shutdown(coord: _Coordinator, gen) -> None:
    """Ordinary end-of-test teardown for the final generation."""
    coord.release = True
    coord.die.set()
    await gen.sched.stop()
    await drain_handlers()
    await gen.store.engine.dispose()


async def _restart(tmp_path, monkeypatch, coord: _Coordinator, saver, agent: str, *, release: bool) -> SimpleNamespace:
    """Boot the next generation: real boot reconciliation (spare the scheduler's
    accepted claims, fail the rest), then the scheduler's D7 recovery on start()."""
    coord.release = release
    coord.rearm()
    saver.dead = False
    gen = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    push_store = ValidatingPushNotificationConfigStore(make_sqlite_engine(str(tmp_path / "a2a-push.db")))
    spare = gen.sched.unresolved_task_handles()
    gen.interrupted = await initialize_a2a_stores(gen.store, push_store, spare_task_ids=spare)
    await push_store.engine.dispose()
    gen.sched.note_interrupted_tasks(gen.interrupted, recovery=InterruptedTurnRecovery())
    await gen.sched.start()
    return gen


async def _accepted_unresolved_claim(sched, job_id: str) -> sqlite3.Row:
    return await _wait_until(
        lambda: (lambda row: row if row and row["task_id"] and row["state"] in ("claimed", "unknown") else None)(
            _occurrence(sched.path, job_id)
        ),
        message=f"{job_id}: fire accepted (task handle committed) and unresolved",
    )


# ── r1 + r6: a slow model call in a job session resumes once; chat turns unchanged ──


@pytest.mark.asyncio
async def test_sigterm_during_slow_model_call_resumes_job_from_checkpoint_once(tmp_path, monkeypatch):
    """SIGTERM while a job session's turn sits inside a slow model call (its tool call
    already completed and checkpointed): after restart the turn resumes from the
    checkpoint — against the original thread and task handle, never a new prompt —
    and completes exactly once. The model's resumed view carries the completed tool
    call and its result exactly once each; the tool body never re-runs. An interactive
    chat turn interrupted at the same moment keeps today's behavior: failed at boot."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-slow"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    job = gen1.sched.add_job("SLOW-MODEL digest the overnight queue", _past_iso(), job_id="job-slow")
    owner = job.context_id
    assert owner.startswith("job:")

    # The turn reaches its second (slow) model pass — step_one completed + checkpointed.
    await _wait_until(lambda: len(coord.views.get("SLOW-MODEL") or []) >= 2, message="the slow model call started")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-slow")
    task_id = claim["task_id"]

    # An interactive chat turn is mid-flight at the same moment (r6's control).
    async with _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=gen1.app), base_url="http://in-proc") as client:
        r = await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "chat-1",
                "method": "SendMessage",
                "params": {
                    "configuration": {"returnImmediately": True},
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "CHAT-HOLD summarize this"}],
                        "messageId": "chat-msg-1",
                        "contextId": "chat-decoy",
                    },
                },
            },
        )
    chat_task_id = task_id_from_response(r.json())
    assert chat_task_id
    await _wait_until(lambda: coord.views.get("CHAT-HOLD"), message="the chat turn started")

    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        # Boot classification: only the scheduler-owned task was spared; the
        # interactive chat turn failed with today's restart diagnosis.
        assert gen2.interrupted == {task_id}
        chat_state, chat_text = _task_status(tmp_path, chat_task_id)
        assert chat_state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in chat_text

        # The resumed turn completes and settles through the ORIGINAL handle.
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-slow") or {"state": ""})["state"] == "settled",
            message="the resumed occurrence settled",
        )
        settled = _occurrence(gen2.sched.path, "job-slow")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == task_id
        assert settled["message_id"] == claim["message_id"]  # never a resend
        assert settled["auto_resumes"] == 1
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-slow'")) == 1
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_COMPLETED"
        assert "done SLOW-MODEL" in text
        await _wait_until(lambda: gen2.sched.get_job("job-slow") is None, message="the settled one-shot was removed")

        # The model saw no repeated completed tool calls and no new prompt: the resumed
        # pass's whole view is the original prompt, ONE tool call, ONE result.
        assert coord.views["SLOW-MODEL"][-1] == [
            ("human",),
            ("ai", ("step_one",)),
            ("tool", "step_one", "step_one ok"),
        ]
        assert len(coord.views["SLOW-MODEL"]) == 3  # start, slow pass (killed), resumed pass
        assert coord.tool_runs["step_one"] == 1
    finally:
        await _shutdown(coord, gen2)


# ── r2: a wait: continuation mid-tool-call resumes with the interruption report ────


@pytest.mark.asyncio
async def test_sigterm_during_tool_call_in_wait_continuation_resumes_with_report(tmp_path, monkeypatch):
    """SIGTERM while a ``wait:`` continuation's tool call is in flight: after restart
    it resumes with ONE ToolMessage per dangling call reporting the interruption, the
    model acts on the report (the loop continues) and the turn completes against the
    original handle. The uncertain tool body is never re-run."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-wait"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    ctx = f"job:{uuid.uuid4()}"
    job = gen1.sched.add_job(
        "WAIT-TOOL drive the merge queue loop", _past_iso(), job_id=f"wait:{ctx}", context_id=ctx
    )
    assert job.context_id == ctx

    await _wait_until(lambda: coord.tool_runs.get("merge_pr") == 1, message="the tool call started")
    claim = await _accepted_unresolved_claim(gen1.sched, f"wait:{ctx}")
    task_id = claim["task_id"]

    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, f"wait:{ctx}") or {"state": ""})["state"] == "settled",
            message="the resumed continuation settled",
        )
        settled = _occurrence(gen2.sched.path, f"wait:{ctx}")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == task_id
        assert settled["auto_resumes"] == 1
        state, _ = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_COMPLETED"

        # The dangling call was answered by the interruption report, not re-executed —
        # and the loop CONTINUED: the model verified, then finished.
        assert coord.tool_runs["merge_pr"] == 1
        assert coord.tool_runs["verify_state"] == 1
        resumed_view = coord.views["WAIT-TOOL"][1]  # first pass after the restart
        assert resumed_view[-1][0] == "tool"
        assert resumed_view[-1][1] == "merge_pr"
        assert resumed_view[-1][2] in INTERRUPTED_TOOL_RESULT  # the report, truncated by describe()
        final_view = coord.views["WAIT-TOOL"][-1]
        assert sum(1 for entry in final_view if entry[0] == "human") == 1  # never a new prompt
        assert ("tool", "verify_state", "verify_state ok") in final_view
    finally:
        await _shutdown(coord, gen2)


# ── r3: any other one-shot mid-tool-call → interrupted_needs_operator, not resent ──


@pytest.mark.asyncio
async def test_sigterm_during_tool_call_in_other_one_shot_goes_to_operator(tmp_path, monkeypatch):
    """SIGTERM while an ordinary one-shot job's tool call is in flight: the occurrence
    is marked ``interrupted_needs_operator`` — not ``failed``, never resent, resume
    counter untouched — the job row is held for the operator (S7), and the dead task
    handle is failed with the restart diagnosis so nothing reads as alive forever."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-oneshot"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    gen1.sched.add_job("ONESHOT-TOOL archive the exports", _past_iso(), job_id="job-oneshot")
    await _wait_until(lambda: coord.tool_runs.get("risky_step") == 1, message="the tool call started")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-oneshot")
    task_id = claim["task_id"]

    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-oneshot") or {"state": ""})["state"]
            == "interrupted_needs_operator",
            message="the occurrence went to the operator",
        )
        held = _occurrence(gen2.sched.path, "job-oneshot")
        assert held["task_id"] == task_id
        assert held["message_id"] == claim["message_id"]
        assert held["auto_resumes"] == 0
        assert "operator" in held["diagnostic"]
        # Held for the operator: the scheduling row survives, carrying the diagnosis.
        job_row = gen2.sched.get_job("job-oneshot")
        assert job_row is not None
        assert "operator" in (job_row.last_error or "")
        # The dead handle is terminal with the restart diagnosis — but the OCCURRENCE
        # is the D7 state, not settled-failed.
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text

        # Never resent, never resumed: hold the store open and confirm no new model
        # pass, no tool re-run, no second occurrence.
        await asyncio.sleep(15 * _TEST_POLL_S)
        assert len(coord.views["ONESHOT-TOOL"]) == 1
        assert coord.tool_runs["risky_step"] == 1
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")) == 1
    finally:
        await _shutdown(coord, gen2)


# ── r4: a recurring job's interrupted occurrence is skipped; the next slot fires ───


@pytest.mark.asyncio
async def test_recurring_job_interrupted_occurrence_is_skipped_and_next_slot_fires(tmp_path, monkeypatch):
    """SIGTERM during a recurring job's tool call: the occurrence is recorded
    ``interrupted`` (skipped), its dangling call is sealed on the thread with the
    interruption report, and the next scheduled slot fires and completes normally on
    the same execution session."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-cron"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    gen1.sched.add_job("CRON-TOOL sweep the queue", "* * * * *", job_id="job-cron")
    _execute(gen1.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-cron'", (_past_iso(9),))
    await _wait_until(lambda: coord.tool_runs.get("cron_step") == 1, message="the tool call started")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-cron")
    task_id, slot1 = claim["task_id"], claim["next_fire"]

    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-cron", slot1) or {"state": ""})["state"] == "interrupted",
            message="the interrupted occurrence was skipped",
        )
        skipped = _occurrence(gen2.sched.path, "job-cron", slot1)
        assert skipped["auto_resumes"] == 0
        assert "skipped" in skipped["diagnostic"]
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text

        # The next slot runs normally: rewind it to due and watch a FRESH occurrence
        # fire and settle on the same execution session.
        slot2 = _past_iso(2)
        _execute(gen2.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-cron'", (slot2,))
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-cron", slot2) or {"state": ""})["state"] == "settled",
            message="the next slot fired and settled",
        )
        fresh = _occurrence(gen2.sched.path, "job-cron", slot2)
        assert fresh["task_state"] == "completed"
        assert fresh["message_id"] != claim["message_id"]  # a new occurrence, not a resend
        assert coord.tool_runs["cron_step"] == 2
        # The skipped occurrence's dangling call was sealed: the next fire's model view
        # opens with the interruption report answering the gen-1 tool call.
        next_fire_view = coord.views["CRON-TOOL"][1]
        assert ("tool", "cron_step", INTERRUPTED_TOOL_RESULT[:60]) in next_fire_view
        assert gen2.sched.get_job("job-cron") is not None  # the recurring job lives on
    finally:
        await _shutdown(coord, gen2)


# ── r5: a second interruption of the same occurrence is never auto-resumed ─────────


@pytest.mark.asyncio
async def test_second_interruption_of_same_occurrence_is_never_auto_resumed(tmp_path, monkeypatch):
    """The automatic resume is capped at one per occurrence, counted in the claim
    transaction BEFORE the resume runs: a job whose resumed turn is interrupted again
    (the crash-loop case) goes to ``interrupted_needs_operator`` on the next boot —
    the model is never driven a third time."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-twice"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    gen1.sched.add_job("SLOW-MODEL rebuild the index", _past_iso(), job_id="job-twice")
    await _wait_until(lambda: len(coord.views.get("SLOW-MODEL") or []) >= 2, message="the slow model call started")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-twice")
    task_id = claim["task_id"]

    await _sigterm(coord, saver, gen1)

    # Restart #1: the resume is claimed (counted on the row) and blocks in the same
    # slow model call — then the server dies AGAIN, mid-resume.
    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=False)
    assert gen2.interrupted == {task_id}
    await _wait_until(lambda: len(coord.views["SLOW-MODEL"]) >= 3, message="the automatic resume started")
    assert _occurrence(gen2.sched.path, "job-twice")["auto_resumes"] == 1  # counted BEFORE the resume ran
    saver.dead = True
    gen2.store.dead = True
    # A killed process writes nothing and unwinds nothing: tear the resume task down
    # by cancellation, exactly like the event loop dying.
    for t in list(gen2.sched._fire_tasks):
        t.cancel()
    await gen2.sched.stop()
    await drain_handlers()
    await gen2.store.engine.dispose()
    _reset_module_globals()

    # Restart #2: the same occurrence is interrupted again — it goes to the operator.
    gen3 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen3.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen3.sched.path, "job-twice") or {"state": ""})["state"]
            == "interrupted_needs_operator",
            message="the twice-interrupted occurrence went to the operator",
        )
        held = _occurrence(gen3.sched.path, "job-twice")
        assert held["auto_resumes"] == 1  # the cap: one resume, never two
        assert "after one automatic resume" in held["diagnostic"]
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text
        # No third model pass, no tool re-run, nothing resent.
        await asyncio.sleep(15 * _TEST_POLL_S)
        assert len(coord.views["SLOW-MODEL"]) == 3
        assert coord.tool_runs["step_one"] == 1
    finally:
        await _shutdown(coord, gen3)
