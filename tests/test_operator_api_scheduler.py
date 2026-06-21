"""Tests for the scheduler operator-API routes.

Registers the routes against a FastAPI TestClient backed by a fake in-memory
scheduler that mirrors the SchedulerBackend contract (add/list/cancel, ValueError
on malformed schedule). Mirrors tests/test_operator_api_routes.py.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import register_operator_routes


class _FakeJob:
    def __init__(self, jid, prompt, schedule, timezone=None):
        self.id = jid
        self.prompt = prompt
        self.schedule = schedule
        self.timezone = timezone
        self.next_fire = "2026-06-01T09:00:00+00:00"
        self.enabled = True

    def as_dict(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "timezone": self.timezone,
            "next_fire": self.next_fire,
            "enabled": self.enabled,
        }


class _FakeScheduler:
    name = "local"

    def __init__(self):
        self._jobs: dict[str, _FakeJob] = {}
        self._n = 0

    def list_jobs(self):
        return list(self._jobs.values())

    def add_job(self, prompt, schedule, *, job_id=None):
        if schedule == "bad":
            raise ValueError("malformed schedule")
        self._n += 1
        jid = job_id or f"job-{self._n}"
        job = _FakeJob(jid, prompt, schedule)
        self._jobs[jid] = job
        return job

    def cancel_job(self, job_id):
        return self._jobs.pop(job_id, None) is not None

    def update_job(self, job_id, prompt, schedule, *, timezone=None):
        if schedule == "bad":
            raise ValueError("malformed schedule")
        if job_id not in self._jobs:
            raise ValueError(f"no job {job_id!r} to update")
        job = self._jobs[job_id]
        job.prompt, job.schedule, job.timezone = prompt, schedule, timezone
        return job


def _client(scheduler=None):
    import asyncio

    app = FastAPI()
    sched = scheduler  # None → no-backend behavior

    async def _list():
        if sched is None:
            return {"jobs": [], "backend": "disabled"}
        jobs = await asyncio.to_thread(sched.list_jobs)
        return {"jobs": [j.as_dict() for j in jobs], "backend": sched.name}

    async def _add(req):
        if sched is None:
            raise RuntimeError("scheduler is not loaded")
        if not req.get("prompt"):
            raise ValueError("prompt is required")
        job = await asyncio.to_thread(sched.add_job, req["prompt"], req["schedule"], job_id=req.get("job_id") or None)
        return job.as_dict()

    async def _cancel(job_id):
        if sched is None:
            raise RuntimeError("scheduler is not loaded")
        return {"canceled": bool(await asyncio.to_thread(sched.cancel_job, job_id))}

    async def _update(job_id, req):
        if sched is None:
            raise RuntimeError("scheduler is not loaded")
        if not req.get("prompt"):
            raise ValueError("prompt is required")
        job = await asyncio.to_thread(
            sched.update_job, job_id, req["prompt"], req["schedule"], timezone=req.get("timezone") or None
        )
        return job.as_dict()

    register_operator_routes(
        app,
        runtime_status=lambda: {"graph_loaded": True},
        subagent_list=lambda: [],
        subagent_run=lambda req: None,
        subagent_batch=lambda req: None,
        scheduler_list=_list,
        scheduler_add=_add,
        scheduler_cancel=_cancel,
        scheduler_update=_update,
    )
    return TestClient(app)


def test_list_empty_then_populated() -> None:
    sched = _FakeScheduler()
    client = _client(sched)

    assert client.get("/api/scheduler/jobs").json() == {"jobs": [], "backend": "local"}

    created = client.post("/api/scheduler/jobs", json={"prompt": "sweep", "schedule": "0 9 * * *"})
    assert created.status_code == 200
    job = created.json()["job"]
    assert job["prompt"] == "sweep" and job["schedule"] == "0 9 * * *" and job["next_fire"]

    listed = client.get("/api/scheduler/jobs").json()
    assert len(listed["jobs"]) == 1 and listed["jobs"][0]["id"] == job["id"]


def test_add_honors_job_id_and_cancel() -> None:
    client = _client(_FakeScheduler())
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "* * * * *", "job_id": "nightly"})
    assert any(j["id"] == "nightly" for j in client.get("/api/scheduler/jobs").json()["jobs"])

    assert client.delete("/api/scheduler/jobs/nightly").json() == {"canceled": True}
    assert client.delete("/api/scheduler/jobs/nightly").json() == {"canceled": False}
    assert client.get("/api/scheduler/jobs").json()["jobs"] == []


def test_put_updates_in_place() -> None:
    client = _client(_FakeScheduler())
    client.post("/api/scheduler/jobs", json={"prompt": "old", "schedule": "0 9 * * *", "job_id": "j1"})

    resp = client.put(
        "/api/scheduler/jobs/j1",
        json={"prompt": "new", "schedule": "0 17 * * 1-5", "timezone": "America/Chicago"},
    )
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["id"] == "j1" and job["prompt"] == "new" and job["schedule"] == "0 17 * * 1-5"
    assert job["timezone"] == "America/Chicago"

    # Still one job, edited in place (not a new row).
    listed = client.get("/api/scheduler/jobs").json()["jobs"]
    assert len(listed) == 1 and listed[0]["id"] == "j1" and listed[0]["prompt"] == "new"


def test_put_missing_job_is_400() -> None:
    client = _client(_FakeScheduler())
    resp = client.put("/api/scheduler/jobs/nope", json={"prompt": "p", "schedule": "0 9 * * *"})
    assert resp.status_code == 400
    assert "no job" in resp.json()["detail"]


def test_put_malformed_schedule_is_400() -> None:
    client = _client(_FakeScheduler())
    client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *", "job_id": "j1"})
    assert client.put("/api/scheduler/jobs/j1", json={"prompt": "p", "schedule": "bad"}).status_code == 400


def test_malformed_schedule_is_400() -> None:
    client = _client(_FakeScheduler())
    resp = client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "bad"})
    assert resp.status_code == 400
    assert "malformed" in resp.json()["detail"]


def test_no_backend_paths() -> None:
    client = _client(None)
    # list is graceful
    assert client.get("/api/scheduler/jobs").json() == {"jobs": [], "backend": "disabled"}
    # add maps RuntimeError "not loaded" → 409
    assert client.post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "* * * * *"}).status_code == 409


def test_routes_absent_when_accessors_not_wired() -> None:
    # When scheduler accessors aren't passed, the routes shouldn't exist.
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda req: None,
        subagent_batch=lambda req: None,
    )
    client = TestClient(app)
    assert client.get("/api/scheduler/jobs").status_code == 404
    assert client.put("/api/scheduler/jobs/x", json={"prompt": "p", "schedule": "0 9 * * *"}).status_code == 404
