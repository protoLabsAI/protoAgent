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
async def test_start_retries_owner_lock_then_polls(tmp_path):
    """A jobs.db lock held at boot (a restart/redeploy overlap) must NOT
    permanently skip the scheduler. start() schedules a background retry and
    begins polling once the lock frees — instead of staying off until a reload."""
    import scheduler.local as sl

    s = _make_scheduler(tmp_path)
    s._LOCK_RETRY_SECONDS = 0.05  # don't wait the real 15s
    key = str(s.path)
    sl._LOCKED_PATHS.add(key)  # simulate another live instance owning the jobs.db
    try:
        await s.start()
        assert s._task is not None  # scheduled a retry — did NOT give up
        assert s._lock_fd is None   # not acquired yet (still held)

        sl._LOCKED_PATHS.discard(key)  # the other instance exits → lock frees
        for _ in range(60):            # let the background retry acquire it
            if s._lock_fd is not None:
                break
            await asyncio.sleep(0.05)
        assert s._lock_fd is not None  # acquired after waiting → now polling
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_fire_defers_quietly_when_agent_not_reachable(tmp_path, monkeypatch, caplog):
    """bd-3vp: a connection error to our own /a2a (Uvicorn not accepting yet during
    startup catch-up) is an expected, self-healing condition — _fire returns False
    and logs concisely, not a scary 'fire exception' traceback."""
    import httpx

    s = _make_scheduler(tmp_path)
    job = s.add_job("X", (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job_id="jx")

    class _Refused:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Refused)

    with caplog.at_level("INFO"):
        ok = await s._fire(job)
    assert ok is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "deferring fire" in msgs
    assert "fire exception" not in msgs  # no error-level traceback for a not-ready server


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

    # Fires route into the durable Activity thread (ADR 0003) so the response
    # surfaces, with an origin tag for the activity surface. A2A 1.0: contextId +
    # metadata live on the message (#477).
    call = next(c for c in fired if "FIRED-ME" in str(c["json"]))
    msg = call["json"]["params"]["message"]
    assert msg["contextId"] == "system:activity"
    assert msg["metadata"]["origin"] == "scheduler"


async def test_fire_publishes_scheduler_fired_event(tmp_path, monkeypatch):
    """A dispatched job publishes `scheduler.fired` on the bus (ADR 0051)."""
    events: list = []
    s = LocalScheduler(
        agent_name="gina-test", invoke_url="http://127.0.0.1:7870",
        api_key="k", bearer_token="b", db_dir=tmp_path,
        event_publish=lambda topic, data: events.append((topic, data)),
    )
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    s.add_job("nightly audit", past, job_id="firetest")

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

        async def post(self, *_a, **_kw):
            return _FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    await s.start()
    await asyncio.sleep(1.5)
    await s.stop()

    fired = [d for (t, d) in events if t == "scheduler.fired"]
    assert fired and fired[0]["job_id"] == "firetest"
    assert fired[0]["prompt"] == "nightly audit"


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


# ── backend selection: Workstacean is opt-in (local is the default) ──────────


def test_workstacean_is_opt_in(monkeypatch):
    """Workstacean env vars alone must NOT switch the backend; only an explicit
    SCHEDULER_BACKEND=workstacean does. Otherwise the default stays local."""
    import server
    from graph.config import LangGraphConfig
    from scheduler import LocalScheduler, WorkstaceanScheduler

    cfg = LangGraphConfig()  # scheduler_enabled defaults True
    monkeypatch.setenv("WORKSTACEAN_API_BASE", "https://example.com")
    monkeypatch.setenv("WORKSTACEAN_API_KEY", "k")
    monkeypatch.delenv("SCHEDULER_BACKEND", raising=False)
    monkeypatch.delenv("SCHEDULER_DISABLED", raising=False)

    backend = server._build_scheduler(cfg)
    assert isinstance(backend, LocalScheduler)  # env present but not opted in → local

    monkeypatch.setenv("SCHEDULER_BACKEND", "workstacean")
    backend = server._build_scheduler(cfg)
    assert isinstance(backend, WorkstaceanScheduler)  # explicit opt-in honored


def test_workstacean_opt_in_without_creds_falls_back_local(monkeypatch):
    import server
    from graph.config import LangGraphConfig
    from scheduler import LocalScheduler

    cfg = LangGraphConfig()
    monkeypatch.setenv("SCHEDULER_BACKEND", "workstacean")
    monkeypatch.delenv("WORKSTACEAN_API_BASE", raising=False)
    monkeypatch.delenv("WORKSTACEAN_API_KEY", raising=False)
    monkeypatch.delenv("SCHEDULER_DISABLED", raising=False)

    assert isinstance(server._build_scheduler(cfg), LocalScheduler)


# ── A2A 1.0 loopback wire shape (#477) ───────────────────────────────────────


class _CaptureClient:
    """Stub for httpx.AsyncClient that records the single POST _fire makes."""

    def __init__(self):
        self.url = self.headers = self.json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.url, self.headers, self.json = url, headers, json
        return type("R", (), {"status_code": 200, "text": "ok"})()


@pytest.mark.asyncio
async def test_fire_emits_a2a_1_0_wire_shape(tmp_path, monkeypatch):
    """_fire must POST the A2A 1.0 shape (the sidecar's a2a-sdk 1.1 handler
    rejects 0.3): A2A-Version header, SendMessage, ROLE_USER, parts:[{text}],
    contextId + metadata ON the message. Regresses #477."""
    import httpx

    from events import ACTIVITY_CONTEXT

    cap = _CaptureClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: cap)

    s = _make_scheduler(tmp_path)
    job = s.add_job("do the thing", "0 9 * * *")
    assert await s._fire(job) is True

    assert cap.headers["A2A-Version"] == "1.0"
    assert cap.url.endswith("/a2a")

    body = cap.json
    assert body["method"] == "SendMessage"          # not 0.3 "message/send"
    assert "contextId" not in body["params"]          # moved onto the message
    msg = body["params"]["message"]
    assert msg["role"] == "ROLE_USER"                 # not "user"
    assert msg["parts"] == [{"text": "do the thing"}] # not [{"kind":"text",...}]
    assert msg["contextId"] == ACTIVITY_CONTEXT
    assert msg["metadata"]["scheduler_job_id"] == job.id
    assert msg["metadata"]["origin"] == "scheduler"


@pytest.mark.asyncio
async def test_schedule_task_dedupes_identical_jobs(tmp_path):
    """schedule_task must not create a second job identical to an active one
    (same prompt + schedule) — the common cause of scheduled-task spam."""
    from tools.lg_tools import _build_scheduler_tools

    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in _build_scheduler_tools(sched)}
    schedule = tools["schedule_task"]

    r1 = await schedule.ainvoke({"prompt": "summarize logs", "when": "0 * * * *"})
    assert "Scheduled job" in r1
    r2 = await schedule.ainvoke({"prompt": "summarize logs", "when": "0 * * * *"})
    assert "Already scheduled" in r2 and "duplicate" in r2
    assert len(sched.list_jobs()) == 1

    # A different schedule for the same prompt is NOT a duplicate.
    r3 = await schedule.ainvoke({"prompt": "summarize logs", "when": "0 9 * * *"})
    assert "Scheduled job" in r3
    assert len(sched.list_jobs()) == 2


@pytest.mark.asyncio
async def test_slow_fire_not_refired_while_in_flight(tmp_path, monkeypatch):
    """A scheduled turn that runs longer than the poll interval must fire ONCE.

    message/send blocks until the turn is terminal, so a multi-tick turn would
    otherwise be re-claimed every second and fire repeatedly (the duplicate
    scheduled-turn / spam bug). The in-flight guard prevents re-claiming.
    """
    s = _make_scheduler(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    s.add_job("SLOW", past, job_id="slow")  # one-shot, already due

    calls: list[int] = []

    class _SlowClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(1)
            await asyncio.sleep(2.2)  # turn spans multiple 1s poll ticks

            class _R:
                status_code = 200
                text = "ok"

            return _R()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _SlowClient)

    await s.start()
    await asyncio.sleep(2.8)  # several ticks elapse during the single slow turn
    await s.stop()

    assert len(calls) == 1          # fired once, not once-per-tick
    assert s.list_jobs() == []      # one-shot deleted after the turn finally landed


def test_per_job_timezone_evaluates_cron_in_that_zone(tmp_path):
    """A cron with a timezone fires at local wall-clock time, stored as UTC."""
    from zoneinfo import ZoneInfo

    s = _make_scheduler(tmp_path)
    job = s.add_job("noon in chicago", "0 12 * * *", job_id="tz", timezone="America/Chicago")
    assert job.timezone == "America/Chicago"
    # next_fire is stored UTC; converted back to Chicago it must be 12:00 local.
    nf_local = datetime.fromisoformat(job.next_fire).astimezone(ZoneInfo("America/Chicago"))
    assert nf_local.hour == 12 and nf_local.minute == 0
    # Round-trips through the DB.
    assert s.list_jobs()[0].timezone == "America/Chicago"


def test_invalid_timezone_raises(tmp_path):
    s = _make_scheduler(tmp_path)
    with pytest.raises(ValueError, match="invalid timezone"):
        s.add_job("x", "0 9 * * *", job_id="bad", timezone="Mars/Phobos")


def test_no_timezone_defaults_to_utc(tmp_path):
    from zoneinfo import ZoneInfo

    s = _make_scheduler(tmp_path)
    job = s.add_job("noon utc", "0 12 * * *", job_id="utc")
    assert job.timezone is None
    nf_utc = datetime.fromisoformat(job.next_fire).astimezone(ZoneInfo("UTC"))
    assert nf_utc.hour == 12


# ── context_id / same-session resume (ADR 0053) ──────────────────────────────


class _CapClient:
    """Captures the JSON body of the single POST the scheduler fires."""

    posted: dict = {}

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, _url, headers=None, json=None):  # noqa: A002
        _CapClient.posted = json or {}

        class _R:
            status_code = 200
            text = "ok"

        return _R()


class TestContextId:
    def test_add_job_round_trips_context_id(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("resume", "2099-01-01T00:00:00+00:00", job_id="j", context_id="chat-abc")
        assert s.list_jobs()[0].context_id == "chat-abc"

    def test_context_id_defaults_to_none(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("plain", "2099-01-01T00:00:00+00:00", job_id="j")
        assert s.list_jobs()[0].context_id is None

    def test_migrates_pre_context_id_db(self, tmp_path):
        # Simulate a store created before the context_id column existed: rebuild
        # the table with the old shape, then a fresh instance runs the lazy
        # ALTER-TABLE migration on init.
        s = _make_scheduler(tmp_path)
        old_schema = (
            "DROP TABLE jobs;"
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, "
            "schedule TEXT NOT NULL, agent_name TEXT NOT NULL, next_fire TEXT NOT NULL, "
            "last_fire TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
            "timezone TEXT);"
        )
        db = sqlite3.connect(str(s.path))
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO jobs (id, prompt, schedule, agent_name, next_fire, enabled, created_at) "
            "VALUES ('old', 'p', '0 9 * * *', 'gina-test', '2099-01-01T00:00:00+00:00', 1, "
            "'2026-01-01T00:00:00+00:00')"
        )
        db.commit()
        db.close()

        s2 = _make_scheduler(tmp_path)
        jobs = s2.list_jobs()
        assert len(jobs) == 1 and jobs[0].context_id is None  # old row → no context
        s2.add_job("new", "2099-01-02T00:00:00+00:00", job_id="new", context_id="chat-x")
        got = {j.id: j for j in s2.list_jobs()}
        assert got["new"].context_id == "chat-x"

    @pytest.mark.asyncio
    async def test_fire_routes_to_job_context_id(self, tmp_path, monkeypatch):
        import httpx

        from events import ACTIVITY_CONTEXT

        s = _make_scheduler(tmp_path)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _CapClient())

        soon = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        scoped = s.add_job("resume me", soon, job_id="scoped", context_id="chat-abc")
        assert await s._fire(scoped) is True
        assert _CapClient.posted["params"]["message"]["contextId"] == "chat-abc"

        plain = s.add_job("plain", soon, job_id="plain")
        await s._fire(plain)
        assert _CapClient.posted["params"]["message"]["contextId"] == ACTIVITY_CONTEXT
