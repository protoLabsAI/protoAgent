"""Background job sessions S4 — execution-grounded acceptance (ADR 0129 D6, #5110).

Concurrency isolation, restart/migration safety and Work decision access through the
PRODUCTION paths over temporary persistent stores. Real: ``LocalScheduler`` over a temp
``jobs.db`` (claim / serialize / settle / reconcile / ``job_sessions.migrate``), the
a2a-sdk handler + ``ProtoAgentExecutor`` + the production ``_chat_langgraph_stream``
turn driver (per-thread locks, decision park), ``install_parked_task_routing``, the
``DecisionStore`` ledger + the ``/api/decisions`` routes the Work panel calls, the
``InboxStore`` and the production inbox now-fire. Faked: only the model (deterministic
LangGraph nodes gated on asyncio events) and the network edge (httpx → in-process ASGI).

The dispatch-latency bound: the scheduler claims due work every ``_POLL_INTERVAL_S``
(1.0 s in production), so an unrelated due job / inbox now-item normally STARTS within
one poll tick plus turn startup. The tests scale the interval to 50 ms and allow
``_DISPATCH_BOUND_TICKS`` = 40 ticks (2.0 s wall clock) so CI scheduling noise cannot
flake the bound while a genuinely blocked dispatch (waiting on the held turns) fails it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import httpx
import pytest
from fastapi import FastAPI

import scheduler.local as scheduler_local
from a2a_impl import hitl_routing
from a2a_impl.executor import set_progress_hook, set_terminal_hook
from events import ACTIVITY_CONTEXT
from graph.decisions import DecisionStore
from operator_api import decision_routes
from runtime.state import STATE
from scheduler.local import LocalScheduler
from tests._a2a_app import build_app, drain_handlers

chat_mod = importlib.import_module("server.chat")

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# Scaled poll cadence + the documented start-latency bound derived from it (see module
# docstring): normal dispatch latency is ONE poll tick; the assertion allows 40.
_TEST_POLL_S = 0.05
_DISPATCH_BOUND_TICKS = 40
_DISPATCH_BOUND_S = _TEST_POLL_S * _DISPATCH_BOUND_TICKS


def _reset_module_globals():
    import server.a2a as a2a
    from graph import steering

    set_terminal_hook(None)
    set_progress_hook(None)
    hitl_routing._ROUTER[0] = None
    steering._QUEUES.clear()
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
    """Point every httpx.AsyncClient (fire + GetTask, the answer's loopback resume, the
    inbox now-fire) at the in-process ASGI app — the network edge is all that changes."""

    class _InProc(_REAL_ASYNC_CLIENT):
        def __init__(self, **kw):
            kw.pop("transport", None)
            kw.pop("base_url", None)
            super().__init__(transport=httpx.ASGITransport(app=app), base_url="http://in-proc", **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _InProc)


def _make_scheduler(tmp_path, agent: str) -> LocalScheduler:
    return LocalScheduler(
        agent_name=agent,
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
    )


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


def _rows(path, table: str) -> list[tuple]:
    return [tuple(r) for r in _query(path, f"SELECT * FROM {table} ORDER BY 1, 2")]  # noqa: S608 — test-owned names


def _occurrence(path, job_id: str, next_fire: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM occurrences WHERE job_id = ? AND (? IS NULL OR next_fire = ?)"
    found = _query(path, sql, (job_id, next_fire, next_fire))
    return found[0] if found else None


def _unresolved_occurrences(path) -> list[tuple]:
    return [tuple(r) for r in _query(path, "SELECT job_id, state FROM occurrences WHERE state IN ('claimed', 'unknown')")]

# ── the deterministic model boundary ──────────────────────────────────────────────


class _Coordinator:
    """Monotonic start stamps + asyncio events, set from inside the graph nodes."""

    def __init__(self):
        self.started: dict[str, float] = {}
        self.sessions: dict[str, str] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.release = asyncio.Event()  # gates the long multi-step turn's second step
        self.resumed: list[tuple[str, str]] = []  # (execution session, decision answer)

    def event(self, key: str) -> asyncio.Event:
        return self.events.setdefault(key, asyncio.Event())

    def stamp(self, key: str, session: str) -> None:
        self.started.setdefault(key, time.monotonic())
        self.sessions.setdefault(key, session)
        self.event(key).set()

    async def wait(self, key: str, timeout: float) -> None:
        await asyncio.wait_for(self.event(key).wait(), timeout)


def _marker(state) -> str:
    text = " ".join(str(getattr(m, "content", "")) for m in state.get("messages") or [])
    return next((m for m in ("PARK-DECISION", "LONG-TURN", "QUICK-SCHEDULE", "INBOX-ITEM") if m in text), "")


def _build_graph(coord: _Coordinator):
    """A REAL compiled LangGraph (real checkpointer, real interrupt) with a deterministic
    model node: ``PARK-DECISION`` parks on the real ``request_decision`` interrupt;
    ``LONG-TURN`` is a controlled multi-step turn (second step gated on ``coord.release``)."""
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from tools.lg_tools import request_decision

    class S(TypedDict):
        messages: list
        session_id: str

    # Un-annotated callables: postponed annotations break hint inference on a nested TypedDict.
    async def model(state):
        marker = _marker(state)
        session = state.get("session_id", "")
        if marker == "PARK-DECISION":
            # interrupt() on the first pass; the operator's answer on resume.
            chosen = request_decision.invoke({"question": "Ship the nightly batch?", "options": ["ship", "hold"]})
            coord.resumed.append((session, chosen))
            return {"messages": [AIMessage(content=f"Decision: {chosen}")]}
        if marker:
            coord.stamp(marker, session)
        return {"messages": [AIMessage(content=f"handled {marker or 'turn'}")]}

    async def held_step(state):
        coord.stamp("LONG-TURN-STEP2", state.get("session_id", ""))
        await asyncio.wait_for(coord.release.wait(), timeout=30)
        return {"messages": [AIMessage(content="long multi-step turn finished")]}

    def route(state):
        return "held_step" if _marker(state) == "LONG-TURN" else END

    builder = StateGraph(S)
    builder.add_node("model", model)
    builder.add_node("held_step", held_step)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route)
    builder.add_edge("held_step", END)
    return builder.compile(checkpointer=InMemorySaver())


def _wire_state(monkeypatch, *, graph, tmp_path, app, scheduler=None, inbox_store=None):
    from graph.config import LangGraphConfig

    store = DecisionStore(tmp_path / "decisions.db")
    wired = {"graph": graph, "graph_config": LangGraphConfig(), "goal_controller": None, "decision_store": store,
             "scheduler": scheduler, "inbox_store": inbox_store, "fastapi_app": app, "active_port": 7870}
    for name, value in wired.items():
        monkeypatch.setattr(STATE, name, value, raising=False)
    return store

# ── 1. concurrency: a parked decision + a long multi-step turn hold NOTHING else ──


@pytest.mark.asyncio
async def test_parked_decision_and_long_turn_do_not_delay_unrelated_dispatch(tmp_path, monkeypatch):
    """One job parks on a REAL graph decision interrupt, another sits mid-way through a
    controlled long multi-step turn. An unrelated scheduled one-shot and an inbox
    now-item both START within ``_DISPATCH_BOUND_S`` while both held jobs stay occupied.
    The parked decision is then listed fleet-wide over the REAL ``/api/decisions``
    routes and answered WITHOUT opening its session; only the owning job resumes."""
    import server.a2a as a2a
    from inbox import InboxStore

    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord = _Coordinator()
    app = build_app(chat_mod._chat_langgraph_stream)
    router = hitl_routing.install_parked_task_routing(app.state.a2a_handler)
    assert router is not None
    inbox = InboxStore(str(tmp_path / "inbox.db"))
    scheduler = _make_scheduler(tmp_path, "iso-concurrency")
    ledger = _wire_state(
        monkeypatch, graph=_build_graph(coord), tmp_path=tmp_path, app=app, scheduler=scheduler, inbox_store=inbox
    )
    _route_httpx_to(app, monkeypatch)

    op_app = FastAPI()
    decision_routes.register_decision_routes(op_app)

    try:
        await scheduler.start()
        parked = scheduler.add_job(
            "PARK-DECISION ship the nightly batch", _past_iso(), job_id="job-decision",
            origin_session="chat-origin-a",
        )
        long_turn = scheduler.add_job("LONG-TURN crunch the backlog", _past_iso(), job_id="job-long")
        assert parked.context_id.startswith("job:") and long_turn.context_id.startswith("job:")
        assert parked.context_id != long_turn.context_id

        # Both held jobs are OCCUPIED: the decision durably parked, the long turn inside step 2.
        await _wait_until(
            lambda: [d for d in ledger.list(state="pending") if d.session_id == parked.context_id],
            message="the decision job parked on its interrupt",
        )
        await coord.wait("LONG-TURN-STEP2", timeout=10)

        # While they hold, dispatch unrelated work through the production producers.
        unrelated = scheduler.add_job("QUICK-SCHEDULE unrelated sweep", _past_iso(), job_id="job-unrelated")
        item = inbox.add("INBOX-ITEM triage the pager", priority="now", source="pager")
        t0 = time.monotonic()
        assert await a2a._fire_activity_from_inbox(item) is True

        await coord.wait("QUICK-SCHEDULE", timeout=_DISPATCH_BOUND_S)
        await coord.wait("INBOX-ITEM", timeout=_DISPATCH_BOUND_S)
        schedule_latency = coord.started["QUICK-SCHEDULE"] - t0
        inbox_latency = coord.started["INBOX-ITEM"] - t0
        assert schedule_latency <= _DISPATCH_BOUND_S, f"scheduled fire started after {schedule_latency:.3f}s"
        assert inbox_latency <= _DISPATCH_BOUND_S, f"inbox now-item started after {inbox_latency:.3f}s"

        # …on their OWN isolated sessions while both held jobs were still occupied
        # (decision pending, release not yet granted, both claims open).
        assert unrelated.context_id.startswith("job:")
        assert coord.sessions["QUICK-SCHEDULE"] == unrelated.context_id
        assert coord.sessions["INBOX-ITEM"].startswith("job:")
        occupied = {parked.context_id, long_turn.context_id}
        assert coord.sessions["QUICK-SCHEDULE"] not in occupied
        assert coord.sessions["INBOX-ITEM"] not in occupied | {unrelated.context_id}
        assert not coord.release.is_set()
        assert [d.session_id for d in ledger.list(state="pending")] == [parked.context_id]
        held = dict(_unresolved_occurrences(scheduler.path))
        assert held.get("job-decision") in ("claimed", "unknown")
        assert held.get("job-long") in ("claimed", "unknown")
        # The parked fire committed its acceptance evidence (task handle) durably.
        parked_claim = _occurrence(scheduler.path, "job-decision")
        assert parked_claim["task_id"]

        # Work access, fleet-wide: the SAME /api/decisions list + pending count the Work
        # panel and Needs-you marker read — unscoped, visible without opening the session.
        async with _REAL_ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=op_app), base_url="http://operator"
        ) as operator:
            listing = (await operator.get("/api/decisions", params={"state": "pending"})).json()
            assert listing["pending"] == 1
            record = next(d for d in listing["decisions"] if d["session_id"] == parked.context_id)
            assert record["question"] == "Ship the nightly batch?"

            # The production answer route: real loopback resume, real parked-task routing,
            # real checkpoint consumption — 200 only once consumed (#5099).
            answered = await operator.post(f"/api/decisions/{record['id']}/answer", json={"option_id": "ship"})
            assert answered.status_code == 200
            body = answered.json()
            assert body["decision"]["state"] == "answered"
            assert body["decision"]["answer"] == "ship"

        # Only the OWNING job resumed; the long turn is still held.
        assert coord.resumed == [(parked.context_id, "Operator chose: ship (ship)")]
        assert not coord.release.is_set()
        assert dict(_unresolved_occurrences(scheduler.path)).get("job-long") in ("claimed", "unknown")

        coord.release.set()
        # Every fire settles; nothing is left pending on the ledger.
        await _wait_until(lambda: not _unresolved_occurrences(scheduler.path), message="all occurrences settled")
        await _wait_until(lambda: scheduler.get_job("job-decision") is None, message="parked one-shot settled")
        assert ledger.list(state="pending") == []
    finally:
        coord.release.set()
        await scheduler.stop()
        await router.drain()

# ── 2. legacy migration + restart: exactly one invocation per intended fire ───────


def _capture_app(events: list, job_states: dict | None = None) -> FastAPI:
    """The faked network edge for scheduler-only tests: SendMessage mints one task per
    message whose state ``job_states`` controls BY JOB ID (default COMPLETED); GetTask
    serves that task's current state. Every call lands in ``events``."""
    states = job_states if job_states is not None else {}
    tasks: dict[str, str] = {}  # task id -> the job id whose fire minted it
    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(body: dict):
        if body.get("method") == "SendMessage":
            message = body["params"]["message"]
            job_id = str(message.get("metadata", {}).get("scheduler_job_id", ""))
            task_id = f"task-{message['messageId']}"
            tasks[task_id] = job_id
            events.append(("send", message["contextId"], message["messageId"], job_id))
        else:
            task_id = body["params"]["id"]
            events.append(("gettask", task_id))
        state = states.get(tasks.get(task_id, ""), "TASK_STATE_COMPLETED")
        return {"result": {"task": {"id": task_id, "status": {"state": state}}}}

    return app


@pytest.mark.asyncio
async def test_legacy_null_and_activity_jobs_restart_without_loss_or_double_fire(tmp_path, monkeypatch):
    """Legacy entries (context NULL and ``system:activity``) in the three unresolved
    shapes — QUEUED (due row, no claim), ACCEPTED (claim with task handle) and orphan
    (claim, no handle) — migrate idempotently and restart to exactly one invocation per
    intended fire: queued fires once on an isolated owner, the accepted claim settles
    through its ORIGINAL handle (never resent) before its owner's next independent
    fire, and the orphan stays durably quarantined (never lost, never resent)."""
    agent = "iso-migration"
    seed = _make_scheduler(tmp_path, agent)
    now = datetime.now(UTC)
    old_slot = (now - timedelta(seconds=120)).isoformat()
    due_slot = (now - timedelta(seconds=60)).isoformat()

    seed.add_job("legacy queued one-shot", (now + timedelta(seconds=0.4)).isoformat(), job_id="legacy-queued")
    accepted = seed.add_job("legacy accepted cron", "0 9 * * *", job_id="legacy-accepted")
    orphan = seed.add_job("legacy orphan cron", "0 9 * * *", job_id="legacy-orphan")

    def _snapshot(job, context, next_fire: str) -> str:
        shape = job.as_dict()
        shape.update(context_id=context, next_fire=next_fire)
        return json.dumps(shape)

    # Rewind to the pre-ADR-0129 shape: no owner mappings, legacy contexts, ledger mid-flight.
    db = sqlite3.connect(str(seed.path))
    try:
        db.execute("DELETE FROM job_sessions")
        db.execute("UPDATE jobs SET context_id = NULL WHERE id IN ('legacy-queued', 'legacy-orphan')")
        db.execute("UPDATE jobs SET context_id = ? WHERE id = 'legacy-accepted'", (ACTIVITY_CONTEXT,))
        db.execute("UPDATE jobs SET next_fire = ? WHERE id IN ('legacy-accepted', 'legacy-orphan')", (due_slot,))
        db.execute(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, expected_next, task_id, job_snapshot) "
            "VALUES ('legacy-accepted', ?, 'msg-legacy-accepted', 'claimed', ?, ?, 'task-legacy-accepted', ?)",
            (old_slot, accepted.created_at, due_slot, _snapshot(accepted, ACTIVITY_CONTEXT, old_slot)),
        )
        db.execute(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, expected_next, job_snapshot) "
            "VALUES ('legacy-orphan', ?, 'msg-legacy-orphan', 'claimed', ?, ?, ?)",
            (old_slot, orphan.created_at, due_slot, _snapshot(orphan, None, old_slot)),
        )
        db.commit()
    finally:
        db.close()
    pre_migration_occurrences = _rows(seed.path, "occurrences")

    # Restart #1 runs the versioned identity migration; restart #2 proves it is a no-op.
    _make_scheduler(tmp_path, agent)
    first = {t: _rows(seed.path, t) for t in ("jobs", "occurrences", "job_sessions")}
    restarted = _make_scheduler(tmp_path, agent)
    second = {t: _rows(seed.path, t) for t in ("jobs", "occurrences", "job_sessions")}
    assert first == second  # byte-for-byte: a second migration changes nothing

    # Migration changed ROUTING only: distinct isolated owners, no held rows, and the
    # occurrence ledger — keys, message ids, handle, snapshots — untouched.
    assert _rows(seed.path, "occurrences") == pre_migration_occurrences
    owners = {j.id: j for j in restarted.list_jobs()}
    contexts = {job_id: owners[job_id].context_id for job_id in ("legacy-queued", "legacy-accepted", "legacy-orphan")}
    assert all(c and c.startswith("job:") for c in contexts.values())
    assert len(set(contexts.values())) == 3
    assert ACTIVITY_CONTEXT not in contexts.values()
    assert all(owners[j].execution_hold is None for j in contexts)

    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    events: list = []
    _route_httpx_to(_capture_app(events), monkeypatch)

    await restarted.start()
    try:
        await _wait_until(
            lambda: len([e for e in events if e[0] == "send"]) >= 2
            and dict(_unresolved_occurrences(seed.path)) == {"legacy-orphan": "claimed"},
            message="queued fire + accepted job's next fire both settled",
        )
        # Nothing further fires: hold the store open for 15 more ticks.
        await asyncio.sleep(15 * _TEST_POLL_S)
    finally:
        await restarted.stop()

    sends = [e for e in events if e[0] == "send"]
    gettasks = [e for e in events if e[0] == "gettask"]

    # Exactly one invocation per intended fire — never system:activity, never a
    # duplicated identity, never a resend of the accepted or orphaned claims.
    assert len(sends) == 2
    assert len({(e[1], e[2]) for e in sends}) == 2
    send_contexts = {e[1] for e in sends}
    assert send_contexts == {contexts["legacy-queued"], contexts["legacy-accepted"]}
    assert ACTIVITY_CONTEXT not in send_contexts
    assert "msg-legacy-accepted" not in {e[2] for e in sends}

    # The accepted claim settled through its ORIGINAL task handle, BEFORE its owner
    # admitted the next independent fire (serialization by persisted ownership).
    assert ("gettask", "task-legacy-accepted") in gettasks
    reconcile_at = events.index(("gettask", "task-legacy-accepted"))
    new_fire_at = next(i for i, e in enumerate(events) if e[0] == "send" and e[1] == contexts["legacy-accepted"])
    assert reconcile_at < new_fire_at

    # Evidence preserved: identity + handle + terminal outcome; counters moved once per fire.
    settled = _occurrence(seed.path, "legacy-accepted", old_slot)
    assert (settled["message_id"], settled["task_id"]) == ("msg-legacy-accepted", "task-legacy-accepted")
    assert (settled["state"], settled["task_state"]) == ("settled", "completed")
    accepted_row = restarted.get_job("legacy-accepted")
    assert accepted_row.fire_count == 2  # the reconciled legacy fire + the new one
    assert accepted_row.consecutive_failures == 0 and accepted_row.last_ok

    # The orphan lost nothing and sent nothing: quarantined byte-for-byte, no invocation.
    orphan_claim = _occurrence(seed.path, "legacy-orphan", old_slot)
    assert (orphan_claim["state"], orphan_claim["task_id"], orphan_claim["message_id"]) == (
        "claimed", None, "msg-legacy-orphan",
    )
    assert contexts["legacy-orphan"] not in send_contexts

    # The one-shot's scheduling row is gone but its owner record (tombstone) remains.
    assert restarted.get_job("legacy-queued") is None
    db = sqlite3.connect(str(seed.path))
    try:
        tombstone = db.execute(
            "SELECT context_id FROM job_sessions WHERE job_id = 'legacy-queued'"
        ).fetchone()
    finally:
        db.close()
    assert tombstone and tombstone[0] == contexts["legacy-queued"]

    # Restart #3 after settlement: nothing re-fires or is re-read; the orphan still waits visibly.
    events_after: list = []
    _route_httpx_to(_capture_app(events_after), monkeypatch)
    final = _make_scheduler(tmp_path, agent)
    await final.start()
    try:
        await asyncio.sleep(15 * _TEST_POLL_S)
    finally:
        await final.stop()
    assert events_after == []
    assert dict(_unresolved_occurrences(seed.path)) == {"legacy-orphan": "claimed"}

# ── 3. a parked job cannot be superseded; background waits stay isolated ──────────


@pytest.mark.asyncio
async def test_parked_job_is_never_superseded_and_background_waits_stay_isolated(tmp_path, monkeypatch):
    """Drive a REAL fire into a REAL park (input-required task; the production fire path
    leaves the accepted claim + handle unresolved). While parked: a replacement of the
    same job id queues behind the same owner (claim refused, parked occurrence
    untouched); an unrelated job starts within the dispatch bound; each background job's
    ``wait`` lives on its OWN execution session and neither replaces the other. Once the
    parked task settles, the owner's wait continuation — and only then — may enter."""
    import runtime.state as rs

    from tools.lg_tools import _build_scheduler_tools

    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    events: list = []
    job_states = {"job-parked": "TASK_STATE_INPUT_REQUIRED"}
    _route_httpx_to(_capture_app(events, job_states), monkeypatch)

    scheduler = _make_scheduler(tmp_path, "iso-supersede")
    scheduler._fire_timeout_s = 0.3  # the parked fire goes UNKNOWN quickly; claim + handle stay
    monkeypatch.setattr(rs.STATE, "scheduler", scheduler, raising=False)
    wait_tool = next(t for t in _build_scheduler_tools(scheduler) if t.name == "wait")

    await scheduler.start()
    try:
        first = scheduler.add_job("park me", _past_iso(), job_id="job-parked")
        owner = first.context_id
        assert owner.startswith("job:")
        # The REAL fire path parks: accepted (task handle recorded), unresolved.
        parked = await _wait_until(
            lambda: (
                lambda row: row if row and row["task_id"] and row["state"] in ("claimed", "unknown") else None
            )(_occurrence(scheduler.path, "job-parked")),
            message="the fire accepted and stayed unresolved (parked)",
        )

        # An unrelated job starts within the documented bound while the park holds.
        t0 = time.monotonic()
        other = scheduler.add_job("unrelated sweep", _past_iso(), job_id="job-other")
        assert other.context_id.startswith("job:") and other.context_id != owner
        await _wait_until(
            lambda: any(e[3] == "job-other" for e in events if e[0] == "send"),
            timeout=_DISPATCH_BOUND_S,
            message="the unrelated job dispatched while another was parked",
        )
        assert time.monotonic() - t0 <= _DISPATCH_BOUND_S
        await _wait_until(
            lambda: dict(_unresolved_occurrences(scheduler.path)).get("job-other") is None,
            message="the unrelated fire settled",
        )

        # A replacement rides the SAME owner, queued BEHIND the accepted predecessor:
        # its claim is refused and the parked claim is untouched.
        from graph.sdk import run_in_session

        assert run_in_session(ACTIVITY_CONTEXT, "park me again", job_id="job-parked")["ok"]
        replacement = scheduler.get_job("job-parked")
        assert replacement.created_at != first.created_at
        assert replacement.context_id == owner
        assert scheduler._claim_occurrence(replacement) is None
        after = _occurrence(scheduler.path, "job-parked", parked["next_fire"])
        assert tuple(after) == tuple(parked)

        # Background waits: one per OWNING execution session, mutually isolated.
        await wait_tool.ainvoke({"seconds": 3600, "then": "resume parked", "state": {"session_id": owner}})
        await wait_tool.ainvoke({"seconds": 3600, "then": "resume other", "state": {"session_id": other.context_id}})
        wait_parked = scheduler.get_job(f"wait:{owner}")
        wait_other = scheduler.get_job(f"wait:{other.context_id}")
        assert wait_parked.context_id == owner
        assert wait_other.context_id == other.context_id
        assert wait_parked.id != wait_other.id
        # Neither wait disturbed the other, the parked claim, or shared Activity.
        assert tuple(_occurrence(scheduler.path, "job-parked", parked["next_fire"])) == tuple(parked)
        assert ACTIVITY_CONTEXT not in (wait_parked.context_id, wait_other.context_id)

        # While the owner's invocation is unresolved even its own continuation waits;
        # the settled owner's continuation may enter.
        assert scheduler._claim_occurrence(wait_parked) is None
        assert scheduler._claim_occurrence(wait_other) is not None

        # The parked task finally ends; reconciliation settles the claim through the
        # retained handle, and only now may the owner's continuation enter.
        job_states["job-parked"] = "TASK_STATE_COMPLETED"
        await _wait_until(
            lambda: (_occurrence(scheduler.path, "job-parked", parked["next_fire"]) or {"state": ""})["state"] == "settled",
            message="the parked claim settled through its retained task handle",
        )
        assert scheduler._claim_occurrence(wait_parked) is not None
        settled = _occurrence(scheduler.path, "job-parked", parked["next_fire"])
        assert (settled["message_id"], settled["task_id"]) == (parked["message_id"], parked["task_id"])
    finally:
        await scheduler.stop()
