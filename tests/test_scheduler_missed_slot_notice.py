"""Owner notices reuse durable scheduler recovery and outcome transitions."""
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from graph.plugins import chat_surface
from scheduler.local import FAILURE_NOTICE_AFTER, LocalScheduler
from server.agent_init import _notify_scheduler_notice

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def notices(tmp_path, monkeypatch):
    received = []
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": received.append})
    monkeypatch.setattr("security.public_console.public_origin", lambda: "https://console.example")
    scheduler = LocalScheduler(
        "notice-test", invoke_url="http://test", db_dir=tmp_path,
        notify=_notify_scheduler_notice,
    )
    return scheduler, received


def test_skipped_slot_notified_once_across_restart(notices):
    scheduler, received = notices
    job = scheduler.add_job("daily check", "0 9 * * *", job_id="daily")
    for age in (3, 2):
        stale = (datetime.now(UTC) - timedelta(days=age)).isoformat()
        with sqlite3.connect(scheduler.path) as db:
            db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (stale, job.id))
        scheduler._recover_missed_fires()
        assert stale in received[-1].text
        assert scheduler.get_job(job.id).next_fire > datetime.now(UTC).isoformat()
        scheduler = LocalScheduler(
            "notice-test", invoke_url="http://test", db_dir=scheduler.path.parent.parent,
            notify=_notify_scheduler_notice,
        )
        scheduler._recover_missed_fires()
    assert len(received) == 2
    notice = received[0]
    assert (notice.kind, notice.importance) == ("notify", "high")
    assert "request a catch-up" in notice.text
    assert notice.link.startswith("https://console.example/app/?")
    assert parse_qs(urlsplit(notice.link).query) == {"session": [job.context_id]}


@pytest.mark.asyncio
@pytest.mark.parametrize("task_handle", [True, False])
async def test_failure_threshold_success_reset_and_healthy_silence(notices, monkeypatch, task_handle):
    scheduler, received = notices
    scheduler.add_job("daily check", "0 9 * * *", job_id="daily")
    answer = {}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=answer))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport))

    async def fire(failed):
        job = scheduler.get_job("daily")
        task = {"status": {"state": "TASK_STATE_FAILED" if failed else "TASK_STATE_COMPLETED",
                           "message": {"parts": [{"text": "gateway unavailable"}]}}}
        if task_handle:
            task["id"] = f"task-{job.next_fire}"
        answer.clear()
        answer.update({"result": {"task": task}})
        scheduler._claimed_messages[job.id] = scheduler._claim_occurrence(
            job, advance_at=datetime.fromisoformat(job.next_fire),
        )
        await scheduler._fire(job)
        if task_handle:
            # Reconciliation seeing the same terminal snapshot cannot notify twice.
            scheduler._settle_task(job, task["id"], answer)

    await fire(False)
    assert received == []
    for _ in range(2):
        for i in range(FAILURE_NOTICE_AFTER):
            count = len(received)
            await fire(True)
            assert len(received) == count + (i == FAILURE_NOTICE_AFTER - 1)
        count = len(received)
        # Re-opening the store must not re-arm a climbing failure streak.
        scheduler = LocalScheduler(
            "notice-test", invoke_url="http://test", db_dir=scheduler.path.parent.parent,
            notify=_notify_scheduler_notice,
        )
        await fire(True)
        assert len(received) == count
        await fire(False)
        assert len(received) == count
        assert scheduler.get_job("daily").consecutive_failures == 0
    assert len(received) == 2
    assert all(n.kind == "notify" and n.importance == "high" for n in received)
    assert all("pause the job in Work" in n.text for n in received)


def test_recovery_exemptions_and_delivery_failure(notices):
    scheduler, received = notices
    recent = scheduler.add_job("catch up", "0 9 * * *", job_id="recent")
    held = scheduler.add_job("in flight", "0 9 * * *", job_id="held")
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with sqlite3.connect(scheduler.path) as db:
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (old, held.id))
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (
            (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), recent.id,
        ))
    scheduler._claim_occurrence(scheduler.get_job(held.id))
    scheduler.add_job("old one shot", old, job_id="one-shot")
    scheduler._recover_missed_fires()
    assert received == []
    assert scheduler.get_job("one-shot") is None

    def broken(*args, **kwargs):
        raise RuntimeError("transport stopped")

    scheduler._notify = broken
    with sqlite3.connect(scheduler.path) as db:
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (old, recent.id))
    scheduler._recover_missed_fires()
    assert scheduler.get_job(recent.id).next_fire > datetime.now(UTC).isoformat()


def test_rolled_back_settlement_does_not_send_notice(notices):
    scheduler, received = notices
    scheduler.add_job("daily check", "0 9 * * *", job_id="daily")
    with sqlite3.connect(scheduler.path) as db:
        db.execute("UPDATE jobs SET consecutive_failures = ?", (FAILURE_NOTICE_AFTER - 1,))
    job = scheduler.get_job("daily")
    scheduler._claim_occurrence(job, advance_at=datetime.fromisoformat(job.next_fire))
    scheduler._remember_task(job, "task")
    with sqlite3.connect(scheduler.path) as db:
        db.execute("""CREATE TRIGGER fail_settlement BEFORE UPDATE OF task_state ON occurrences
                      BEGIN SELECT RAISE(ABORT, 'settlement failed'); END""")
    payload = {"result": {"id": "task", "status": {"state": "TASK_STATE_FAILED"}}}
    with pytest.raises(sqlite3.DatabaseError, match="settlement failed"):
        scheduler._settle_task(job, "task", payload)
    assert received == []
    assert scheduler.get_job("daily").consecutive_failures == FAILURE_NOTICE_AFTER - 1
    with sqlite3.connect(scheduler.path) as db:
        db.execute("DROP TRIGGER fail_settlement")
    scheduler._settle_task(job, "task", payload)
    assert len(received) == 1
