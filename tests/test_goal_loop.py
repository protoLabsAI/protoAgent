"""One-call goal-driven recurring loop helper (graph.sdk.start_goal_loop / stop_goal_loop).

Composes the goal controller (set a monitor goal, ADR 0028/0030) + the scheduler (a recurring
tick, ADR 0003/0053) so a plugin declares a self-driving OODA loop in one call. Tested by
faking the two host singletons on ``STATE`` — no live controller/scheduler needed.
"""

from __future__ import annotations

import pytest

from graph import sdk
from graph.sdk import _to_cron, run_in_session, start_goal_loop, stop_goal_loop
from runtime.state import STATE
from scheduler.interface import is_cron


# ── _to_cron ─────────────────────────────────────────────────────────────────────────
def test_to_cron_passes_through_a_cron_expression():
    assert _to_cron("0 */6 * * *") == "0 */6 * * *"


@pytest.mark.parametrize(
    "every,expected",
    [
        ("15m", "*/15 * * * *"),
        ("30m", "*/30 * * * *"),
        ("2h", "0 */2 * * *"),
        ("1d", "0 0 */1 * *"),
        ("  45m ", "*/45 * * * *"),
    ],
)
def test_to_cron_converts_duration_shorthand(every, expected):
    assert _to_cron(every) == expected


@pytest.mark.parametrize("bad", ["", "soon", "0m", "60m", "24h", "32d", "2025-01-01T00:00"])
def test_to_cron_rejects_bad_durations(bad):
    with pytest.raises(ValueError):
        _to_cron(bad)


# ── fakes for the two host singletons ────────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.cleared: list[str] = []

    def clear(self, sid):
        self.cleared.append(sid)
        return True


class _Controller:
    def __init__(self, ok=True, msg="Goal set."):
        self._ok, self._msg = ok, msg
        self.calls: list[dict] = []
        self.store = _Store()

    def set_goal_safe(self, session_id, condition, verifier, max_iterations=None, no_progress_limit=None, mode="drive"):
        self.calls.append({"session_id": session_id, "condition": condition, "verifier": verifier, "mode": mode})
        return (self._ok, self._msg)


class _Job:
    def __init__(self, jid):
        self.id = jid


class _Scheduler:
    def __init__(self, raise_on_add=False):
        self.added: list[dict] = []
        self.cancelled: list[str] = []
        self._raise = raise_on_add

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        if self._raise:
            raise ValueError("bad timezone")
        self.added.append(
            {"prompt": prompt, "schedule": schedule, "job_id": job_id, "timezone": timezone, "context_id": context_id}
        )
        return _Job(job_id or "job-1")

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True


@pytest.fixture
def wired(monkeypatch):
    ctrl, sched = _Controller(), _Scheduler()
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(STATE, "scheduler", sched)
    return ctrl, sched


# ── start_goal_loop ──────────────────────────────────────────────────────────────────
def test_start_goal_loop_sets_a_monitor_goal_and_schedules_the_tick(wired):
    ctrl, sched = wired
    res = start_goal_loop(
        session_id="sess-1",
        goal="reach 1,000,000 credits",
        verifier="spacetraders:credits",
        verifier_args={"min": 1_000_000},
        every="30m",
        prompt="Run the OODA tick and report.",
    )
    assert res["ok"] and res["job_id"] == "job-1" and res["schedule"] == "*/30 * * * *"
    # the goal is a MONITOR goal verified by the plugin verifier
    call = ctrl.calls[0]
    assert call["mode"] == "monitor"
    assert call["verifier"] == {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1_000_000}}
    # the tick fires back INTO the goal's session (so it drives the right goal)
    assert sched.added[0]["context_id"] == "sess-1"
    assert sched.added[0]["schedule"] == "*/30 * * * *"


def test_bad_schedule_does_not_set_a_goal(wired):
    ctrl, sched = wired
    res = start_goal_loop(session_id="s", goal="g", verifier="p:v", every="whenever", prompt="tick")
    assert not res["ok"]
    assert ctrl.calls == [] and sched.added == []  # nothing wired on bad input


def test_goal_rejected_means_no_job_scheduled(monkeypatch):
    ctrl = _Controller(ok=False, msg="verifier not found")
    sched = _Scheduler()
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(STATE, "scheduler", sched)
    res = start_goal_loop(session_id="s", goal="g", verifier="p:missing", every="15m", prompt="tick")
    assert not res["ok"] and "goal not set" in res["message"]
    assert sched.added == []


def test_scheduling_failure_rolls_back_the_goal(monkeypatch):
    ctrl = _Controller()
    sched = _Scheduler(raise_on_add=True)
    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    monkeypatch.setattr(STATE, "scheduler", sched)
    res = start_goal_loop(
        session_id="sess-1", goal="g", verifier="p:v", every="15m", prompt="tick", timezone="Bad/Zone"
    )
    assert not res["ok"]
    assert ctrl.store.cleared == ["sess-1"]  # goal rolled back so we don't strand it


def test_unavailable_subsystems_are_reported(monkeypatch):
    monkeypatch.setattr(STATE, "goal_controller", None)
    monkeypatch.setattr(STATE, "scheduler", _Scheduler())
    assert not start_goal_loop(session_id="s", goal="g", verifier="p:v", every="15m", prompt="t")["ok"]
    monkeypatch.setattr(STATE, "goal_controller", _Controller())
    monkeypatch.setattr(STATE, "scheduler", None)
    assert not start_goal_loop(session_id="s", goal="g", verifier="p:v", every="15m", prompt="t")["ok"]


# ── stop_goal_loop ───────────────────────────────────────────────────────────────────
def test_stop_goal_loop_clears_goal_and_cancels_job(wired):
    ctrl, sched = wired
    res = stop_goal_loop(session_id="sess-1", job_id="job-1")
    assert res["ok"] and res["goal_cleared"] and res["job_cancelled"]
    assert ctrl.store.cleared == ["sess-1"] and sched.cancelled == ["job-1"]


def test_stop_goal_loop_without_job_id_just_clears(wired):
    ctrl, sched = wired
    res = stop_goal_loop(session_id="sess-1")
    assert res["goal_cleared"] and not res["job_cancelled"]
    assert sched.cancelled == []


def test_sdk_module_exposes_the_helpers():
    assert callable(sdk.start_goal_loop) and callable(sdk.stop_goal_loop)


# ── run_in_session (goal fires → run a prompt as an agent turn) ───────────────────────
def test_run_in_session_enqueues_a_one_shot_into_the_session(wired):
    _ctrl, sched = wired
    res = run_in_session("sess-9", "Summarize what just happened and open the next PR.")
    assert res["ok"] and res["job_id"] == "job-1"
    add = sched.added[0]
    assert add["context_id"] == "sess-9"  # fires into the goal's OWN session
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
    _ctrl, sched = wired
    run_in_session("s", "p", job_id="reaction-1")
    assert sched.cancelled == ["reaction-1"]  # idempotent: drop any existing before re-adding
    assert sched.added[0]["job_id"] == "reaction-1"


def test_sdk_module_exposes_run_in_session():
    assert callable(sdk.run_in_session)
