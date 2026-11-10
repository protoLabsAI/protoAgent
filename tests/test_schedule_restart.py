"""schedule_restart — the agent self-restart scheduler (operator_api/runtime_routes, #4577).

``POST /api/restart`` 409s unless the agent is idle at request time; a tool always runs
inside a turn, so ``schedule_restart`` writes the audit row and then arms the graceful
re-exec from a background task that waits for the turn to finish. These tests never really
exit: ``restart_readiness``, ``request_server_exit`` and the sleep are all stubbed, and the
background wait-then-arm task is captured and driven by hand (as test_restart_route.py does
for the when-idle drain)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from operator_api import runtime_routes
from runtime.state import STATE


def _readiness(*, idle: bool, supervised: bool = True) -> dict:
    """Shape a restart_readiness() reply, mirroring the real keys the scheduler reads."""
    return {
        "idle": idle,
        "busy": [] if idle else ["a chat turn is running"],
        "supervised": supervised,
        "how_to": "" if supervised else "Stop the server and start it again the way you launched it.",
        "boot_id": "test",
    }


def _schedule_and_drive(monkeypatch, *, readiness, sleep, wait_s=600, actor="agent", reason="plugin updated"):
    """Call schedule_restart with stubbed readiness/exit/sleep, then capture and await the
    background task it schedules (if any). ``readiness`` is a zero-arg callable returning the
    dict restart_readiness would. Returns (result, exited_mock)."""
    exited = Mock(return_value=True)
    monkeypatch.setattr(runtime_routes, "restart_readiness", readiness)
    monkeypatch.setattr(runtime_routes, "request_server_exit", exited)
    monkeypatch.setattr(runtime_routes.asyncio, "sleep", sleep)
    created = []

    async def invoke():
        with monkeypatch.context() as patch:
            patch.setattr(runtime_routes.asyncio, "create_task", created.append)
            result = runtime_routes.schedule_restart(reason, actor=actor, wait_s=wait_s)
        if created:  # supervised → a wait-then-arm coroutine was scheduled; drive it now
            await created.pop()
        return result

    return asyncio.run(invoke()), exited


def test_unsupervised_returns_not_scheduled_and_does_not_arm(monkeypatch):
    """r3/r4: without a supervisor nothing would relaunch the process, so schedule_restart
    reports scheduled=False with the how-to text and arms nothing — but still records the
    audit row with the agent actor and the reason."""
    from observability.audit import audit_logger

    monkeypatch.setattr(STATE, "restart_requested", False)
    audit = Mock()
    monkeypatch.setattr(audit_logger, "log", audit)

    async def sleep(_seconds):  # never reached — no background task is scheduled
        raise AssertionError("an unsupervised restart must not wait or arm")

    result, exited = _schedule_and_drive(
        monkeypatch, readiness=lambda: _readiness(idle=True, supervised=False), sleep=sleep
    )

    assert result == {
        "scheduled": False,
        "reason": "Stop the server and start it again the way you launched it.",
    }
    assert STATE.restart_requested is False
    exited.assert_not_called()
    assert audit.call_args.kwargs["args"] == {
        "actor": "agent",
        "reason": "plugin updated",
        "target": "self",
    }


def test_busy_then_idle_arms_and_requests_exit(monkeypatch):
    """r1/r4: the re-exec is armed only after the turn finishes. Busy on the first poll, idle
    after one second — schedule_restart arms STATE.restart_requested and asks the server to
    exit, within wait_s, and the audit row names the agent actor and reason."""
    from observability.audit import audit_logger

    monkeypatch.setattr(STATE, "restart_requested", False)
    monkeypatch.setattr(STATE, "uvicorn_server", object())  # live, not winding down
    audit = Mock()
    monkeypatch.setattr(audit_logger, "log", audit)

    state = {"idle": False}
    sleep_calls = []

    async def sleep(seconds):
        sleep_calls.append(seconds)
        state["idle"] = True  # the in-flight turn finishes during the first poll

    result, exited = _schedule_and_drive(
        monkeypatch, readiness=lambda: _readiness(idle=state["idle"]), sleep=sleep
    )

    assert result["scheduled"] is True and "600" in result["reason"]
    assert sleep_calls == [1]  # one poll while busy, then idle
    assert STATE.restart_requested is True
    exited.assert_called_once()
    assert audit.call_args.kwargs["args"] == {
        "actor": "agent",
        "reason": "plugin updated",
        "target": "self",
    }


def test_stays_busy_times_out_without_arming(monkeypatch):
    """r2: a turn that never finishes must not leave a restart queued. After exactly wait_s
    polls the background task abandons without arming the re-exec or asking the server to
    exit."""
    from observability.audit import audit_logger

    monkeypatch.setattr(STATE, "restart_requested", False)
    monkeypatch.setattr(STATE, "uvicorn_server", object())
    monkeypatch.setattr(audit_logger, "log", Mock())

    sleep_calls = []

    async def sleep(seconds):
        sleep_calls.append(seconds)  # agent stays busy — idle never returns true

    result, exited = _schedule_and_drive(
        monkeypatch, readiness=lambda: _readiness(idle=False), sleep=sleep, wait_s=3
    )

    assert result["scheduled"] is True
    assert sleep_calls == [1, 1, 1]  # exactly wait_s polls, never idle
    assert STATE.restart_requested is False
    exited.assert_not_called()


def test_shutdown_underway_abandons_without_arming(monkeypatch):
    """r2: a Ctrl-C / hub supervisor stop during the wait must not re-exec. The task bails on
    the first poll once the server is already winding down, leaving STATE.restart_requested
    unset so _main exits cleanly."""
    from observability.audit import audit_logger

    monkeypatch.setattr(STATE, "restart_requested", False)
    monkeypatch.setattr(STATE, "uvicorn_server", SimpleNamespace(should_exit=True))  # winding down
    monkeypatch.setattr(audit_logger, "log", Mock())

    sleep_calls = []

    async def sleep(seconds):
        sleep_calls.append(seconds)

    result, exited = _schedule_and_drive(
        monkeypatch, readiness=lambda: _readiness(idle=False), sleep=sleep
    )

    assert result["scheduled"] is True  # supervised → the task was scheduled...
    assert sleep_calls == []  # ...but it bailed before the first sleep
    assert STATE.restart_requested is False
    exited.assert_not_called()


def test_scheduler_claim_before_send_survives_restart_and_stale_recovery(tmp_path):
    """A crash between the durable claim and POST cannot safely be distinguished
    from a crash after the receiver accepted it, even after the recovery window."""
    from datetime import UTC, datetime, timedelta

    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("claim-test", invoke_url="http://127.0.0.1:7870", db_dir=tmp_path)
    past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    job = scheduler.add_job("crash before send", past)
    message_id = scheduler._claim_occurrence(job)
    assert message_id

    restarted = LocalScheduler("claim-test", invoke_url="http://127.0.0.1:7870", db_dir=tmp_path)
    restarted._recover_missed_fires()
    assert restarted._claim_due_jobs(datetime.now(UTC)) == []
    stored = restarted.get_job(job.id)
    assert stored is not None
    assert stored.last_error == "delivery unknown; not automatically retried"
    assert stored.consecutive_failures == stored.fire_count == 0
    with restarted._connect() as db:
        claim = db.execute("SELECT * FROM occurrences").fetchone()
        assert claim["state"] == "claimed"
        assert claim["message_id"] == message_id
        assert claim["next_fire"] == job.next_fire
