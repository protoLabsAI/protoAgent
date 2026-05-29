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
    def __init__(self, jid, prompt, schedule):
        self.id = jid
        self.prompt = prompt
        self.schedule = schedule
        self.next_fire = "2026-06-01T09:00:00+00:00"
        self.enabled = True

    def as_dict(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "schedule": self.schedule,
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

    register_operator_routes(
        app,
        runtime_status=lambda: {"graph_loaded": True},
        subagent_list=lambda: [],
        subagent_run=lambda req: None,
        subagent_batch=lambda req: None,
        scheduler_list=_list,
        scheduler_add=_add,
        scheduler_cancel=_cancel,
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
    assert TestClient(app).get("/api/scheduler/jobs").status_code == 404
