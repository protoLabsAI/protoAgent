"""A long-running accepted turn is 'still running', not ERROR 'delivery unknown'.

The production failure (bd-snei6): a recurring job whose turn outlived
``SCHEDULER_FIRE_TIMEOUT_S`` logged ``ERROR … delivery unknown; not automatically
retried`` with a TimeoutError traceback on every fire — 74 times over a weekend —
yet every occurrence later settled ``completed``. An ACCEPTED send (a task handle
exists) whose bounded GetTask wait merely ran out is still running: no error, no
``jobs.last_error``, no failure streak; the occurrence goes ``running`` and the
existing reconciliation settles it — and publishes ``turn.finished`` — exactly once
when the task ends. 'delivery unknown' remains reserved for real unknowns (no task
id after the send, a transport failure before acceptance).

A real ``LocalScheduler`` over a temp ``jobs.db``; only the network edge is a
stateful fake A2A endpoint whose task — the slow model turn — stays WORKING until
the test ends it. ``_fire`` runs unmocked, bounded wait and all.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import scheduler.local as scheduler_local
from scheduler.local import LocalScheduler

_DELIVERY_UNKNOWN = "delivery unknown; not automatically retried"


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _SlowA2A:
    """The agent's A2A edge: SendMessage is ACCEPTED immediately (``returnImmediately``
    shape, durable task id) but the turn is slow — GetTask answers WORKING until the
    test completes the task. With ``accept = False`` the send answers 200 with no task
    at all (the real-unknown control)."""

    def __init__(self):
        self.task_states: dict[str, str] = {}
        self.sends: list[str] = []
        self.accept = True

    def client(self, **_kw) -> "_SlowA2A":
        return self

    async def __aenter__(self) -> "_SlowA2A":
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, url, headers=None, json=None) -> _Response:
        if json.get("method") == "SendMessage":
            self.sends.append(json["params"]["message"]["messageId"])
            if not self.accept:
                return _Response({"result": {}})
            task_id = f"task-{self.sends[-1]}"
            self.task_states.setdefault(task_id, "TASK_STATE_WORKING")
        else:
            task_id = json["params"]["id"]
        state = self.task_states.get(task_id, "TASK_STATE_WORKING")
        return _Response({"result": {"task": {"id": task_id, "status": {"state": state}}}})


def _scheduler(tmp_path, monkeypatch, fake: _SlowA2A, events: list | None = None) -> LocalScheduler:
    import httpx

    monkeypatch.setenv("SCHEDULER_FIRE_TIMEOUT_S", "0.2")  # a small bounded fire wait
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    publish = (lambda topic, data: events.append((topic, data))) if events is not None else None
    s = LocalScheduler(agent_name="long-turn", invoke_url="http://agent", api_key="k",
                       bearer_token="b", db_dir=tmp_path, event_publish=publish)
    assert s._fire_timeout_s == 0.2
    return s


def _occurrence(s: LocalScheduler, job_id: str) -> sqlite3.Row | None:
    with s._connect() as db:
        return db.execute("SELECT * FROM occurrences WHERE job_id = ?", (job_id,)).fetchone()


def _rewind(s: LocalScheduler, job_id: str) -> None:
    with s._connect() as db:
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?",
                   ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(), job_id))


async def _drain(s: LocalScheduler) -> None:
    """One poll tick, then let every spawned fire/reconcile task finish."""
    await s._tick()
    while s._fire_tasks:
        await asyncio.gather(*list(s._fire_tasks))


async def _drain_until(s: LocalScheduler, pred, message: str, rounds: int = 200) -> None:
    """Tick the (unstarted) scheduler until ``pred`` holds — reconciliation only runs
    from ticks, and explicit ticks keep the test deterministic."""
    for _ in range(rounds):
        await _drain(s)
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for: {message}")


@pytest.mark.asyncio
async def test_long_running_accepted_turn_is_running_not_delivery_unknown(tmp_path, monkeypatch, caplog):
    fake = _SlowA2A()
    events: list = []
    s = _scheduler(tmp_path, monkeypatch, fake, events)
    job = s.add_job("sweep the pr queue", "* * * * *", job_id="job-long")
    _rewind(s, job.id)

    with caplog.at_level(logging.INFO, logger="scheduler.local"):
        await _drain(s)  # the fire's bounded wait runs out while the turn is mid-flight

        # r1: still running — an INFO line, no ERROR, no traceback, no last_error,
        # and the occurrence keeps its task handle for reconciliation.
        claim = _occurrence(s, job.id)
        task_id = claim["task_id"]
        assert claim["state"] == "running" and task_id
        assert "still running after" in caplog.text
        assert "delivery unknown" not in caplog.text
        assert [r for r in caplog.records if r.name == "scheduler.local"
                and (r.levelno >= logging.ERROR or r.exc_info)] == []
        row = s.get_job(job.id)
        assert row.last_error is None
        assert row.consecutive_failures == 0

        # r3: the ownership hold stays while it runs — the next cron slot waits.
        assert s._claim_due_jobs(datetime.now(UTC) + timedelta(days=2)) == []
        await _drain(s)
        assert fake.sends == [claim["message_id"]]  # nothing re-fired or resent meanwhile

        # The slow turn finally ends; later ticks settle it via GetTask reconciliation.
        fake.task_states[task_id] = "TASK_STATE_COMPLETED"
        await _drain_until(s, lambda: (_occurrence(s, job.id) or {"state": ""})["state"] == "settled",
                           "the running occurrence settled once its task ended")

    settled = _occurrence(s, job.id)
    assert settled["task_state"] == "completed"
    assert settled["task_id"] == task_id
    row = s.get_job(job.id)
    assert row.last_error is None and row.last_ok
    assert row.fire_count == 1 and row.consecutive_failures == 0

    # r2: turn.finished exactly once, at real settlement, addressed to this task.
    finished = [data for topic, data in events if topic == "turn.finished"]
    assert [f["task_id"] for f in finished] == [task_id]
    assert finished[0]["ok"] is True

    # r3: once settled, the same job's next due slot fires normally.
    _rewind(s, job.id)
    await _drain(s)
    assert len(fake.sends) == 2


@pytest.mark.asyncio
async def test_send_without_task_id_is_still_delivery_unknown(tmp_path, monkeypatch, caplog):
    fake = _SlowA2A()
    fake.accept = False  # 200, but no task handle anywhere in the body
    s = _scheduler(tmp_path, monkeypatch, fake)
    job = s.add_job("opaque send", (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
                    job_id="job-opaque")

    with caplog.at_level(logging.INFO, logger="scheduler.local"):
        await _drain(s)

    claim = _occurrence(s, job.id)
    assert claim["state"] == "unknown" and claim["task_id"] is None
    assert s.get_job(job.id).last_error == _DELIVERY_UNKNOWN
    assert "still running after" not in caplog.text  # never mistaken for an accepted turn
    await _drain(s)
    assert fake.sends == [claim["message_id"]]  # quarantined: never automatically resent
