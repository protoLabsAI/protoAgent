"""sdk.run_in_session — enqueue a one-shot agent turn into a session (#1494).

The monitor-goal ``start_goal_loop``/``stop_goal_loop`` helpers were retired with goal
``monitor`` mode (ADR 0067 — a metric to watch over time is now a *watch*,
``sdk.create_watch``). This covers the surviving ``run_in_session`` primitive.
"""

from __future__ import annotations

import pytest

from graph import sdk
from graph.sdk import run_in_session
from runtime.state import STATE
from scheduler.interface import is_cron


class _Job:
    def __init__(self, jid):
        self.id = jid


class _Scheduler:
    def __init__(self):
        self.added: list[dict] = []
        self.cancelled: list[str] = []

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        self.added.append(
            {"prompt": prompt, "schedule": schedule, "job_id": job_id, "timezone": timezone, "context_id": context_id}
        )
        return _Job(job_id or "job-1")

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True


@pytest.fixture
def wired(monkeypatch):
    sched = _Scheduler()
    monkeypatch.setattr(STATE, "scheduler", sched)
    return sched


def test_run_in_session_enqueues_a_one_shot_into_the_session(wired):
    res = run_in_session("sess-9", "Summarize what just happened and open the next PR.")
    assert res["ok"] and res["job_id"] == "job-1"
    add = wired.added[0]
    assert add["context_id"] == "sess-9"  # fires into the target session
    assert add["prompt"].startswith("Summarize")
    assert not is_cron(add["schedule"])  # a one-shot ISO fire time, not a recurring cron


def test_run_in_session_requires_scheduler_and_inputs(monkeypatch):
    monkeypatch.setattr(STATE, "scheduler", None)
    assert not run_in_session("s", "p")["ok"]  # no scheduler
    sched = _Scheduler()
    monkeypatch.setattr(STATE, "scheduler", sched)
    assert not run_in_session("", "p")["ok"]  # empty session
    assert not run_in_session("s", "  ")["ok"]  # empty prompt
    assert sched.added == []  # nothing enqueued on bad input


def test_run_in_session_job_id_replaces_the_pending_one_shot(wired):
    run_in_session("s", "p", job_id="reaction-1")
    assert wired.cancelled == ["reaction-1"]  # idempotent: drop any existing before re-adding
    assert wired.added[0]["job_id"] == "reaction-1"


def test_sdk_module_exposes_run_in_session():
    assert callable(sdk.run_in_session)
