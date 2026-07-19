"""sdk.run_in_session + the goal-loop sugar (#1494, #2060).

The original monitor-goal ``start_goal_loop``/``stop_goal_loop`` were retired with goal
``monitor`` mode (ADR 0067 — a metric to watch over time is a *watch*). #2060 brought the
names back as WATCH-based sugar: ``start_goal_loop`` = ``create_watch`` +
``schedule_recurring`` under a shared derived id, ``stop_goal_loop`` cancels both. This
covers that pair, the ``_to_cron`` shorthand, and the ``run_in_session`` primitive.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph import sdk
from graph.config import LangGraphConfig
from graph.sdk import run_in_session, start_goal_loop, stop_goal_loop
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from runtime.state import STATE
from scheduler.interface import is_cron


class _Job:
    def __init__(self, jid):
        self.id = jid


class _Scheduler:
    def __init__(self):
        self.added: list[dict] = []
        self.cancelled: list[str] = []
        self.jobs: dict[str, bool] = {}
        self.fail_add = False

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        if self.fail_add:
            raise ValueError("scheduler exploded")
        self.added.append(
            {"prompt": prompt, "schedule": schedule, "job_id": job_id, "timezone": timezone, "context_id": context_id}
        )
        jid = job_id or f"job-{len(self.added)}"
        self.jobs[jid] = True
        return _Job(jid)

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return self.jobs.pop(job_id, None) is not None


@pytest.fixture
def wired(monkeypatch):
    sched = _Scheduler()
    monkeypatch.setattr(STATE, "scheduler", sched)
    return sched


@pytest.fixture
def looped(wired, monkeypatch, tmp_path):
    """Scheduler fake + a REAL WatchController on a tmp store — the loop sugar composes
    create_watch, so exercising the real create/clear path catches contract drift."""
    ctrl = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    monkeypatch.setattr(STATE, "watch_controller", ctrl)
    return SimpleNamespace(sched=wired, watches=ctrl)


# --- _to_cron ---------------------------------------------------------------


def test_to_cron_passes_through_a_cron_expression():
    assert sdk._to_cron("0 */6 * * *") == "0 */6 * * *"


@pytest.mark.parametrize(
    ("every", "expected"),
    [("15m", "*/15 * * * *"), ("30M", "*/30 * * * *"), ("2h", "0 */2 * * *"), ("1d", "0 0 */1 * *")],
)
def test_to_cron_converts_duration_shorthand(every, expected):
    assert sdk._to_cron(every) == expected


@pytest.mark.parametrize("bad", ["", "soon", "90m", "24h", "40d", "0m", "2026-01-01T00:00:00"])
def test_to_cron_rejects_bad_durations(bad):
    with pytest.raises(ValueError):
        sdk._to_cron(bad)


# --- start_goal_loop --------------------------------------------------------

_LOOP = dict(
    goal="reach 1M credits",
    verifier="fleet:credits",
    verifier_args={"min": 1_000_000},
    every="30m",
    prompt="Run the OODA tick and report.",
    plugin_id="fleet",
    loop_id="credits-1m",
    session_id="sess-7",
)


def test_start_goal_loop_arms_a_watch_and_schedules_the_tick(looped):
    res = start_goal_loop(**_LOOP)
    assert res["ok"], res["message"]
    assert res["watch_id"] == "fleet:goal-loop:credits-1m"
    assert res["job_id"] == "plugin:fleet:goal-loop:credits-1m"
    assert res["schedule"] == "*/30 * * * *"
    (watch,) = looped.watches.list_watches()
    assert watch.id == "fleet:goal-loop:credits-1m"
    assert watch.condition == "reach 1M credits"
    assert watch.verifier == {"type": "plugin", "check": "fleet:credits", "args": {"min": 1_000_000}}
    (add,) = looped.sched.added
    assert add["job_id"] == "plugin:fleet:goal-loop:credits-1m"
    assert add["schedule"] == "*/30 * * * *" and is_cron(add["schedule"])
    assert add["context_id"] == "sess-7"  # the tick fires INTO the caller's session


def test_start_goal_loop_is_idempotent_by_ids(looped):
    start_goal_loop(**_LOOP)
    res = start_goal_loop(**{**_LOOP, "every": "1h"})
    assert res["ok"]
    assert len(looped.watches.list_watches()) == 1  # replaced, not duplicated
    # schedule_recurring dropped the pending tick before re-adding (idempotent re-arm)
    assert "plugin:fleet:goal-loop:credits-1m" in looped.sched.cancelled
    assert looped.sched.added[-1]["schedule"] == "0 */1 * * *"


def test_start_goal_loop_done_prompt_rides_the_watch(looped):
    res = start_goal_loop(**_LOOP, done_prompt="Wrap up and congratulate.")
    assert res["ok"]
    (watch,) = looped.watches.list_watches()
    assert watch.run_prompt == "Wrap up and congratulate."
    assert watch.run_session == "sess-7"


def test_start_goal_loop_done_prompt_requires_a_session(looped):
    res = start_goal_loop(**{**_LOOP, "session_id": ""}, done_prompt="Wrap up.")
    assert not res["ok"] and "session_id" in res["message"]
    assert looped.watches.list_watches() == [] and looped.sched.added == []


def test_start_goal_loop_validates_ids_and_schedule(looped):
    assert not start_goal_loop(**{**_LOOP, "plugin_id": "a:b"})["ok"]  # ':' breaks namespacing
    assert not start_goal_loop(**{**_LOOP, "plugin_id": " "})["ok"]
    assert not start_goal_loop(**{**_LOOP, "loop_id": ""})["ok"]
    assert not start_goal_loop(**{**_LOOP, "every": "soon"})["ok"]
    assert looped.watches.list_watches() == [] and looped.sched.added == []  # nothing half-armed


def test_start_goal_loop_watch_refused_means_no_tick(looped):
    res = start_goal_loop(**{**_LOOP, "verifier": ""})  # plugin verifier needs a check
    assert not res["ok"] and "goal watch not set" in res["message"]
    assert looped.sched.added == []


def test_start_goal_loop_schedule_failure_rolls_back_the_watch(looped):
    looped.sched.fail_add = True
    res = start_goal_loop(**_LOOP)
    assert not res["ok"] and "tick not scheduled" in res["message"]
    assert looped.watches.list_watches() == []  # never half a loop


def test_start_goal_loop_reports_unavailable_subsystems(looped, monkeypatch):
    monkeypatch.setattr(STATE, "watch_controller", None)
    assert "watch system unavailable" in start_goal_loop(**_LOOP)["message"]
    monkeypatch.setattr(STATE, "watch_controller", looped.watches)
    monkeypatch.setattr(STATE, "scheduler", None)
    res = start_goal_loop(**_LOOP)
    assert not res["ok"]
    assert looped.watches.list_watches() == []  # rolled back when the tick half is off


# --- stop_goal_loop ---------------------------------------------------------


def test_stop_goal_loop_cancels_the_tick_and_clears_the_watch(looped):
    start_goal_loop(**_LOOP)
    res = stop_goal_loop(plugin_id="fleet", loop_id="credits-1m")
    assert res["ok"] and res["job_cancelled"] and res["watch_cleared"]
    assert looped.watches.list_watches() == []
    assert looped.sched.cancelled[-1] == "plugin:fleet:goal-loop:credits-1m"


def test_stop_goal_loop_is_idempotent_on_an_absent_loop(looped):
    res = stop_goal_loop(plugin_id="fleet", loop_id="never-started")
    assert res["ok"] and not res["job_cancelled"] and not res["watch_cleared"]


def test_stop_goal_loop_validates_ids(looped):
    assert not stop_goal_loop(plugin_id="a:b", loop_id="x")["ok"]
    assert not stop_goal_loop(plugin_id="fleet", loop_id="")["ok"]


# --- run_in_session ---------------------------------------------------------


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


def test_sdk_module_exposes_the_helpers():
    assert callable(sdk.run_in_session)
    assert callable(sdk.start_goal_loop)
    assert callable(sdk.stop_goal_loop)
