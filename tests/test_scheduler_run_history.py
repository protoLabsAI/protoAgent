"""Scheduler run history (ADR 0135 D8, #5543): ``GET /api/scheduler/jobs/{id}/runs``.

The route reads a job's past runs from the ``occurrences`` ledger the scheduler already
keeps — there is no second history store. Fires go through the REAL
``LocalScheduler._fire`` against a canned A2A endpoint, so the rows asserted here are the
ones the dispatch path itself wrote.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api import console_handlers as ch
from operator_api.routes import register_operator_routes
from scheduler.local import RUN_HISTORY_MAX_LIMIT, LocalScheduler, _row_to_run

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_OK = {"result": {"task": {"id": "t-ok", "status": {"state": "TASK_STATE_COMPLETED"}}}}
_FAILED = {"result": {"task": {"id": "t-bad", "status": {
    "state": "TASK_STATE_FAILED", "message": {"parts": [{"text": "model stream died"}]}}}}}
# A failed turn answered without a task handle — settled on the fire path itself.
_FAILED_NO_HANDLE = {"result": {"status": {
    "state": "TASK_STATE_FAILED", "message": {"parts": [{"text": "gateway refused the turn"}]}}}}


def _app(**accessors) -> FastAPI:
    app = FastAPI()
    register_operator_routes(
        app, runtime_status=lambda: {}, subagent_list=lambda: [],
        subagent_run=lambda r: None, subagent_batch=lambda r: None, **accessors,
    )
    return app


@pytest.fixture
def history(tmp_path, monkeypatch):
    """``(client, scheduler, fire)`` — the real handlers over a real tmp ``LocalScheduler``;
    ``fire(job_id, body)`` runs one due slot whose turn the A2A endpoint answers with ``body``."""
    import runtime.state as rs

    sched = LocalScheduler("t", invoke_url="http://test", db_dir=tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", sched, raising=False)
    answer: dict = {}
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=answer["body"]))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(transport=transport))

    async def fire(job_id: str, body: dict) -> str:
        answer["body"] = body
        job = sched.get_job(job_id)
        # Claim the way a tick does: the cron row rolls to its next slot at claim time.
        slot = datetime.fromisoformat(job.next_fire)
        sched._claimed_messages[job.id] = sched._claim_occurrence(job, advance_at=slot)
        await sched._fire(job)
        return job.next_fire

    client = TestClient(_app(scheduler_list=ch._operator_scheduler_list, scheduler_runs=ch._operator_scheduler_runs))
    return client, sched, fire


@pytest.mark.asyncio
async def test_runs_are_newest_first_with_state_time_and_error(history):
    client, sched, fire = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    slots = [await fire("j1", body) for body in (_OK, _FAILED, _FAILED_NO_HANDLE)]

    resp = client.get("/api/scheduler/jobs/j1/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "j1"
    runs = body["runs"]
    assert [r["scheduled_for"] for r in runs] == slots[::-1]  # newest slot first
    assert slots == sorted(slots) and len(set(slots)) == 3
    assert [r["state"] for r in runs] == ["settled"] * 3
    assert [r["ok"] for r in runs] == [False, False, True]
    assert [r["error"] for r in runs] == ["gateway refused the turn", "model stream died", None]
    assert [r["task_state"] for r in runs] == [None, "failed", "completed"]
    assert [r["task_id"] for r in runs] == [None, "t-bad", "t-ok"]
    # The same ledger rows, not a copy: nothing but ``occurrences`` holds a run.
    with sqlite3.connect(str(sched.path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert db.execute("SELECT COUNT(*) FROM occurrences WHERE job_id = 'j1'").fetchone()[0] == 3
    assert not {t for t in tables if "run" in t or "history" in t}


@pytest.mark.asyncio
async def test_limit_bounds_the_list(history):
    client, sched, fire = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    slots = [await fire("j1", _OK) for _ in range(4)]

    two = client.get("/api/scheduler/jobs/j1/runs?limit=2").json()["runs"]
    assert [r["scheduled_for"] for r in two] == slots[:1:-1]  # the two newest
    # Out of range is clamped to the backend's bounds, never unbounded.
    assert len(client.get("/api/scheduler/jobs/j1/runs?limit=0").json()["runs"]) == 1
    assert len(client.get("/api/scheduler/jobs/j1/runs?limit=999999").json()["runs"]) == 4
    assert client.get("/api/scheduler/jobs/j1/runs?limit=abc").status_code == 422

    with sqlite3.connect(str(sched.path)) as db:  # more ledger rows than the ceiling
        db.executemany(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, expected_next) "
            "VALUES ('j1', ?, ?, 'settled', 'gen', 'x')",
            [(f"2001-01-01T00:00:00.{i:06d}+00:00", f"m{i}") for i in range(RUN_HISTORY_MAX_LIMIT + 5)],
        )
    assert len(sched.list_runs("j1", 10**9)) == RUN_HISTORY_MAX_LIMIT


@pytest.mark.asyncio
async def test_unknown_job_is_404_but_a_known_one_is_never(history):
    client, sched, fire = history
    missing = client.get("/api/scheduler/jobs/nope/runs")
    assert missing.status_code == 404
    assert "nope" in missing.json()["detail"]

    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    assert client.get("/api/scheduler/jobs/j1/runs").json() == {"job_id": "j1", "runs": []}  # not fired yet

    # The ledger outlives the job row: a canceled job's past runs stay readable.
    slot = await fire("j1", _OK)
    assert sched.cancel_job("j1") is True
    gone = client.get("/api/scheduler/jobs/j1/runs")
    assert gone.status_code == 200
    assert [r["scheduled_for"] for r in gone.json()["runs"]] == [slot]


def test_route_rejects_unauthenticated_callers_like_the_other_scheduler_routes(history):
    """Auth is positional (the ``/api`` guard), so the new path must sit under it exactly
    as ``/api/scheduler/jobs`` does: no credential 401, a federation credential 403."""
    from a2a_impl import auth

    _, sched, _ = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    app = _app(scheduler_list=ch._operator_scheduler_list, scheduler_runs=ch._operator_scheduler_runs)
    auth.configure(bearer_token="op-secret", api_key="", allowed_origins_raw="", federation_token="fed-secret")
    app.add_middleware(auth.A2AAuthMiddleware)
    client = TestClient(app)

    for path in ("/api/scheduler/jobs", "/api/scheduler/jobs/j1/runs"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer fed-secret"}).status_code == 403
        assert client.get(path, headers={"Authorization": "Bearer op-secret"}).status_code == 200


def test_backend_without_run_history_is_501_and_unwired_route_is_absent(monkeypatch):
    import runtime.state as rs

    class _NoHistoryScheduler:
        name = "local"

        def list_jobs(self):
            return []

    monkeypatch.setattr(rs.STATE, "scheduler", _NoHistoryScheduler(), raising=False)
    resp = TestClient(_app(scheduler_runs=ch._operator_scheduler_runs)).get("/api/scheduler/jobs/j1/runs")
    assert resp.status_code == 501
    assert TestClient(_app()).get("/api/scheduler/jobs/j1/runs").status_code == 404


@pytest.mark.parametrize(
    ("state", "task_state", "diagnostic", "ok", "error"),
    [
        ("running", "working", "task t pending (working): queued or running", None, None),
        ("unknown", "missing", "task t missing or unavailable", None, "task t missing or unavailable"),
        ("settled", "canceled", None, None, None),
        ("settled", None, None, None, None),  # settled before the ledger kept outcomes
        ("interrupted_needs_operator", None, "interrupted by a restart", None, "interrupted by a restart"),
    ],
)
def test_unresolved_and_outcome_less_runs_never_read_as_ok_or_failed(state, task_state, diagnostic, ok, error):
    row = {"job_id": "j1", "next_fire": "2026-01-01T09:00:00+00:00", "created_at": "gen", "state": state,
           "task_state": task_state, "diagnostic": diagnostic, "task_id": "t", "message_id": "m"}
    run = _row_to_run(row)
    assert (run["ok"], run["error"]) == (ok, error)


# ── run timing + reported (#5543 S2) ─────────────────────────────────────────


def _done(task_id: str) -> dict:
    return {"result": {"task": {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}}}}


def _utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)  # ISO, and UTC
    return parsed


@pytest.mark.asyncio
async def test_a_sent_and_completed_run_carries_start_end_and_duration(history):
    client, sched, fire = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    before = datetime.now(UTC)
    await fire("j1", _OK)
    (run,) = client.get("/api/scheduler/jobs/j1/runs").json()["runs"]
    started, ended = _utc(run["started_at"]), _utc(run["ended_at"])
    assert before <= started <= ended <= datetime.now(UTC)
    assert run["duration_s"] == (ended - started).total_seconds()
    assert (run["state"], run["ok"], run["reported"]) == ("settled", True, None)  # no card decision yet

    # A failure settled on the fire path itself (no task handle) is timed the same way.
    await fire("j1", _FAILED_NO_HANDLE)
    failed = sched.list_runs("j1")[0]
    assert failed["ok"] is False and failed["duration_s"] >= 0 and failed["reported"] is None


@pytest.mark.asyncio
async def test_unsettled_runs_have_no_end_and_no_duration(history, monkeypatch):
    import scheduler.local as local

    _, sched, fire = history
    working = {"result": {"task": {"id": "t-long", "status": {"state": "TASK_STATE_WORKING"}}}}
    no_handle = {"result": {"status": {"state": "TASK_STATE_WORKING"}}}
    monkeypatch.setattr(local, "_POLL_INTERVAL_S", 0.01)
    sched._fire_timeout_s = 0.05  # the bounded wait runs out while the turn still works
    for job_id, body in (("running", working), ("unknown", no_handle)):
        sched.add_job("nightly sweep", "0 9 * * *", job_id=job_id)
        await fire(job_id, body)
    sched.add_job("nightly sweep", "0 9 * * *", job_id="claimed")
    assert sched._claim_occurrence(sched.get_job("claimed"))  # claimed, never sent

    for job_id in ("running", "unknown", "claimed"):
        (run,) = sched.list_runs(job_id)
        assert run["state"] == job_id
        assert (run["ended_at"], run["duration_s"], run["reported"]) == (None, None, None)
        # Sent runs know when they started; a merely claimed one does not.
        assert (run["started_at"] is None) == (job_id == "claimed")


@pytest.mark.asyncio
async def test_reported_follows_the_result_card_decision(history, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from server import a2a

    _, sched, fire = history
    events: list = []
    monkeypatch.setattr(a2a, "_SCHEDULED_CARD_HASHES", {})
    monkeypatch.setattr(a2a, "_SCHEDULED_UNCHANGED_RUNS", {})
    monkeypatch.setattr(a2a._event_bus, "publish", lambda ev, d, **_kw: events.append((ev, d)))
    monkeypatch.setattr(a2a, "_notify_scheduled_card", lambda _payload: None)
    monkeypatch.setattr(a2a, "_followup_waiting_on_operator", lambda _sid: False)
    monkeypatch.setattr(a2a, "_scheduled_home_session", lambda: "")

    async def run(job_id: str, body: dict, text: str, state: str = "completed") -> None:
        await fire(job_id, body)
        pending = set(a2a._BG_WAKE_TASKS)
        a2a._handle_scheduled_delivery(SimpleNamespace(
            origin="scheduler", trigger=job_id, context_id=f"job:{job_id}", text=text,
            state=state, task_id=body["result"]["task"]["id"],
            error="model stream died" if state == "failed" else "",
        ))
        await asyncio.gather(*(a2a._BG_WAKE_TASKS - pending))

    sched.add_job("watch the queue", "0 9 * * *", job_id="j1", origin_session="chat-42")
    await run("j1", _done("t1"), "Two reviews landed.")
    await run("j1", _done("t2"), "Checked again.\nNO_UPDATE")
    await run("j1", _done("t3"), "")
    await run("j1", _done("t4"), "Two reviews landed.")  # a repeat of the last card
    await run("j1", _FAILED, "", state="failed")  # the failure card posts; not a report
    assert sched._claim_occurrence(sched.get_job("j1"))  # an unsettled run

    runs = sched.list_runs("j1")
    assert [r["reported"] for r in runs] == [None, None, False, False, False, True]
    assert [r["ok"] for r in runs] == [None, False, True, True, True, True]
    cards = [d for ev, d in events if ev == "scheduler.completed"]
    assert [c["status"] for c in cards] == ["completed", "failed"]

    # A job with no chat to report to completes without a card decision: unknown.
    sched.add_job("silent sweep", "0 9 * * *", job_id="j2")
    await run("j2", _done("t5"), "NO_UPDATE")
    assert [(r["ok"], r["reported"]) for r in sched.list_runs("j2")] == [(True, None)]


@pytest.mark.asyncio
async def test_record_run_report_matches_the_run_by_task_or_pending_claim(history):
    _, sched, fire = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    await fire("j1", _done("t1"))
    assert sched.record_run_report("j1", "t-other", True) is False  # no such run, nothing claimed
    assert sched.record_run_report("j1", "", True) is False
    assert sched.list_runs("j1")[0]["reported"] is None

    # The turn can end before the fire stored its task handle: the claimed slot takes it.
    assert sched._claim_occurrence(sched.get_job("j1"))
    assert sched.record_run_report("j1", "t2", False) is True
    with sqlite3.connect(str(sched.path)) as db:
        assert [r[0] for r in db.execute("SELECT reported FROM occurrences ORDER BY next_fire")] == [None, 0]
    # ...and a quiet run stays unknown in history until it has actually completed.
    assert [r["reported"] for r in sched.list_runs("j1")] == [None, None]


@pytest.mark.asyncio
async def test_a_ledger_from_before_the_timing_columns_upgrades_in_place(history, tmp_path):
    client, sched, fire = history
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")
    await fire("j1", _OK)
    with sqlite3.connect(str(sched.path)) as db:  # back to the pre-change table shape
        for column in ("started_at", "ended_at", "reported"):
            db.execute(f"ALTER TABLE occurrences DROP COLUMN {column}")
        legacy = {r[1] for r in db.execute("PRAGMA table_info(occurrences)")}
    assert not legacy & {"started_at", "ended_at", "reported"}

    reopened = LocalScheduler("t", invoke_url="http://test", db_dir=tmp_path)
    (run,) = reopened.list_runs("j1")
    assert (run["started_at"], run["ended_at"], run["duration_s"], run["reported"]) == (None,) * 4
    # Every key the entry already had keeps its value.
    assert (run["state"], run["task_state"], run["ok"], run["error"], run["task_id"]) == (
        "settled", "completed", True, None, "t-ok")
    # ...and the upgraded store records the new fields for the next run.
    slot = await fire("j1", _OK)
    newest = client.get("/api/scheduler/jobs/j1/runs").json()["runs"][0]
    assert newest["scheduled_for"] == slot and newest["duration_s"] >= 0
