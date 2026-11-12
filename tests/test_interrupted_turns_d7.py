"""Interrupted turns D7 S6 (ADR 0129, #5149): classify from checkpoint on boot and
auto-resume the safe cases.

Restart-interruption acceptance over the PRODUCTION paths and temporary persistent
stores: a real ``LocalScheduler`` over a temp ``jobs.db``, the a2a-sdk handler +
executor + ``_chat_langgraph_stream``, the durable sqlite task store with
``initialize_a2a_stores`` boot reconciliation, a real compiled LangGraph over a
checkpointer shared across the simulated restart, and the production
``InterruptedTurnRecovery``. Faked: only the model (deterministic graph nodes) and
the process death — at the "SIGTERM" moment the checkpointer and task store drop
writes, exactly what a killed process can no longer persist, so the zombie turn's
error paths cannot retro-write state the real crash would have lost. The scenarios
map 1:1 onto the card's acceptance criteria.
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
    mark_restart_spared_tasks,
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
    mark_restart_spared_tasks(())  # the D7 janitor-exemption registry is module state
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
    "ASK-HITL": "prep_step",
    "ASK-DECISION": "prep_step",
}
# What the post-restart resumed model pass asks for, via a real LangGraph ``interrupt()``
# in the tools node — the same mechanics as the production ask/decision tools.
_HITL_FOR = {
    "ASK-HITL": {"question": "May I proceed with the merge queue?"},
    "ASK-DECISION": {"kind": "decision", "question": "Which environment?", "options": ["staging", "prod"]},
}
_MARKERS = (*_TOOL_FOR, "CHAT-HOLD")


class _Coordinator:
    """Deterministic model behavior + the SIGTERM trigger, shared across generations."""

    def __init__(self):
        self.release = False  # True ⇒ nothing blocks: turns run to completion
        self.die = asyncio.Event()  # armed per generation; set ⇒ blocked nodes die
        self.tool_runs: dict[str, int] = {}
        self.views: dict[str, list[list[tuple]]] = {}  # marker -> every model pass's input view
        self.meta: dict[str, list[dict]] = {}  # marker -> every model pass's request metadata

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
    from langgraph.types import interrupt

    from graph.middleware.request_context import current_request_metadata

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
        # What the production projection/prompt-capture middleware would read on this
        # pass — the live fire's turn context, which a D7 resume must rebuild.
        coord.meta.setdefault(marker, []).append(dict(current_request_metadata()))
        if marker == "CHAT-HOLD":
            await coord.killed()
        last = msgs[-1]
        if isinstance(last, HumanMessage):
            tool = _TOOL_FOR[marker]
            call_id = f"call-{tool}-{len(coord.views[marker])}"
            return {"messages": [AIMessage(content="", tool_calls=[{"name": tool, "args": {}, "id": call_id}])]}
        if marker in ("SLOW-MODEL", *_HITL_FOR) and not coord.release:
            await coord.killed()  # the slow model call the restart cuts down
        if marker in _HITL_FOR and not coord.tool_runs.get("hitl_step"):
            # The RESUMED pass asks: the pause happens mid-resume, not before the kill.
            return {
                "messages": [AIMessage(content="", tool_calls=[{"name": "hitl_step", "args": {}, "id": "call-ask-1"}])]
            }
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
            if call["name"] == "hitl_step":
                # A real checkpointed interrupt, as the production HITL tools park.
                answer = interrupt(_HITL_FOR[marker_of(state["messages"])])
                out.append(ToolMessage(content=str(answer), tool_call_id=call["id"], name=call["name"]))
                continue
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


async def _accepted_unresolved_claim(sched, job_id: str, next_fire: str | None = None) -> sqlite3.Row:
    return await _wait_until(
        lambda: (lambda row: row if row and row["task_id"] and row["state"] in ("claimed", "unknown") else None)(
            _occurrence(sched.path, job_id, next_fire)
        ),
        message=f"{job_id}: fire accepted (task handle committed) and unresolved",
    )


def _stamp_prompts_old(monkeypatch, iso: str = "2026-01-01T00:00:00Z") -> None:
    """Every prompt written from here on carries ``protoagent_sent_at`` = ``iso`` — the
    production spacing, where the PREVIOUS slot's turn ran minutes-to-hours before this
    occurrence's due time. The stale-checkpoint anchor is second-resolution, and a test
    would otherwise pack both slots into the same wall-clock second."""
    from graph import compaction_engine

    monkeypatch.setattr(compaction_engine, "sent_at_kwargs", lambda: {compaction_engine.SENT_AT_KWARG: iso})


async def _interrupted_first_fire(tmp_path, monkeypatch, agent, *, prompt, job_id, ready, context_id=None):
    """One killed first generation: boot, fire ``job_id``, wait for ``ready(coord)``
    (the scenario's blocking point), capture the accepted claim, SIGTERM."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord, saver = _Coordinator(), _KillableSaver()
    gen = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen.sched.start()
    gen.sched.add_job(prompt, _past_iso(), job_id=job_id, context_id=context_id)
    await _wait_until(lambda: ready(coord), message=f"{job_id}: the fire reached its blocking point")
    claim = await _accepted_unresolved_claim(gen.sched, job_id)
    await _sigterm(coord, saver, gen)
    return coord, saver, claim


async def _quiesce_polls(sched, *, polls: int = 15) -> None:
    """Event-driven replacement for a real sleep when proving "never resent": wait
    until the live claim loop has polled ``polls`` more times — each poll is one
    opportunity to re-claim — then let the caller assert nothing changed."""
    seen = 0
    orig = sched._claim_due_jobs

    def counting(now):
        nonlocal seen
        seen += 1
        return orig(now)

    sched._claim_due_jobs = counting
    try:
        await _wait_until(lambda: seen >= polls, message=f"{polls} scheduler polls")
    finally:
        del sched._claim_due_jobs  # restore the class method


# ── r1 + r6: a slow model call in a job session resumes once; chat turns unchanged ──


@pytest.mark.asyncio
async def test_sigterm_during_slow_model_call_resumes_job_from_checkpoint_once(tmp_path, monkeypatch):
    """SIGTERM inside a slow model call (its tool call already checkpointed): after
    restart the turn resumes from the checkpoint — original thread and task handle,
    never a new prompt, no repeated tool calls — and completes exactly once. An
    interactive chat turn interrupted at the same moment still fails at boot (r6)."""
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
        # Boot classification: only the scheduler-owned task was spared.
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

        # No repeated tool calls, no new prompt: one prompt, ONE call, ONE result.
        assert coord.views["SLOW-MODEL"][-1] == [
            ("human",),
            ("ai", ("step_one",)),
            ("tool", "step_one", "step_one ok"),
        ]
        assert len(coord.views["SLOW-MODEL"]) == 3  # start, slow pass (killed), resumed pass
        assert coord.tool_runs["step_one"] == 1

        # The resume rebuilt the fire's turn context: the resumed pass reads the SAME
        # request metadata the live fire's passes carried, so projection's
        # ``_is_firing_now`` still recognises the job as THIS turn's own fire (not a
        # pending schedule) and prompt capture keeps its ``a2a.task_id`` link.
        keys = ("scheduler_job_id", "scheduler_kind", "origin", "a2a.task_id")
        live_meta, resumed_meta = coord.meta["SLOW-MODEL"][0], coord.meta["SLOW-MODEL"][-1]
        assert {k: resumed_meta.get(k) for k in keys} == {k: live_meta.get(k) for k in keys}
        assert resumed_meta["scheduler_job_id"] == "job-slow"
        assert resumed_meta["a2a.task_id"] == task_id
        assert resumed_meta["origin"] == "scheduler"
    finally:
        await _shutdown(coord, gen2)


# ── r2: a wait: continuation mid-tool-call resumes with the interruption report ────


@pytest.mark.asyncio
async def test_sigterm_during_tool_call_in_wait_continuation_resumes_with_report(tmp_path, monkeypatch):
    """SIGTERM mid tool call in a ``wait:`` continuation: after restart it resumes with
    ONE ToolMessage per dangling call reporting the interruption, the loop continues,
    and the turn completes against the original handle. The tool body never re-runs."""
    agent = "d7-wait"
    ctx = f"job:{uuid.uuid4()}"
    coord, saver, claim = await _interrupted_first_fire(
        tmp_path, monkeypatch, agent, prompt="WAIT-TOOL drive the merge queue loop",
        job_id=f"wait:{ctx}", context_id=ctx, ready=lambda c: c.tool_runs.get("merge_pr") == 1,
    )
    task_id = claim["task_id"]

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

        # The dangling call got the report, not a re-run — and the loop CONTINUED.
        assert coord.tool_runs["merge_pr"] == 1
        assert coord.tool_runs["verify_state"] == 1
        resumed_view = coord.views["WAIT-TOOL"][1]  # first pass after the restart
        assert resumed_view[-1][0] == "tool"
        assert resumed_view[-1][1] == "merge_pr"
        assert resumed_view[-1][2] in INTERRUPTED_TOOL_RESULT  # the report, truncated by describe()
        final_view = coord.views["WAIT-TOOL"][-1]
        assert sum(1 for entry in final_view if entry[0] == "human") == 1  # never a new prompt
        assert ("tool", "verify_state", "verify_state ok") in final_view
        assert coord.meta["WAIT-TOOL"][1]["scheduler_job_id"] == f"wait:{ctx}"  # the resumed pass's turn context
    finally:
        await _shutdown(coord, gen2)


# ── r3: any other one-shot mid-tool-call → interrupted_needs_operator, not resent ──


@pytest.mark.asyncio
async def test_sigterm_during_tool_call_in_other_one_shot_goes_to_operator(tmp_path, monkeypatch):
    """SIGTERM mid tool call in an ordinary one-shot: ``interrupted_needs_operator`` —
    not failed, never resent, resume counter untouched — the job row held for S7, the
    dead task handle failed with the restart diagnosis."""
    agent = "d7-oneshot"
    coord, saver, claim = await _interrupted_first_fire(
        tmp_path, monkeypatch, agent, prompt="ONESHOT-TOOL archive the exports",
        job_id="job-oneshot", ready=lambda c: c.tool_runs.get("risky_step") == 1,
    )
    task_id = claim["task_id"]

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
        # The dead handle is terminal; the OCCURRENCE keeps the D7 state.
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text

        # Never resent, never resumed: no new model pass, re-run or occurrence.
        await _quiesce_polls(gen2.sched)
        assert len(coord.views["ONESHOT-TOOL"]) == 1
        assert coord.tool_runs["risky_step"] == 1
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")) == 1
    finally:
        await _shutdown(coord, gen2)


# ── r4: a recurring job's interrupted occurrence is skipped; the next slot fires ───


@pytest.mark.asyncio
async def test_recurring_job_interrupted_occurrence_is_skipped_and_next_slot_fires(tmp_path, monkeypatch):
    """SIGTERM during a recurring job's tool call: the occurrence is ``interrupted``
    (skipped), its dangling call sealed on the thread with the interruption report,
    and the next slot fires and completes normally on the same execution session."""
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

        # The next slot runs normally: a FRESH occurrence on the same session.
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
        # Sealed: the next fire's view opens with the report answering the gen-1 call.
        next_fire_view = coord.views["CRON-TOOL"][1]
        assert ("tool", "cron_step", INTERRUPTED_TOOL_RESULT[:60]) in next_fire_view
        assert gen2.sched.get_job("job-cron") is not None  # the recurring job lives on
    finally:
        await _shutdown(coord, gen2)


# ── a kill before the fire's prompt checkpointed: the stale thread is never "clean" ──


@pytest.mark.asyncio
async def test_kill_before_prompt_checkpointed_never_completes_with_previous_reply(tmp_path, monkeypatch):
    """A fire killed after its task handle committed (``returnImmediately``) but BEFORE
    the graph checkpointed its prompt leaves the reused ``job:`` thread holding the
    PREVIOUS slot's finished turn — identical prompt text, older timestamp. That
    checkpoint is stale (case c), never clean: the occurrence is skipped, the task
    fails with the restart diagnosis — NOT completed with the prior slot's reply —
    and the next slot fires normally."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    _stamp_prompts_old(monkeypatch)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-stale"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    coord.release = True  # slot 1 runs to completion: the durable thread ends in a finished turn
    gen1.sched.add_job("CRON-TOOL sweep the queue", "* * * * *", job_id="job-stale")
    _execute(gen1.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-stale'", (_past_iso(9),))
    await _wait_until(
        lambda: (_occurrence(gen1.sched.path, "job-stale") or {"state": ""})["state"] == "settled",
        message="slot 1 completed",
    )

    # Slot 2: the handle commits, but NOTHING of the new turn reaches the checkpointer —
    # the kill window between the task save and the graph's first checkpoint write.
    saver.dead = True
    coord.release = False
    slot2 = _past_iso(2)
    _execute(gen1.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-stale'", (slot2,))
    await _wait_until(lambda: coord.tool_runs.get("cron_step") == 2, message="slot 2's zombie turn blocked")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-stale", slot2)
    task_id = claim["task_id"]
    # Park the root far out: a minute boundary must not fire a fresh slot mid-assert
    # after the restart — slot 3 is made due explicitly below.
    far = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _execute(gen1.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-stale'", (far,))
    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-stale", slot2) or {"state": ""})["state"] == "interrupted",
            message="the stale occurrence was skipped, never resumed",
        )
        assert _occurrence(gen2.sched.path, "job-stale", slot2)["auto_resumes"] == 0  # never auto-resumed
        assert coord.tool_runs["cron_step"] == 2  # slot 1 + the zombie — the resume never drove the model
        # THE regression: the prior slot's reply must never settle this fire's task.
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text
        assert "done CRON-TOOL" not in text

        # The next slot fires and completes normally on the same execution session.
        slot3 = _past_iso(1)
        _execute(gen2.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-stale'", (slot3,))
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-stale", slot3) or {"state": ""})["state"] == "settled",
            message="the next slot fired and settled",
        )
        assert _occurrence(gen2.sched.path, "job-stale", slot3)["task_state"] == "completed"
    finally:
        await _shutdown(coord, gen2)


@pytest.mark.asyncio
async def test_stale_wait_continuation_goes_to_operator_not_completed(tmp_path, monkeypatch):
    """The same pre-checkpoint kill window on a ``wait:`` continuation (a one-shot):
    the reused session thread still ends in the previous loop turn, so nothing of this
    fire is recoverable — ``interrupted_needs_operator``, never auto-resumed, never
    completed with the previous turn's reply."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    _stamp_prompts_old(monkeypatch)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-stale-wait"
    ctx = f"job:{uuid.uuid4()}"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    coord.release = True  # loop turn 1 completes; its spent one-shot row is removed
    gen1.sched.add_job("WAIT-TOOL drive the merge queue loop", _past_iso(), job_id=f"wait:{ctx}", context_id=ctx)
    await _wait_until(lambda: gen1.sched.get_job(f"wait:{ctx}") is None, message="loop turn 1 settled")

    saver.dead = True  # turn 2's prompt will never reach the checkpointer
    coord.release = False
    gen1.sched.add_job("WAIT-TOOL drive the merge queue loop", _past_iso(), job_id=f"wait:{ctx}", context_id=ctx)
    await _wait_until(lambda: coord.tool_runs.get("merge_pr") == 2, message="turn 2's zombie blocked")
    claim = await _wait_until(
        lambda: next(
            (
                r
                for r in _query(gen1.sched.path, "SELECT * FROM occurrences WHERE job_id = ?", (f"wait:{ctx}",))
                if r["task_id"] and r["state"] in ("claimed", "unknown")
            ),
            None,
        ),
        message="turn 2 accepted (task handle committed) and unresolved",
    )
    task_id = claim["task_id"]
    await _sigterm(coord, saver, gen1)

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, f"wait:{ctx}", claim["next_fire"]) or {"state": ""})["state"]
            == "interrupted_needs_operator",
            message="the stale continuation went to the operator",
        )
        held = _occurrence(gen2.sched.path, f"wait:{ctx}", claim["next_fire"])
        assert held["auto_resumes"] == 0  # a stale checkpoint is never auto-resumed
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text
        assert "done WAIT-TOOL" not in text  # the previous loop turn's reply never settles this fire
        assert coord.tool_runs["merge_pr"] == 2  # turn 1 + the zombie — the resume never drove the model
    finally:
        await _shutdown(coord, gen2)


# ── r5: a second interruption of the same occurrence is never auto-resumed ─────────


@pytest.mark.asyncio
async def test_second_interruption_of_same_occurrence_is_never_auto_resumed(tmp_path, monkeypatch):
    """The automatic resume is capped at one per occurrence, counted BEFORE the resume
    runs: a resumed turn interrupted again (the crash-loop case) goes to
    ``interrupted_needs_operator`` — the model is never driven a third time."""
    agent = "d7-twice"
    coord, saver, claim = await _interrupted_first_fire(
        tmp_path, monkeypatch, agent, prompt="SLOW-MODEL rebuild the index",
        job_id="job-twice", ready=lambda c: len(c.views.get("SLOW-MODEL") or []) >= 2,
    )
    task_id = claim["task_id"]

    # Restart #1: the resume is claimed, blocks again, and the server dies mid-resume.
    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=False)
    assert gen2.interrupted == {task_id}
    await _wait_until(lambda: len(coord.views["SLOW-MODEL"]) >= 3, message="the automatic resume started")
    assert _occurrence(gen2.sched.path, "job-twice")["auto_resumes"] == 1  # counted BEFORE the resume ran
    saver.dead = True
    gen2.store.dead = True
    # Tear the resume down by cancellation, exactly like the event loop dying.
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
        await _quiesce_polls(gen3.sched)
        assert len(coord.views["SLOW-MODEL"]) == 3
        assert coord.tool_runs["step_one"] == 1
    finally:
        await _shutdown(coord, gen3)


# ── the resumed turn's HITL rule: autonomous auto-answer; decisions park durably ────


@pytest.mark.asyncio
async def test_resumed_turn_auto_answers_hitl_instead_of_parking(tmp_path, monkeypatch):
    """A D7 resume follows the autonomous-origin HITL rule: an ask_human-style pause
    hit mid-resume is answered with the no-operator sentinel and the turn completes —
    never parked ``input_required``, which would hold the owner's claim forever (D6)."""
    agent = "d7-ask"
    coord, saver, claim = await _interrupted_first_fire(
        tmp_path, monkeypatch, agent, prompt="ASK-HITL merge the queue",
        job_id="job-ask", ready=lambda c: len(c.views.get("ASK-HITL") or []) >= 2,
    )
    task_id = claim["task_id"]

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-ask") or {"state": ""})["state"] == "settled",
            message="the resumed asking turn settled",
        )
        settled = _occurrence(gen2.sched.path, "job-ask")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == task_id
        state, text = _task_status(tmp_path, task_id)
        assert state == "TASK_STATE_COMPLETED"
        assert "done ASK-HITL" in text
        # The model received the sentinel as the ask's answer and finished on it.
        final_view = coord.views["ASK-HITL"][-1]
        assert any(e[0] == "tool" and e[1] == "hitl_step" and "no interactive operator" in e[2] for e in final_view)
    finally:
        await _shutdown(coord, gen2)


@pytest.mark.asyncio
async def test_resumed_turn_parks_on_a_decision_with_a_durable_record(tmp_path, monkeypatch):
    """``request_decision`` is the ONE pause a resumed autonomous turn parks on (#4725):
    it parks WITH its durable decisions-ledger record, the task handle held
    ``input_required`` and the claim unresolved until the decision resolves."""
    agent = "d7-decide"
    coord, saver, claim = await _interrupted_first_fire(
        tmp_path, monkeypatch, agent, prompt="ASK-DECISION choose the rollout",
        job_id="job-decide", ready=lambda c: len(c.views.get("ASK-DECISION") or []) >= 2,
    )
    task_id = claim["task_id"]
    owner = json.loads(claim["job_snapshot"])["context_id"]

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: _task_status(tmp_path, task_id)[0] == "TASK_STATE_INPUT_REQUIRED",
            message="the resumed turn parked on the decision",
        )
        _, text = _task_status(tmp_path, task_id)
        assert "Which environment?" in text
        # The durable ledger record exists, pending, keyed to this execution session.
        records = [r for r in STATE.decision_store.list(session_id=owner) if r.pending]
        assert [r.question for r in records] == ["Which environment?"]
        assert records[0].thread_id  # answerable: the record points back at the checkpoint
        # The claim stays unresolved and nothing re-fires while the decision pends.
        await _quiesce_polls(gen2.sched)
        occ = _occurrence(gen2.sched.path, "job-decide")
        assert occ["state"] in ("claimed", "unknown")
        assert len(coord.views["ASK-DECISION"]) == 3  # start, killed pass, resumed ask
    finally:
        await _shutdown(coord, gen2)


# ── a parked continuation no longer counts as the owner's outstanding work ──────────


@pytest.mark.asyncio
async def test_parked_continuation_unblocks_the_owner_for_new_fires(tmp_path, monkeypatch):
    """A continuation disposed ``interrupted_needs_operator`` keeps its jobs row as the
    S7 handle but stops counting as the owner's outstanding work (D7 'unblock the
    owner for NEW work'): the recurring root claims and completes its next slot, and
    missed-fire recovery rolls a stale root forward instead of pinning it."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    saver = _KillableSaver()
    agent = "d7-owner"

    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    root = gen1.sched.add_job("CRON-TOOL sweep the queue", "* * * * *", job_id="job-root")
    owner = root.context_id
    far = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _execute(gen1.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-root'", (far,))
    cont_id = f"wait:{owner}"
    gen1.sched.add_job("SLOW-MODEL continue the loop", _past_iso(), job_id=cont_id, context_id=owner)
    await _wait_until(lambda: len(coord.views.get("SLOW-MODEL") or []) >= 2, message="the slow model call started")
    claim = await _accepted_unresolved_claim(gen1.sched, cont_id)
    task_id = claim["task_id"]

    await _sigterm(coord, saver, gen1)
    # The occurrence's one automatic resume is already spent (the r5 crash-loop shape).
    _execute(gen1.sched.path, "UPDATE occurrences SET auto_resumes = 1 WHERE job_id = ?", (cont_id,))

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        assert gen2.interrupted == {task_id}
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, cont_id) or {"state": ""})["state"] == "interrupted_needs_operator",
            message="the continuation went to the operator",
        )
        assert gen2.sched.get_job(cont_id) is not None  # the S7 handle survives

        # A >24h-stale root rolls FORWARD instead of pinning behind the parked row.
        stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        _execute(gen2.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-root'", (stale,))
        gen2.sched._recover_missed_fires()
        rolled = gen2.sched.get_job("job-root")
        assert rolled is not None
        assert rolled.next_fire > datetime.now(UTC).isoformat()

        # Live-claim: a due root slot fires and settles while the parked row stays put.
        slot = _past_iso(2)
        _execute(gen2.sched.path, "UPDATE jobs SET next_fire = ? WHERE id = 'job-root'", (slot,))
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-root", slot) or {"state": ""})["state"] == "settled",
            message="the root's next slot fired and settled",
        )
        assert _occurrence(gen2.sched.path, "job-root", slot)["task_state"] == "completed"
        held = _occurrence(gen2.sched.path, cont_id)
        assert held["state"] == "interrupted_needs_operator"
        assert held["auto_resumes"] == 1  # untouched: a second resume was never claimed
        assert held["task_id"] == task_id  # the original handle — nothing was resent
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = ?", (cont_id,))) == 1
    finally:
        await _shutdown(coord, gen2)
