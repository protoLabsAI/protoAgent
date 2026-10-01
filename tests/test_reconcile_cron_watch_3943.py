"""#3943 (+ the scheduler PUT item of #3957) — regression tests.

1. A background job reconciled at restart settles its delegation-ledger edge.
2. A malformed schedule is a ``ValueError`` (tool → "Error: …", REST → 400), never a
   500 or a crashed tool call.
3. ``WatchController.clear`` racing an in-flight ``evaluate`` cannot resurrect the
   watch (or clobber a watch re-created under the same id).
4. ``PUT /api/scheduler/jobs/{id}`` is a partial update.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.ledger_store import LedgerStore

# The watch tests import graph.goals.verifiers (platform-branching), and the scheduler
# tests touch real SQLite files — run them on the Windows lane too.
pytestmark = pytest.mark.platform_sensitive

# --- 1. reconcile_interrupted settles the ledger edge ----------------------------------


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    import runtime.state as rs

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


@pytest.mark.asyncio
async def test_a_job_interrupted_by_a_restart_settles_its_ledger_edge(ledger_db, tmp_path, monkeypatch):
    from background.manager import BackgroundManager
    from background.store import INTERRUPTED_ERROR, BackgroundStore

    store = BackgroundStore(str(Path(tmp_path) / "bg.db"))
    mgr = BackgroundManager(
        agent_name="a", invoke_url="http://127.0.0.1:7870", store=store, api_key="k", bearer_token="b"
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    running = await mgr.spawn(origin_session="s1", subagent_type="researcher", description="dig", prompt="go")
    done = await mgr.spawn(origin_session="s1", subagent_type="researcher", description="done", prompt="go")
    store.mark_complete(done, "completed", "found it")
    edges = {r["task_id"]: r for r in ledger_db.recent()}
    assert edges[running]["outcome"] == "ok" and not edges[running]["duration_ms"]  # open
    done_before = dict(edges[done])

    # The process dies; the next boot reconciles the job left running.
    assert BackgroundStore(str(Path(tmp_path) / "bg.db")).reconcile_interrupted() == 1

    edges = {r["task_id"]: r for r in ledger_db.recent()}
    assert edges[running]["outcome"] == "failed", "an interrupted job's edge must not read as running forever"
    assert INTERRUPTED_ERROR in edges[running]["error"]
    # The job's end time is unknown, so no duration is recorded — boot-time minus
    # spawn-time would be the server's downtime, not the job's work (#3969 review).
    assert not edges[running]["duration_ms"]
    assert edges[done] == done_before, "an already-settled edge is left alone"
    job = store.get(running)
    assert job.status == "failed" and job.error == INTERRUPTED_ERROR
    assert "Interrupted" in job.result
    # Idempotent: a second boot has nothing left to reconcile.
    assert store.reconcile_interrupted() == 0


def test_reconcile_does_not_record_the_downtime_as_the_jobs_duration(ledger_db, tmp_path):
    """A job spawned an hour before a crash-and-restart did not WORK for an hour."""
    from datetime import UTC, datetime, timedelta

    from background.store import BackgroundStore

    s = BackgroundStore(str(tmp_path / "jobs.db"))
    jid = s.create(
        agent_name="a",
        origin_session="s",
        subagent_type="r",
        description="d",
        prompt="p",
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    ledger_db.record(from_agent="a", to_kind="subagent", to_name="r", task_id=jid, origin="background")

    assert s.reconcile_interrupted() == 1

    (edge,) = [r for r in ledger_db.recent() if r["task_id"] == jid]
    assert edge["outcome"] == "failed"
    assert not edge["duration_ms"], f"recorded {edge['duration_ms']}ms of downtime as work"


def test_reconcile_without_a_ledger_still_fails_the_jobs(tmp_path, monkeypatch):
    import runtime.state as rs
    from background.store import BackgroundStore

    monkeypatch.setattr(rs.STATE, "ledger_store", None, raising=False)
    s = BackgroundStore(str(tmp_path / "jobs.db"))
    ids = [
        s.create(agent_name="a", origin_session="s", subagent_type="r", description="d", prompt="p") for _ in range(3)
    ]
    assert s.reconcile_interrupted() == 3
    assert all(s.get(i).status == "failed" for i in ids)


# --- 2. malformed schedules normalise to ValueError ------------------------------------

# Each of these raised something other than ValueError on origin/main (OverflowError /
# TypeError), which the REST route mapped to a 500.
_NON_VALUEERROR_SCHEDULES = [
    ("9999-12-31T23:59:59-14:00", None),  # UTC conversion overflows datetime → OverflowError
    ("0001-01-01T00:00+14:00", None),  # likewise, below the range
    (None, None),  # TypeError from the cron regex
    (12345, None),
    ("0 9 * * *", 5),  # a non-string timezone → TypeError from ZoneInfo
]
# Already ValueError on main — must stay so.
_VALUEERROR_SCHEDULES = ["60 * * * *", "a b c d e", "*/0 * * * *", "0 0 31 2 *", "not a schedule", ""]


def _local_scheduler(tmp_path):
    from scheduler.local import LocalScheduler

    return LocalScheduler("t", invoke_url="http://127.0.0.1:1", db_dir=tmp_path)


@pytest.mark.parametrize(("schedule", "tz"), _NON_VALUEERROR_SCHEDULES)
def test_add_and_update_raise_valueerror_for_a_malformed_schedule(tmp_path, schedule, tz):
    s = _local_scheduler(tmp_path)
    with pytest.raises(ValueError, match="invalid (schedule|timezone)"):
        s.add_job("p", schedule, timezone=tz)
    s.add_job("p", "0 9 * * *", job_id="j1")
    with pytest.raises(ValueError, match="invalid (schedule|timezone)"):
        s.update_job("j1", "p", schedule, timezone=tz)
    assert s.get_job("j1").schedule == "0 9 * * *", "a rejected edit leaves the job intact"


@pytest.mark.parametrize("schedule", _VALUEERROR_SCHEDULES)
def test_croniter_and_iso_errors_still_valueerror(tmp_path, schedule):
    with pytest.raises(ValueError):
        _local_scheduler(tmp_path).add_job("p", schedule)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["9999-12-31T23:59:59-14:00", "0001-01-01T00:00+14:00", "60 * * * *", "a b c d e"])
async def test_schedule_task_tool_reports_a_clear_error(tmp_path, when):
    from tools.scheduler_tools import _build_scheduler_tools

    tools = {t.name: t for t in _build_scheduler_tools(_local_scheduler(tmp_path))}
    out = await tools["schedule_task"].ainvoke({"prompt": "p", "when": when})
    assert out.startswith("Error: invalid schedule"), out
    assert "add_job failed" not in out  # the old catch-all wording for an unexpected exception


def _real_scheduler_client(tmp_path, monkeypatch):
    """The REAL console handlers + routes over a real LocalScheduler."""
    import runtime.state as rs
    from operator_api import console_handlers as ch
    from operator_api.routes import register_operator_routes

    sched = _local_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", sched, raising=False)
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {"graph_loaded": True},
        subagent_list=lambda: [],
        subagent_run=lambda req: None,
        subagent_batch=lambda req: None,
        scheduler_list=ch._operator_scheduler_list,
        scheduler_add=ch._operator_scheduler_add,
        scheduler_cancel=ch._operator_scheduler_cancel,
        scheduler_update=ch._operator_scheduler_update,
    )
    return TestClient(app, raise_server_exceptions=False), sched


@pytest.mark.parametrize(
    "body",
    [
        {"schedule": "9999-12-31T23:59:59-14:00"},
        {"schedule": "0001-01-01T00:00+14:00"},
        {"schedule": "60 * * * *"},
        {"schedule": "0 0 31 2 *"},
        {"schedule": "0 9 * * *", "timezone": "Not/AZone"},
    ],
)
def test_rest_malformed_schedule_is_400_not_500(tmp_path, monkeypatch, body):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    created = client.post("/api/scheduler/jobs", json={"prompt": "p", **body})
    assert created.status_code == 400, created.text
    assert "invalid" in created.json()["detail"]

    assert (
        client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"}).status_code
        == 200
    )
    edited = client.put("/api/scheduler/jobs/j1", json={"prompt": "p", **body})
    assert edited.status_code == 400, edited.text


# --- 4. PUT /api/scheduler/jobs/{id} is a partial update (#3957) ------------------------


def test_put_with_only_a_schedule_keeps_the_prompt(tmp_path, monkeypatch):
    client, sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post(
        "/api/scheduler/jobs",
        json={"prompt": "sweep logs", "schedule": "0 9 * * *", "job_id": "j1", "timezone": "America/Chicago"},
    )

    resp = client.put("/api/scheduler/jobs/j1", json={"schedule": "0 17 * * 1-5"})
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["schedule"] == "0 17 * * 1-5"
    assert job["prompt"] == "sweep logs", "an omitted prompt keeps its current value"
    assert job["timezone"] == "America/Chicago", "an omitted timezone keeps its current value"


def test_put_with_only_a_prompt_keeps_the_schedule(tmp_path, monkeypatch):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post("/api/scheduler/jobs", json={"prompt": "old", "schedule": "0 9 * * *", "job_id": "j1"})
    job = client.put("/api/scheduler/jobs/j1", json={"prompt": "new"}).json()["job"]
    assert job["prompt"] == "new" and job["schedule"] == "0 9 * * *"


def test_put_explicit_null_timezone_clears_it(tmp_path, monkeypatch):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post(
        "/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1", "timezone": "Europe/Paris"}
    )
    job = client.put("/api/scheduler/jobs/j1", json={"timezone": None}).json()["job"]
    assert job["timezone"] is None and job["prompt"] == "p"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"prompt": "  "}, "prompt cannot be empty"),
        ({"schedule": ""}, "schedule cannot be empty"),
        ({}, "nothing to update"),
        ({"schedule": "not a schedule"}, "invalid schedule"),
    ],
)
def test_put_still_validates_provided_fields(tmp_path, monkeypatch, body, detail):
    client, sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    resp = client.put("/api/scheduler/jobs/j1", json=body)
    assert resp.status_code == 400, resp.text
    assert detail in resp.json()["detail"]
    assert sched.get_job("j1").prompt == "p" and sched.get_job("j1").schedule == "0 9 * * *"


def test_put_partial_on_a_missing_job_is_400(tmp_path, monkeypatch):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    resp = client.put("/api/scheduler/jobs/nope", json={"schedule": "0 9 * * *"})
    assert resp.status_code == 400 and "no job" in resp.json()["detail"]


# --- 3. watch clear() vs an in-flight evaluate() ---------------------------------------


class _GatedVerifier:
    """A verifier that parks mid-evaluation until the test releases it — the window in
    which a real (slow) verifier can be overtaken by a clear."""

    def __init__(self, met: bool):
        self.met = met
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, spec, ctx):
        from graph.goals.verifiers import VerifyResult

        self.entered.set()
        await self.release.wait()
        return VerifyResult(self.met, "verifier said so", "evidence-1")


def _watch_ctrl(tmp_path):
    from graph.config import LangGraphConfig
    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore

    return WatchController(LangGraphConfig(), WatchStore(tmp_path))


async def _race(c, monkeypatch, *, met: bool, between):
    """Start evaluate, wait until it is parked inside the verifier, run ``between``,
    then let the verifier return and the evaluate finish."""
    import graph.watches.controller as wc

    gate = _GatedVerifier(met)
    monkeypatch.setattr(wc, "run_verifier", gate)
    _ok, _m, w = c.create(
        condition="deploy green", verifier={"type": "plugin", "check": "p:v"}, run_session="s1", run_prompt="go"
    )
    task = asyncio.create_task(c.evaluate(w.id))
    await asyncio.wait_for(gate.entered.wait(), 5)
    await between(w)
    gate.release.set()
    return w, await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("met", [False, True])
async def test_clear_during_evaluate_does_not_resurrect_the_watch(tmp_path, monkeypatch, met):
    import graph.sdk as sdk

    reactions: list[str] = []
    monkeypatch.setattr(sdk, "run_in_session", lambda sid, prompt, **kw: reactions.append(sid), raising=False)
    c = _watch_ctrl(tmp_path)

    async def _clear(w):
        # Off the loop, as the operator route and the clear_watch tool do.
        assert await asyncio.to_thread(c.clear, w.id) is True

    w, status = await _race(c, monkeypatch, met=met, between=_clear)
    assert c.store.get(w.id) is None, "a cleared watch must stay cleared"
    assert c.list_watches() == []
    assert status is None
    assert reactions == [], "a cleared watch must not react"


@pytest.mark.asyncio
async def test_clear_then_recreate_during_evaluate_keeps_the_new_watch(tmp_path, monkeypatch):
    c = _watch_ctrl(tmp_path)

    async def _replace(w):
        c.clear(w.id)
        ok, _m, fresh = c.create(
            condition="deploy green", verifier={"type": "plugin", "check": "p:other"}, watch_id=w.id
        )
        assert ok and fresh.id == w.id

    w, _status = await _race(c, monkeypatch, met=False, between=_replace)
    stored = c.store.get(w.id)
    assert stored is not None and stored.verifier["check"] == "p:other"
    assert stored.check_count == 0 and stored.last_evidence in ("", None), "the stale evaluate must not overwrite it"


@pytest.mark.asyncio
async def test_an_uncontended_evaluate_still_writes(tmp_path, monkeypatch):
    async def _nothing(_w):
        return None

    c = _watch_ctrl(tmp_path)
    w, status = await _race(c, monkeypatch, met=False, between=_nothing)
    assert status is None
    stored = c.store.get(w.id)
    assert stored.check_count == 1 and stored.last_evidence == "evidence-1"


# The console's exact PUT bodies (apps/web SchedulePanel → api.updateSchedule). The PUT is
# partial, so the console must SAY "UTC" with an explicit null — these pin both halves.


def test_console_switch_to_utc_payload_clears_the_zone(tmp_path, monkeypatch):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post(
        "/api/scheduler/jobs",
        json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1", "timezone": "America/Chicago"},
    )
    # The "UTC" option in the zone select: `timezone: out.timezone ?? null`.
    resp = client.put("/api/scheduler/jobs/j1", json={"prompt": "p", "schedule": "0 9 * * *", "timezone": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["timezone"] is None


def test_console_cron_to_one_shot_payload_drops_the_zone(tmp_path, monkeypatch):
    client, _sched = _real_scheduler_client(tmp_path, monkeypatch)
    client.post(
        "/api/scheduler/jobs",
        json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1", "timezone": "America/Chicago"},
    )
    # A one-shot carries its own offset, so the builder reports no zone → null.
    resp = client.put(
        "/api/scheduler/jobs/j1", json={"prompt": "p", "schedule": "2030-01-01T09:00:00Z", "timezone": None}
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["timezone"] is None and job["schedule"] == "2030-01-01T09:00:00Z"
