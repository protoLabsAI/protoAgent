"""Server-side undo for console task-deletes and schedule-cancels (#4955 Q10).

Extends the UndoLedger (bd-0ia51.3) from goals/watches to two more console deletes:

* ``DELETE /api/tasks/issues/{id}`` stashes the issue row and returns an ``undo_token``;
  ``POST /api/undo/{token}`` re-inserts it via ``TaskStore.restore`` (re-firing
  ``task.changed`` with ``action: "restored"``).
* Cancelling a scheduled job (``_operator_scheduler_cancel``) stashes ``job.as_dict()`` and
  returns an ``undo_token``; the undo re-adds the job via ``scheduler.add_job`` with its
  original id / prompt / schedule / timezone.

Covers delete→restore for each kind, the "restored" event, the clobber refusal, the
past-one-shot restore, and the unknown / already-used 404s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api import console_handlers as ch
from operator_api.routes import register_operator_routes
from operator_api.undo import LEDGER
from scheduler.local import LocalScheduler
from tasks.store import TaskStore


@pytest.fixture(autouse=True)
def _clean_ledger():
    """Isolate each test from tokens minted by another (shared process-local singleton)."""
    LEDGER._entries.clear()
    yield
    LEDGER._entries.clear()


@pytest.fixture
def events(monkeypatch):
    """Capture the bus events the stores publish (``task.changed``)."""
    import graph.plugins.host as host

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: seen.append((topic, data)), raising=False)
    return seen


# ── tasks ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def tasks_client(tmp_path):
    """A TestClient wired to the real task routes over a real tmp TaskStore."""
    store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        tasks_store=store,
    )
    return TestClient(app), store


def test_task_delete_returns_token_and_undo_restores(tasks_client, events):
    client, store = tasks_client
    issue = store.create("ship the thing", description="do it", priority=1, issue_type="bug")
    iid = issue["id"]
    # Move it off the defaults so the restore has non-trivial state to bring back.
    store.update(iid, status="in_progress")
    original = store.get(iid)

    deleted = client.delete(f"/api/tasks/issues/{iid}")
    assert deleted.status_code == 200
    token = deleted.json()["undo_token"]
    assert token and store.get(iid) is None  # the delete really happened

    before = len(events)
    undo = client.post(f"/api/undo/{token}")
    assert undo.status_code == 200
    assert undo.json() == {"restored": True, "kind": "task"}

    # Restored with the SAME id, title, status and timestamps, and `task.changed`/restored fired.
    restored = store.get(iid)
    assert restored is not None
    assert restored["id"] == iid
    assert restored["title"] == "ship the thing"
    assert restored["status"] == "in_progress"
    assert restored["created_at"] == original["created_at"]
    assert restored["updated_at"] == original["updated_at"]
    assert restored["description"] == "do it" and restored["issue_type"] == "bug" and restored["priority"] == 1
    assert ("task.changed", {"id": iid, "action": "restored"}) in events[before:]

    # A second redeem of the same token is a 404 — undo is one-shot (r3).
    assert client.post(f"/api/undo/{token}").status_code == 404


def test_task_undo_refuses_to_clobber_a_recreated_id(tasks_client, events):
    """A task recreated under the same id between the delete and the undo is NOT overwritten:
    the restore refuses (409) and the token is then spent (a retry is a plain 404)."""
    client, store = tasks_client
    issue = store.create("old task")
    iid = issue["id"]

    token = client.delete(f"/api/tasks/issues/{iid}").json()["undo_token"]
    # Something takes the id back before the operator clicks Undo.
    store.restore({**issue, "title": "new task"})

    before = len(events)
    refused = client.post(f"/api/undo/{token}")
    assert refused.status_code == 409

    # The live row is untouched — the stashed snapshot did NOT overwrite it.
    live = store.get(iid)
    assert live is not None and live["title"] == "new task"
    assert ("task.changed", {"id": iid, "action": "restored"}) not in events[before:]
    # The token is spent — a retry is a plain 404, not another 409.
    assert client.post(f"/api/undo/{token}").status_code == 404


def test_store_restore_refuses_a_live_id(tmp_path):
    """``TaskStore.restore`` itself refuses an id that already exists (r3)."""
    store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    issue = store.create("x")
    with pytest.raises(ValueError, match="already exists"):
        store.restore(issue)


# ── schedules ───────────────────────────────────────────────────────────────


@pytest.fixture
def scheduler_client(tmp_path, monkeypatch):
    """A TestClient wired to the real scheduler-cancel handler over a real LocalScheduler."""
    import runtime.state as rs

    sched = LocalScheduler("t", invoke_url="http://127.0.0.1:1", db_dir=tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", sched, raising=False)
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        scheduler_list=ch._operator_scheduler_list,
        scheduler_add=ch._operator_scheduler_add,
        scheduler_cancel=ch._operator_scheduler_cancel,
    )
    return TestClient(app), sched


def test_schedule_cancel_returns_token_and_undo_restores(scheduler_client):
    client, sched = scheduler_client
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1", timezone="America/Chicago")

    deleted = client.delete("/api/scheduler/jobs/j1")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["canceled"] is True
    token = body["undo_token"]
    assert token and sched.get_job("j1") is None  # the cancel really happened

    undo = client.post(f"/api/undo/{token}")
    assert undo.status_code == 200
    assert undo.json() == {"restored": True, "kind": "schedule"}

    # The job is back with the SAME id, prompt, schedule and timezone.
    restored = sched.get_job("j1")
    assert restored is not None
    assert restored.id == "j1"
    assert restored.prompt == "nightly sweep"
    assert restored.schedule == "0 9 * * *"
    assert restored.timezone == "America/Chicago"

    # One-shot undo: a second redeem is a 404.
    assert client.post(f"/api/undo/{token}").status_code == 404


def test_schedule_undo_restores_a_past_one_shot(scheduler_client):
    """A one-shot whose time has ALREADY passed is restored as-is — its past fire time is
    kept (``add_job`` does not reject it), so the scheduler fires it on its next tick rather
    than dropping it."""
    client, sched = scheduler_client
    past = "2020-01-01T00:00:00+00:00"
    sched.add_job("run the overdue report", past, job_id="once")

    token = client.delete("/api/scheduler/jobs/once").json()["undo_token"]
    assert sched.get_job("once") is None

    assert client.post(f"/api/undo/{token}").json() == {"restored": True, "kind": "schedule"}
    restored = sched.get_job("once")
    assert restored is not None and restored.schedule == past


def test_schedule_undo_refuses_a_recreated_id(scheduler_client):
    """A job recreated under the same id before the undo is not clobbered — ``add_job``
    refuses a duplicate id, so the stale restore fails (a 400 at the route)."""
    client, sched = scheduler_client
    sched.add_job("old", "0 9 * * *", job_id="j1")

    token = client.delete("/api/scheduler/jobs/j1").json()["undo_token"]
    sched.add_job("new", "0 10 * * *", job_id="j1")  # the id is taken again

    refused = client.post(f"/api/undo/{token}")
    assert refused.status_code == 400
    # The live job is untouched.
    live = sched.get_job("j1")
    assert live is not None and live.prompt == "new"
    # The token is spent — a retry is a plain 404.
    assert client.post(f"/api/undo/{token}").status_code == 404


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("fence", [["current_time"], []])
def test_schedule_undo_preserves_execution_state(scheduler_client, enabled, fence):
    from graph.fence_scope import current_fence, fence_scope

    client, sched = scheduler_client
    with fence_scope(fence):
        sched.add_job(
            "report", "0 9 * * *", job_id="stateful", timezone="America/Chicago",
            context_id="target", origin_session="origin", ttl="7d", max_fires=5,
        )
    sched.set_enabled("stateful", enabled)
    # Seed a previously fired job, including its failure backoff and expiry clock.
    with sched._connect() as db:
        db.execute(
            "UPDATE jobs SET created_at = ?, next_fire = ?, last_fire = ?, "
            "fire_count = 4, consecutive_failures = 3, last_error = ?, last_ok = ? "
            "WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00",
             "2026-01-02T00:00:00+00:00", "turn failed",
             "2026-01-01T09:00:00+00:00", "stateful"),
        )
    original = sched.get_job("stateful").as_dict()
    token = client.delete("/api/scheduler/jobs/stateful").json()["undo_token"]
    assert current_fence() == []
    assert client.post(f"/api/undo/{token}").status_code == 200
    assert sched.get_job("stateful").as_dict() == original
    due = sched._claim_due_jobs(datetime(2026, 1, 4, tzinfo=UTC))
    assert [job.id for job in due] == (["stateful"] if enabled else [])


@pytest.mark.asyncio
async def test_restored_schedule_fire_carries_fence(scheduler_client, monkeypatch):
    import httpx
    from graph.fence_scope import fence_scope

    client, sched = scheduler_client
    with fence_scope(["current_time"]):
        sched.add_job("report", "2020-01-01T00:00:00+00:00", job_id="fenced")
    token = client.delete("/api/scheduler/jobs/fenced").json()["undo_token"]
    assert client.post(f"/api/undo/{token}").status_code == 200
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.post.return_value = httpx.Response(200, json={"result": {"status": {"state": "TASK_STATE_COMPLETED"}}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: http)
    (job,) = sched._claim_due_jobs(datetime.now(UTC))
    assert await sched._fire(job) is True
    message = http.post.call_args.kwargs["json"]["params"]["message"]
    assert message["metadata"]["subagent_fence"] == ["current_time"]


def test_expired_schedule_token_changes_nothing(scheduler_client, monkeypatch):
    client, sched = scheduler_client
    monkeypatch.setattr(LEDGER, "_clock", lambda: 0)
    sched.add_job("report", "0 9 * * *", job_id="expired")
    token = client.delete("/api/scheduler/jobs/expired").json()["undo_token"]
    monkeypatch.setattr(LEDGER, "_clock", lambda: LEDGER._window_s + 1)
    assert client.post(f"/api/undo/{token}").status_code == 404
    assert sched.get_job("expired") is None


# ── shared 404 ────────────────────────────────────────────────────────────────


def test_unknown_token_is_404(tasks_client):
    client, _ = tasks_client
    assert client.post("/api/undo/never-minted-this").status_code == 404


def test_expired_task_token_changes_nothing(tasks_client, events, monkeypatch):
    client, store = tasks_client
    monkeypatch.setattr(LEDGER, "_clock", lambda: 0)
    issue = store.create("expired task")
    token = client.delete(f"/api/tasks/issues/{issue['id']}").json()["undo_token"]
    before = list(events)
    monkeypatch.setattr(LEDGER, "_clock", lambda: LEDGER._window_s + 1)
    assert client.post(f"/api/undo/{token}").status_code == 404
    assert store.get(issue["id"]) is None
    assert events == before
