"""Pause/resume route + the ``scheduler.changed`` bus event (#4955 Q7–Q8, #1537).

* ``POST /api/scheduler/jobs/{id}/enabled`` pauses (``enabled=false``) / resumes
  (``enabled=true``) a job through the backend's optional ``set_enabled``. An unknown id is a
  404; a backend without ``set_enabled`` is a 501.
* add / update / cancel / pause / resume / undo-restore each publish exactly one
  ``scheduler.changed`` event with the matching action, and a publisher that raises never
  changes the route's response.

Wires the real ``console_handlers`` over a real tmp ``LocalScheduler`` (like
tests/test_operator_undo_tasks_schedules.py), capturing bus events off ``_event_bus.publish``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api import console_handlers as ch
from operator_api.routes import register_operator_routes
from operator_api.undo import LEDGER
from scheduler.local import LocalScheduler


@pytest.fixture(autouse=True)
def _clean_ledger():
    """Isolate each test from undo tokens minted by another (shared process-local singleton)."""
    LEDGER._entries.clear()
    yield
    LEDGER._entries.clear()


@pytest.fixture
def events(monkeypatch):
    """Capture the bus events the handlers publish (``scheduler.changed``, …)."""
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ch._event_bus, "publish", lambda topic, data=None, **kw: seen.append((topic, data))
    )
    return seen


def _changed(events: list[tuple[str, dict]]) -> list[dict]:
    """Just the ``scheduler.changed`` payloads, in order."""
    return [data for topic, data in events if topic == "scheduler.changed"]


@pytest.fixture
def scheduler_client(tmp_path, monkeypatch):
    """A TestClient wired to the real scheduler handlers over a real tmp ``LocalScheduler``."""
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
        scheduler_update=ch._operator_scheduler_update,
        scheduler_set_enabled=ch._operator_scheduler_set_enabled,
    )
    return TestClient(app), sched


# ── pause / resume ──────────────────────────────────────────────────────────


def test_pause_then_resume(scheduler_client):
    client, sched = scheduler_client
    sched.add_job("nightly sweep", "0 9 * * *", job_id="j1")

    paused = client.post("/api/scheduler/jobs/j1/enabled", json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["job"]["enabled"] is False
    assert sched.get_job("j1").enabled is False

    resumed = client.post("/api/scheduler/jobs/j1/enabled", json={"enabled": True})
    assert resumed.status_code == 200
    assert resumed.json()["job"]["enabled"] is True
    assert sched.get_job("j1").enabled is True


def test_unknown_id_is_404(scheduler_client):
    client, _ = scheduler_client
    resp = client.post("/api/scheduler/jobs/nope/enabled", json={"enabled": False})
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_backend_without_set_enabled_is_501(monkeypatch):
    """A backend that doesn't implement ``set_enabled`` answers 501, not a 500."""
    import runtime.state as rs

    class _NoPauseScheduler:
        name = "local"

        def list_jobs(self):
            return []

    monkeypatch.setattr(rs.STATE, "scheduler", _NoPauseScheduler(), raising=False)
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        scheduler_set_enabled=ch._operator_scheduler_set_enabled,
    )
    resp = TestClient(app).post("/api/scheduler/jobs/j1/enabled", json={"enabled": False})
    assert resp.status_code == 501
    assert "pause/resume" in resp.json()["detail"]


def test_route_absent_when_accessor_not_wired():
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
    )
    resp = TestClient(app).post("/api/scheduler/jobs/x/enabled", json={"enabled": False})
    assert resp.status_code == 404


# ── scheduler.changed, one per action ─────────────────────────────────────────


def test_add_emits_added(scheduler_client, events):
    client, _ = scheduler_client
    before = len(events)
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "added"}]


def test_update_emits_updated(scheduler_client, events):
    client, _ = scheduler_client
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    before = len(events)
    client.put("/api/scheduler/jobs/j1", json={"schedule": "0 17 * * *"})
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "updated"}]


def test_cancel_emits_canceled(scheduler_client, events):
    client, _ = scheduler_client
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    before = len(events)
    client.delete("/api/scheduler/jobs/j1")
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "canceled"}]


def test_pause_emits_disabled_resume_emits_enabled(scheduler_client, events):
    client, _ = scheduler_client
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})

    before = len(events)
    client.post("/api/scheduler/jobs/j1/enabled", json={"enabled": False})
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "disabled"}]

    before = len(events)
    client.post("/api/scheduler/jobs/j1/enabled", json={"enabled": True})
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "enabled"}]


def test_undo_restore_emits_restored(scheduler_client, events):
    client, _ = scheduler_client
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    token = client.delete("/api/scheduler/jobs/j1").json()["undo_token"]

    before = len(events)
    assert client.post(f"/api/undo/{token}").status_code == 200
    assert _changed(events[before:]) == [{"job_id": "j1", "action": "restored"}]


# ── a failing publisher never changes the response (r4) ───────────────────────


def test_failing_publisher_does_not_break_pause(scheduler_client, monkeypatch):
    client, sched = scheduler_client
    sched.add_job("p", "0 9 * * *", job_id="j1")

    def _boom(topic, data=None, **kw):
        raise RuntimeError("bus down")

    monkeypatch.setattr(ch._event_bus, "publish", _boom)
    resp = client.post("/api/scheduler/jobs/j1/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["job"]["enabled"] is False
    assert sched.get_job("j1").enabled is False
