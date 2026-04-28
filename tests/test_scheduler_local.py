"""Tests for ``scheduler.local.LocalScheduler``.

The polling-loop firing path is covered by stubbing ``httpx.AsyncClient``
so a unit test doesn't need a running A2A endpoint. Multi-agent
isolation, missed-fire recovery, and reschedule-vs-delete behaviour
all get explicit cases — they're the parts most likely to regress.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scheduler.interface import is_cron, parse_iso_to_utc
from scheduler.local import LocalScheduler, _compute_next_fire


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_scheduler(tmp_path: Path, agent: str = "gina-test") -> LocalScheduler:
    return LocalScheduler(
        agent_name=agent,
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
    )


# ── interface helpers ──────────────────────────────────────────────────────


class TestIsCron:
    def test_cron_5_field(self):
        assert is_cron("0 9 * * *") is True

    def test_cron_with_ranges(self):
        assert is_cron("0 9 * * 1-5") is True

    def test_iso_with_t(self):
        assert is_cron("2026-04-28T15:00:00") is False

    def test_iso_with_space(self):
        assert is_cron("2026-04-28 15:00:00") is False

    def test_iso_with_offset(self):
        assert is_cron("2026-04-28T15:00:00+00:00") is False

    def test_garbage(self):
        assert is_cron("not a schedule") is False
        assert is_cron("0 9 *") is False  # 3 fields, not 5

    def test_seven_fields_rejected(self):
        # 7-field cron (with seconds + year) is not standard 5-field;
        # the current detector accepts only exactly 5.
        assert is_cron("0 0 12 * * MON 2026") is False


class TestParseIso:
    def test_naive_treated_as_utc(self):
        dt = parse_iso_to_utc("2026-04-28T15:00:00")
        assert dt.tzinfo == UTC
        assert dt.hour == 15

    def test_offset_normalized(self):
        dt = parse_iso_to_utc("2026-04-28T15:00:00-05:00")
        assert dt.tzinfo == UTC
        assert dt.hour == 20  # 15 EST → 20 UTC

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match=r"Invalid isoformat|could not convert"):
            parse_iso_to_utc("not an iso string")


# ── add / list / cancel ─────────────────────────────────────────────────────


class TestAddJob:
    def test_cron_job(self, tmp_path):
        s = _make_scheduler(tmp_path)
        job = s.add_job("hi", "0 9 * * *")
        assert job.agent_name == "gina-test"
        assert job.prompt == "hi"
        assert job.next_fire is not None
        assert "T" in job.next_fire  # ISO

    def test_iso_one_shot(self, tmp_path):
        s = _make_scheduler(tmp_path)
        future = "2099-01-01T00:00:00"
        job = s.add_job("hi", future)
        # Naive ISO should be normalized to UTC
        assert job.next_fire.startswith("2099-01-01T00:00:00")

    def test_empty_prompt_rejected(self, tmp_path):
        s = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match=r"prompt is required"):
            s.add_job("   ", "0 9 * * *")

    def test_malformed_schedule_rejected(self, tmp_path):
        s = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match=r"Invalid isoformat|could not convert"):
            s.add_job("hi", "not-a-real-schedule")

    def test_user_id_preserved(self, tmp_path):
        s = _make_scheduler(tmp_path)
        job = s.add_job("hi", "0 9 * * *", job_id="my-custom-id")
        assert job.id == "my-custom-id"

    def test_duplicate_id_rejected(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("hi", "0 9 * * *", job_id="dup")
        with pytest.raises(ValueError, match="already exists"):
            s.add_job("again", "0 9 * * *", job_id="dup")

    def test_auto_id_has_agent_prefix(self, tmp_path):
        s = _make_scheduler(tmp_path, agent="ginavision")
        job = s.add_job("hi", "0 9 * * *")
        assert job.id.startswith("ginavision-")


class TestListAndCancel:
    def test_list_filters_by_agent(self, tmp_path):
        gp = _make_scheduler(tmp_path, agent="gina-personal")
        gw = _make_scheduler(tmp_path, agent="gina-work")
        gp.add_job("p1", "0 9 * * *")
        gp.add_job("p2", "0 10 * * *")
        gw.add_job("w1", "0 9 * * *")
        assert len(gp.list_jobs()) == 2
        assert len(gw.list_jobs()) == 1
        assert gp.list_jobs()[0].agent_name == "gina-personal"

    def test_cancel_returns_true_on_hit(self, tmp_path):
        s = _make_scheduler(tmp_path)
        job = s.add_job("hi", "0 9 * * *")
        assert s.cancel_job(job.id) is True
        assert s.list_jobs() == []

    def test_cancel_returns_false_on_miss(self, tmp_path):
        s = _make_scheduler(tmp_path)
        assert s.cancel_job("does-not-exist") is False

    def test_cross_agent_cancel_blocked(self, tmp_path):
        gp = _make_scheduler(tmp_path, agent="gina-personal")
        gw = _make_scheduler(tmp_path, agent="gina-work")
        gw_job = gw.add_job("w1", "0 9 * * *")
        # gp tries to cancel gw's job — must fail silently (no row deleted)
        assert gp.cancel_job(gw_job.id) is False
        assert len(gw.list_jobs()) == 1


# ── reschedule / delete behaviour ───────────────────────────────────────────


class TestRescheduleOrDelete:
    def test_one_shot_deleted_after_fire(self, tmp_path):
        s = _make_scheduler(tmp_path)
        # ISO in the past so _claim_due_jobs picks it up
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        s.add_job("hi", past, job_id="oneshot")
        job = s.list_jobs()[0]
        s._reschedule_or_delete(job, fired_at=datetime.now(UTC))
        assert s.list_jobs() == []

    def test_cron_rescheduled_after_fire(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("hi", "0 9 * * *", job_id="cron")
        job = s.list_jobs()[0]
        # Fire at a fixed timestamp — 2026-04-28T10:00:00Z is one hour
        # past the 09:00 cron slot, so the next fire must be exactly
        # 2026-04-29T09:00:00Z.
        fired_at = datetime(2026, 4, 28, 10, 0, 0, tzinfo=UTC)
        s._reschedule_or_delete(job, fired_at=fired_at)
        rescheduled = s.list_jobs()[0]
        assert rescheduled.next_fire == "2026-04-29T09:00:00+00:00"
        assert rescheduled.last_fire == fired_at.isoformat()


class TestMissedFireRecovery:
    def test_stale_oneshot_dropped(self, tmp_path):
        s = _make_scheduler(tmp_path)
        # ISO from 2 days ago — outside the 24h window
        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        s.add_job("hi", stale, job_id="stale")
        s._recover_missed_fires()
        assert s.list_jobs() == []

    def test_stale_cron_rolled_forward(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("hi", "0 9 * * *", job_id="cron-stale")
        # Manually rewrite next_fire to 2 days ago (outside window)
        db = sqlite3.connect(str(s.path))
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (old, "cron-stale"))
        db.commit()
        db.close()
        s._recover_missed_fires()
        rolled = s.list_jobs()[0]
        assert rolled.next_fire > datetime.now(UTC).isoformat()

    def test_recent_missed_fire_kept(self, tmp_path):
        s = _make_scheduler(tmp_path)
        # 5 minutes ago — inside the 24h window, should still fire
        recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        s.add_job("hi", recent, job_id="recent")
        s._recover_missed_fires()
        # Job still exists with next_fire in the past — polling will fire it
        jobs = s.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].next_fire < datetime.now(UTC).isoformat()


# ── compute_next_fire ───────────────────────────────────────────────────────


class TestComputeNextFire:
    def test_cron_returns_iso_utc(self):
        result = _compute_next_fire("0 9 * * *")
        # Parses cleanly as ISO
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_cron_after_anchor(self):
        anchor = datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC)
        result = _compute_next_fire("0 9 * * *", after=anchor)
        # 9am UTC on 2026-04-27
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026 and dt.month == 4 and dt.day == 27 and dt.hour == 9

    def test_iso_passthrough(self):
        result = _compute_next_fire("2026-12-25T00:00:00")
        assert result.startswith("2026-12-25T00:00:00")


# ── start / stop loop ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_idempotent(tmp_path):
    s = _make_scheduler(tmp_path)
    await s.start()
    await s.start()  # second call is a no-op, not an error
    assert s._task is not None
    await s.stop()
    await s.stop()  # second call is a no-op, not an error
    assert s._task is None


@pytest.mark.asyncio
async def test_due_job_fires(tmp_path, monkeypatch):
    """End-to-end: an ISO job in the past gets picked up and POSTs to /a2a."""
    s = _make_scheduler(tmp_path)
    # Schedule for 1 second ago so the first tick claims it
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    s.add_job("FIRED-ME", past, job_id="firetest")

    fired: list[dict] = []

    class _FakeResponse:
        status_code = 200
        text = "ok"

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            fired.append({"url": url, "json": json})
            return _FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await s.start()
    # Give the polling loop one tick (poll interval is 1s)
    await asyncio.sleep(1.5)
    await s.stop()

    assert any("FIRED-ME" in str(c["json"]) for c in fired)
    # One-shot was deleted after firing
    assert s.list_jobs() == []


@pytest.mark.asyncio
async def test_fire_failure_leaves_job_in_place(tmp_path, monkeypatch):
    """A 5xx HTTP response from /a2a must NOT delete the job.

    Regression guard for the round-2 review finding: previously,
    _tick() called _reschedule_or_delete in finally, which silently
    consumed one-shot jobs on transient failures. Now the job stays
    until delivery actually succeeds.
    """
    s = _make_scheduler(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    s.add_job("DURABLE", past, job_id="firetest")

    class _FakeResponse:
        status_code = 503
        text = "service unavailable"

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            return _FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await s.start()
    await asyncio.sleep(1.5)  # one polling tick
    await s.stop()

    # Job survives the failed fire, will be retried on the next tick.
    assert len(s.list_jobs()) == 1
    assert s.list_jobs()[0].id == "firetest"


@pytest.mark.asyncio
async def test_fire_returns_bool(tmp_path, monkeypatch):
    """``_fire`` is the success/failure signal feeding the
    reschedule decision in ``_tick``. Lock the contract."""
    s = _make_scheduler(tmp_path)
    job = s.add_job("hi", "0 9 * * *", job_id="x")

    class _OkResponse:
        status_code = 200
        text = "ok"

    class _ErrResponse:
        status_code = 500
        text = "boom"

    class _FakeClient:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            return self._response

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_OkResponse()))
    assert await s._fire(job) is True

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_ErrResponse()))
    assert await s._fire(job) is False
