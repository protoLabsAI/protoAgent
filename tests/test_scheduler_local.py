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


def _make_scheduler(tmp_path: Path, agent: str = "gina-test", **kw) -> LocalScheduler:
    return LocalScheduler(
        agent_name=agent,
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
        **kw,
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        # A2A answers 200 for a turn that FAILED — the outcome is in the body (#3376),
        # so the fire path reads this. Defaults to a completed task so every existing
        # test keeps meaning "this fire succeeded".
        self._payload = (
            payload
            if payload is not None
            else {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}
        )

    def json(self):
        return self._payload


class _FakeClient:
    """Stubs ``httpx.AsyncClient`` so a unit test exercises ``_fire`` without a live A2A."""

    def __init__(self, response, raise_exc=None, **_kw):
        self._response = response
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        if self._raise:
            raise self._raise
        return self._response


_FUTURE_ISO = "2099-01-01T00:00:00+00:00"


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

    def test_origin_session_persisted_and_read_back(self, tmp_path):
        # #2990 r1/r8: origin_session (where a fire's RESULT is delivered) survives a
        # DB round-trip via add_job → list_jobs and the get_job single-row read.
        s = _make_scheduler(tmp_path)
        s.add_job("sweep", "0 9 * * *", job_id="j-origin", origin_session="chat-42")
        assert s.list_jobs()[0].origin_session == "chat-42"
        assert s.get_job("j-origin").origin_session == "chat-42"

    def test_origin_session_defaults_none(self, tmp_path):
        # r6: a schedule created without a chat carries no origin_session.
        s = _make_scheduler(tmp_path)
        s.add_job("sweep", "0 9 * * *", job_id="j-noorigin")
        assert s.get_job("j-noorigin").origin_session is None

    def test_get_job_missing_returns_none(self, tmp_path):
        s = _make_scheduler(tmp_path)
        assert s.get_job("nope") is None

    def test_origin_session_column_migrates_onto_a_legacy_db(self, tmp_path):
        # A jobs.db created before #2990 (no origin_session column) must gain it via the
        # lightweight ALTER on init — a pre-existing row reads back origin_session=None
        # rather than crashing the row mapper.
        db_dir = tmp_path / "legacy"
        db_dir.mkdir()
        path = db_dir / "gina-test" / "jobs.db"
        path.parent.mkdir(parents=True)
        legacy = sqlite3.connect(str(path))
        legacy.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, schedule TEXT NOT NULL, "
            "agent_name TEXT NOT NULL, next_fire TEXT NOT NULL, last_fire TEXT, "
            "enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO jobs (id, prompt, schedule, agent_name, next_fire, created_at) "
            "VALUES ('old', 'p', '0 9 * * *', 'gina-test', ?, ?)",
            (_FUTURE_ISO, datetime.now(UTC).isoformat()),
        )
        legacy.commit()
        legacy.close()
        # Init runs the migration; the pre-existing row now has the column, defaulted None.
        s = _make_scheduler(db_dir)
        assert s.get_job("old").origin_session is None
        # …and a new job can store one.
        s.add_job("new", "0 9 * * *", job_id="new", origin_session="chat-9")
        assert s.get_job("new").origin_session == "chat-9"


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


class TestUpdateJob:
    def test_in_place_update_keeps_id_and_recomputes_next_fire(self, tmp_path):
        s = _make_scheduler(tmp_path)
        job = s.add_job("old prompt", "0 9 * * *", job_id="j1")
        before = job.next_fire
        out = s.update_job("j1", "new prompt", "0 17 * * 1-5", timezone="America/Chicago")
        assert out.id == "j1"  # same job, not a new one
        assert out.prompt == "new prompt"
        assert out.schedule == "0 17 * * 1-5"
        assert out.timezone == "America/Chicago"
        assert out.next_fire != before  # recomputed from the new schedule
        # Exactly one job remains (in-place, not cancel+re-add into a new row).
        jobs = s.list_jobs()
        assert len(jobs) == 1 and jobs[0].id == "j1" and jobs[0].prompt == "new prompt"

    def test_update_missing_job_raises(self, tmp_path):
        s = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match="no job"):
            s.update_job("nope", "p", "0 9 * * *")

    def test_update_rejects_empty_prompt_and_bad_schedule_without_mutating(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("keep", "0 9 * * *", job_id="j1")
        with pytest.raises(ValueError, match="prompt is required"):
            s.update_job("j1", "  ", "0 10 * * *")
        with pytest.raises(ValueError, match="Invalid isoformat|could not convert"):
            s.update_job("j1", "p", "not-a-schedule")
        # The original is untouched after either rejection.
        assert s.list_jobs()[0].prompt == "keep" and s.list_jobs()[0].schedule == "0 9 * * *"

    def test_cross_agent_update_blocked(self, tmp_path):
        gp = _make_scheduler(tmp_path, agent="gina-personal")
        gw = _make_scheduler(tmp_path, agent="gina-work")
        gw_job = gw.add_job("w1", "0 9 * * *")
        with pytest.raises(ValueError, match="no job"):
            gp.update_job(gw_job.id, "hijack", "0 0 * * *")
        assert gw.list_jobs()[0].prompt == "w1"  # gw's job unchanged


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


class TestFireAndSettle:
    """``_fire_and_settle`` is the one-shot cleanup wrapper around ``_fire`` — it
    deletes the row on a successful fire. But ``_fire`` blocks for the *whole*
    agent turn (the self-POST is synchronous), and a wait-chain resume (#2751)
    can call ``wait`` again from inside that turn, rescheduling this exact job id
    to a new ``next_fire`` before the outer POST returns. The post-fire delete
    must not clobber that reschedule."""

    @pytest.mark.asyncio
    async def test_one_shot_deleted_after_clean_fire(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        s = _make_scheduler(tmp_path)
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        job = s.add_job("hi", past, job_id="wait:chat-1")

        await s._fire_and_settle(job, cron=False)

        assert s.list_jobs() == []

    @pytest.mark.asyncio
    async def test_wait_rescheduled_mid_fire_survives_the_post_fire_delete(self, tmp_path, monkeypatch):
        import httpx

        s = _make_scheduler(tmp_path)
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        job = s.add_job("retry after rate limit", past, job_id="wait:chat-1", context_id="chat-1")

        class _SupersedingClient(_FakeClient):
            """Stands in for the self-POST — while the (synchronous, from the
            scheduler's view) agent turn is "running", the turn calls `wait`
            again for the same session, which cancels + re-adds this exact
            job id (the real `wait` tool's supersede path)."""

            async def post(self, url, headers=None, json=None):
                s.cancel_job("wait:chat-1")
                s.add_job("rate limit cleared, retry", _FUTURE_ISO, job_id="wait:chat-1", context_id="chat-1")
                return await super().post(url, headers=headers, json=json)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _SupersedingClient(_FakeResponse(200)))

        await s._fire_and_settle(job, cron=False)

        jobs = s.list_jobs()
        assert [j.id for j in jobs] == ["wait:chat-1"]  # the reschedule, not deleted
        assert jobs[0].prompt == "rate limit cleared, retry"
        assert jobs[0].next_fire == _FUTURE_ISO


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


# ── #1767: turn-lifecycle events around the self-POST ───────────────────────


class TestFireTurnEvents:
    """A scheduled/watch fire holds the connection open for the WHOLE turn, so the
    console is otherwise blind to it. ``_fire`` brackets the self-POST with
    ``turn.started`` / ``turn.finished`` (#1767) so the console can render its typing
    indicator, labelled by trigger, during the agent's longest turns."""

    @pytest.mark.asyncio
    async def test_scheduler_finish_carries_the_task_id_it_fired(self, tmp_path, monkeypatch):
        """``turn.finished`` must name the turn that ended (#3446): a console holding a
        DIFFERENT live turn's control keeps it, instead of clearing whichever one it had —
        which is what dropped an operator's queued interjection."""
        import httpx

        payload = {"result": {"id": "task-99", "status": {"state": "TASK_STATE_COMPLETED"}}}
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=payload))
        )
        events: list = []
        s = _make_scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        job = s.add_job("sweep the inbox", _FUTURE_ISO, job_id="job-1", context_id="chat-42")

        assert await s._fire(job) is True

        assert next(d for (t, d) in events if t == "turn.finished")["task_id"] == "task-99"
        # The fire publishes `turn.started` before the task exists, so it carries no id.
        assert "task_id" not in next(d for (t, d) in events if t == "turn.started")

    @pytest.mark.asyncio
    async def test_scheduler_fire_emits_started_then_finished(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        s = _make_scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        job = s.add_job("sweep the inbox", _FUTURE_ISO, job_id="job-1", context_id="chat-42")

        ok = await s._fire(job)

        assert ok is True
        turn = [(t, d) for (t, d) in events if t.startswith("turn.")]
        assert [t for (t, _) in turn] == ["turn.started", "turn.finished"]
        for _t, d in turn:
            assert d["session_id"] == "chat-42"  # the fire's context (a wait/run_in_session resume)
            assert d["origin"] == "scheduler"
            assert d["trigger"] == "job-1"
        assert turn[-1][1]["ok"] is True

    @pytest.mark.asyncio
    async def test_watch_reaction_fire_tags_watch_origin(self, tmp_path, monkeypatch):
        """A watch reaction (ADR 0067) enqueues a one-shot with a ``watch-<id>`` job id
        via sdk.run_in_session — the origin metadata lets the console label the indicator
        as a watch trigger rather than a plain schedule.

        The bus origin is the plain ``watch`` token, matching the ``origin`` this same fire
        puts on the A2A message metadata. They used to disagree — the bus got the raw
        ``watch-<id>`` job id — and only the console's pattern-match over both spellings
        hid it. The per-watch id is still carried, on ``trigger``."""
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        s = _make_scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        job = s.add_job("check on it", _FUTURE_ISO, job_id="watch-abc123", context_id="chat-9")

        await s._fire(job)

        turn = [(t, d) for (t, d) in events if t.startswith("turn.")]
        assert [t for (t, _) in turn] == ["turn.started", "turn.finished"]
        for _t, d in turn:  # both ends of the bracket, not just the start
            assert d["origin"] == "watch"
            assert d["session_id"] == "chat-9"
            assert d["trigger"] == "watch-abc123"

    @pytest.mark.asyncio
    async def test_fire_without_context_defaults_to_activity_thread(self, tmp_path, monkeypatch):
        import httpx

        from events import ACTIVITY_CONTEXT

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        s = _make_scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        job = s.add_job("daily digest", "0 9 * * *", job_id="cron-1")  # no context_id

        await s._fire(job)

        started = next(d for (t, d) in events if t == "turn.started")
        assert started["session_id"] == ACTIVITY_CONTEXT

    @pytest.mark.asyncio
    async def test_fire_finishes_even_on_http_error(self, tmp_path, monkeypatch):
        """A non-2xx fire must still emit ``turn.finished`` (ok=False) — a hanging
        ``turn.started`` would spin the console's indicator forever."""
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(500, "boom")))
        events: list = []
        s = _make_scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        job = s.add_job("sweep", _FUTURE_ISO, job_id="job-err", context_id="chat-1")

        ok = await s._fire(job)

        assert ok is False
        topics = [t for (t, _) in events if t.startswith("turn.")]
        assert topics == ["turn.started", "turn.finished"]
        finished = next(d for (t, d) in events if t == "turn.finished")
        assert finished["ok"] is False


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
        assert s._lock_fd is None  # not acquired yet (still held)

        sl._LOCKED_PATHS.discard(key)  # the other instance exits → lock frees
        for _ in range(60):  # let the background retry acquire it
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
        agent_name="gina-test",
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
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


# ── backend selection: the bundled LocalScheduler is the only backend ────────


def test_build_scheduler_returns_local(monkeypatch):
    """The scheduler is always the bundled LocalScheduler (the remote Workstacean
    backend was removed). Stale SCHEDULER_BACKEND/WORKSTACEAN_* env vars are ignored."""
    import server
    from graph.config import LangGraphConfig
    from scheduler import LocalScheduler

    cfg = LangGraphConfig()  # scheduler_enabled defaults True
    monkeypatch.delenv("SCHEDULER_DISABLED", raising=False)
    # Leftover Workstacean env from an old deploy must not change anything.
    monkeypatch.setenv("SCHEDULER_BACKEND", "workstacean")
    monkeypatch.setenv("WORKSTACEAN_API_BASE", "https://example.com")
    monkeypatch.setenv("WORKSTACEAN_API_KEY", "k")

    assert isinstance(server._build_scheduler(cfg), LocalScheduler)


def test_build_scheduler_disabled_returns_none(monkeypatch):
    import server
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    monkeypatch.setenv("SCHEDULER_DISABLED", "1")
    assert server._build_scheduler(cfg) is None


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
    assert body["method"] == "SendMessage"  # not 0.3 "message/send"
    assert "contextId" not in body["params"]  # moved onto the message
    msg = body["params"]["message"]
    assert msg["role"] == "ROLE_USER"  # not "user"
    # A2A 1.0 part shape ({text}, not {kind:text}); the prompt now carries a wake-framing
    # header (ADR 0079) so the agent orients on why it's awake, then the original prompt.
    assert len(msg["parts"]) == 1 and set(msg["parts"][0]) == {"text"}
    assert msg["parts"][0]["text"].endswith("do the thing")
    assert "Autonomous wake — scheduled run" in msg["parts"][0]["text"]
    assert msg["contextId"] == ACTIVITY_CONTEXT
    assert msg["metadata"]["scheduler_job_id"] == job.id
    assert msg["metadata"]["origin"] == "scheduler"


@pytest.mark.asyncio
async def test_self_improvement_fire_preserves_slash_command(tmp_path, monkeypatch):
    """The bounded reviewer command must stay at byte zero through scheduler wake framing."""
    import httpx

    cap = _CaptureClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: cap)
    scheduler = _make_scheduler(tmp_path)
    job = scheduler.add_job(
        "/self-improve\nreview data",
        "0 9 * * *",
        job_id="self-improvement-session-42-1",
        context_id="session-42",
    )
    assert await scheduler._fire(job) is True
    assert cap.json["params"]["message"]["parts"][0]["text"] == "/self-improve\nreview data"


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

    assert len(calls) == 1  # fired once, not once-per-tick
    assert s.list_jobs() == []  # one-shot deleted after the turn finally landed


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


class TestRenameDoesNotOrphanJobs:
    """#2382 — the scheduler was keyed TWICE by the agent's editable display name: the
    jobs.db path segment, and an `agent_name` column every query filters on. Renaming an
    agent therefore pointed it at a brand-new empty database AND, even aimed back at the
    right file, would have shown an empty schedule. Nothing was deleted — a real member here
    ended up with `scheduler/myagent/` sitting beside `scheduler/protoEngineer/`."""

    def _instance_scheduler(self, root: Path, agent: str, monkeypatch) -> LocalScheduler:
        """A scheduler on the DEFAULT per-instance path (no db_dir override)."""
        import scheduler.local as sl

        monkeypatch.setattr(
            "infra.paths.instance_paths",
            lambda: type("P", (), {"store": lambda _s, n: root / n})(),
        )
        assert sl._resolve_db_path  # the path under test
        return LocalScheduler(agent_name=agent, invoke_url="http://127.0.0.1:7870", db_dir=None)

    def test_a_rename_keeps_the_same_store_and_the_jobs_in_it(self, tmp_path, monkeypatch):
        before = self._instance_scheduler(tmp_path, "traderAgent", monkeypatch)
        before.add_job("weekly margin review", "0 9 * * 1")
        assert before.path == tmp_path / "scheduler" / "agent" / "jobs.db"

        # Rename the agent → same store, and the job is still listed (it was written under
        # the old name, so the row re-key is what makes it visible).
        after = self._instance_scheduler(tmp_path, "merchantAgent", monkeypatch)
        assert after.path == before.path
        jobs = after.list_jobs()
        assert [j.prompt for j in jobs] == ["weekly margin review"]
        assert jobs[0].agent_name == "merchantAgent"

    def test_an_existing_name_keyed_store_is_adopted_in_place(self, tmp_path, monkeypatch):
        """Existing installs keep their schedule: the private dir can only hold THIS agent's
        store, so a lone name-keyed one is it — used as-is, no move."""
        legacy = tmp_path / "scheduler" / "protoEngineer"
        legacy.mkdir(parents=True)
        (tmp_path / "scheduler" / "myagent").mkdir()  # empty leftover from an earlier rename
        seeded = LocalScheduler(
            agent_name="protoEngineer", invoke_url="http://127.0.0.1:7870", db_dir=tmp_path / "scheduler"
        )
        seeded.add_job("nightly sweep", "0 2 * * *")
        assert seeded.path == legacy / "jobs.db"

        # A fresh boot on the fixed code adopts it — the empty dir must not make it ambiguous.
        s = self._instance_scheduler(tmp_path, "protoEngineer", monkeypatch)
        assert s.path == legacy / "jobs.db"
        assert [j.prompt for j in s.list_jobs()] == ["nightly sweep"]

    def test_two_real_stores_start_clean_and_say_so(self, tmp_path, monkeypatch, caplog):
        """Guessing between two schedules would silently resurrect (or bury) the wrong one."""
        for name in ("traderAgent", "merchantBot"):
            LocalScheduler(agent_name=name, invoke_url="http://127.0.0.1:7870", db_dir=tmp_path / "scheduler").add_job(
                f"{name} job", "0 9 * * *"
            )

        with caplog.at_level("WARNING"):
            s = self._instance_scheduler(tmp_path, "merchantAgent", monkeypatch)
        assert s.path == tmp_path / "scheduler" / "agent" / "jobs.db"
        assert s.list_jobs() == []
        assert "traderAgent" in caplog.text and "merchantBot" in caplog.text

    def test_a_configured_dir_stays_namespaced_by_name(self, tmp_path):
        """SCHEDULER_DB_DIR / db_dir may be shared by several agents on purpose — a constant
        segment there would have them all open one jobs.db."""
        a = LocalScheduler(agent_name="one", invoke_url="http://x", db_dir=tmp_path)
        b = LocalScheduler(agent_name="two", invoke_url="http://x", db_dir=tmp_path)
        assert a.path == tmp_path / "one" / "jobs.db"
        assert b.path == tmp_path / "two" / "jobs.db"

    def test_rows_written_under_an_old_name_are_adopted(self, tmp_path):
        """The row half of the bug, isolated from the path half: every query filters
        `WHERE agent_name = ?`, so rows stored under a previous display name are invisible
        even when the store path is already correct. Uses a configured dir so the path is
        held constant and only the re-key is under test."""
        db_dir = tmp_path / "shared"
        seeded = LocalScheduler(agent_name="traderAgent", invoke_url="http://x", db_dir=db_dir)
        job = seeded.add_job("weekly margin review", "0 9 * * 1")

        # Same file, opened by the renamed agent (the path segment is the old name on disk).
        renamed = LocalScheduler(agent_name="merchantAgent", invoke_url="http://x", db_dir=db_dir)
        renamed.path = seeded.path
        renamed._init_db()

        assert [j.prompt for j in renamed.list_jobs()] == ["weekly margin review"]
        # …and addressable, not merely listed: cancel filters on agent_name too.
        assert renamed.cancel_job(job.id) is True


# ── Fire outcome + backoff (#3376) ──────────────────────────────────────────
# A fire used to be judged on its HTTP status alone, but A2A answers 200 for a
# turn that FAILED. A real job ran broken for six days logging "fired job …"
# every time, and nothing anywhere disagreed.


def _failed_body(text: str = "No module named 'observability.audit'") -> dict:
    """The shape a genuinely failed turn comes back as — taken from a real
    a2a-tasks row, not invented."""
    return {
        "result": {
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": f"**Error:** {text}"}]},
            }
        }
    }


class TestA2ATurnFailure:
    def test_completed_task_is_success(self):
        from scheduler.local import _a2a_turn_failure

        assert _a2a_turn_failure({"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}) == ""

    def test_failed_task_reports_the_agents_own_error_text(self):
        """"TASK_STATE_FAILED" is not actionable; the error inside it is."""
        from scheduler.local import _a2a_turn_failure

        assert "observability.audit" in _a2a_turn_failure(_failed_body())

    def test_failed_task_without_a_message_falls_back_to_the_state(self):
        from scheduler.local import _a2a_turn_failure

        assert _a2a_turn_failure({"result": {"status": {"state": "TASK_STATE_FAILED"}}}) == "TASK_STATE_FAILED"

    def test_jsonrpc_envelope_error_is_a_failure(self):
        from scheduler.local import _a2a_turn_failure

        assert "boom" in _a2a_turn_failure({"error": {"code": -32000, "message": "boom"}})

    @pytest.mark.parametrize("body", [None, "", [], {}, {"result": "not-a-dict"}, {"result": {}}])
    def test_unparseable_bodies_count_as_SUCCESS(self, body):
        """This feeds a backoff. Guessing "failed" from an unfamiliar shape would
        throttle healthy jobs, so anything we can't read is treated as fine."""
        from scheduler.local import _a2a_turn_failure

        assert _a2a_turn_failure(body) == ""


class TestFireOutcomeTracking:
    def _job(self, s: LocalScheduler, schedule: str = "0 14 * * *"):
        return s.add_job(prompt="drift check", schedule=schedule)

    def _row(self, s: LocalScheduler, job_id: str):
        db = sqlite3.connect(str(s.path))
        db.row_factory = sqlite3.Row
        try:
            return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_a_200_with_a_failed_task_is_recorded_as_a_failure(self, tmp_path, monkeypatch):
        """THE regression: six broken runs were logged as successes."""
        import httpx

        s = _make_scheduler(tmp_path)
        job = self._job(s)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )

        assert await s._fire(job) is True  # still DELIVERED — the POST worked
        row = self._row(s, job.id)
        assert row["consecutive_failures"] == 1
        assert "observability.audit" in row["last_error"]

    @pytest.mark.asyncio
    async def test_success_resets_the_streak(self, tmp_path, monkeypatch):
        """A job that recovers is immediately back on its normal cadence."""
        import httpx

        s = _make_scheduler(tmp_path)
        job = self._job(s)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )
        await s._fire(job)
        await s._fire(job)
        assert self._row(s, job.id)["consecutive_failures"] == 2

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        await s._fire(job)
        row = self._row(s, job.id)
        assert row["consecutive_failures"] == 0
        assert row["last_error"] is None
        assert row["last_ok"]

    @pytest.mark.asyncio
    async def test_backoff_pushes_the_next_fire_out_after_a_streak(self, tmp_path, monkeypatch):
        """Past the threshold a broken job stops burning a whole turn every day."""
        import httpx

        from scheduler.local import BACKOFF_AFTER_FAILURES

        s = _make_scheduler(tmp_path)
        job = self._job(s, schedule="0 14 * * *")
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )

        for _ in range(BACKOFF_AFTER_FAILURES - 1):
            await s._fire(job)
        before = self._row(s, job.id)["next_fire"]

        await s._fire(job)  # crosses the threshold
        after = self._row(s, job.id)["next_fire"]
        assert parse_iso_to_utc(after) > parse_iso_to_utc(before), "the streak must delay the next fire"

    @pytest.mark.asyncio
    async def test_backoff_is_capped(self, tmp_path, monkeypatch):
        """A long outage must not drift a daily job into firing once a year — and a
        job that quietly stops retrying is its own kind of silent failure."""
        import httpx

        from scheduler.local import MAX_BACKOFF_SLOTS

        s = _make_scheduler(tmp_path)
        job = self._job(s, schedule="0 14 * * *")
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )

        # Drive the streak far past the cap, then measure ONE more step. The cap bounds a
        # SINGLE backoff, so a single step is the only thing that can demonstrate it.
        for _ in range(25):
            job = s.get_job(job.id) or job
            await s._fire(job)

        before = parse_iso_to_utc(self._row(s, job.id)["next_fire"])
        job = s.get_job(job.id) or job
        await s._fire(job)
        step = parse_iso_to_utc(self._row(s, job.id)["next_fire"]) - before

        # Daily cron ⇒ one slot is one day. Both bounds matter and neither is free:
        # without `min(..., MAX_BACKOFF_SLOTS)` the 26th failure would advance
        # 2**23 days, and a job that stops moving has quietly stopped retrying.
        assert step.days <= MAX_BACKOFF_SLOTS, f"backoff ran away: {step}"
        assert step > timedelta(0), "a backed-off job must still retry"

        # (The previous version asserted a CUMULATIVE gap against `MAX_BACKOFF_SLOTS * 25`
        # — the per-call bound times the loop count — which restates the loop and holds
        # however large the cap is. It could not fail, so it protected nothing.)

    @pytest.mark.asyncio
    async def test_a_failing_job_is_never_disabled(self, tmp_path, monkeypatch):
        """Backoff, not a circuit breaker: the failure that motivated this was a
        transient backend outage, and silently switching off a job the operator
        depends on is worse than a slow retry."""
        import httpx

        s = _make_scheduler(tmp_path)
        job = self._job(s)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )
        for _ in range(10):
            job = s.get_job(job.id) or job
            await s._fire(job)

        row = self._row(s, job.id)
        assert row is not None, "the job must still exist"
        assert bool(row["enabled"]) is True


class TestBackoffFromPostClaimNextFire:
    """#3381: a cron row is advanced to its next slot at CLAIM time (``_tick`` calls
    ``_reschedule_or_delete`` before firing), so the failure backoff must base its delay
    on the PERSISTED post-claim ``next_fire`` — not the stale pre-claim snapshot the fire
    carries. Basing it on the snapshot made the first eligible backoff a no-op (it
    recomputed the slot the claim had already scheduled) and every later delay one slot
    short, so the deferred-slot count never matched the log.

    These cases drive the production sequence claim → reschedule → fire → settle against a
    persisted DB row. The ``#3376`` backoff tests above call ``_fire(job)`` in isolation,
    which skips the claim-time advance and so can never surface this timing bug.
    """

    HOURLY = "0 * * * *"  # top of every hour ⇒ one slot == one hour

    def _row(self, s: LocalScheduler, job_id: str):
        db = sqlite3.connect(str(s.path))
        db.row_factory = sqlite3.Row
        try:
            return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            db.close()

    def _pin_next_fire(self, s: LocalScheduler, job_id: str, when: datetime) -> None:
        db = sqlite3.connect(str(s.path))
        try:
            db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (when.isoformat(), job_id))
            db.commit()
        finally:
            db.close()

    async def _claim_fire_settle(self, s: LocalScheduler, now: datetime) -> None:
        """One production tick at a fixed ``now``: claim due jobs, advance cron rows at
        claim time, then fire + settle — exactly what ``_tick`` does, minus the poll
        loop's wall clock and off-loop task spawn (awaited inline so the settle lands)."""
        for job in s._claim_due_jobs(now):
            s._inflight_ids.add(job.id)
            cron = is_cron(job.schedule)
            if cron:
                s._reschedule_or_delete(job, fired_at=now)
            await s._fire_and_settle(job, cron)

    @pytest.mark.asyncio
    async def test_streak_defers_the_exact_logged_slots_beyond_the_claimed_slot(self, tmp_path, monkeypatch):
        """The 3rd consecutive failure (first past ``BACKOFF_AFTER_FAILURES``) must defer
        ONE slot beyond the already-claimed next slot, and the 4th must defer TWO."""
        import httpx

        from scheduler.local import BACKOFF_AFTER_FAILURES

        # The hour-by-hour timeline below assumes the 3rd fire is the first to back off.
        assert BACKOFF_AFTER_FAILURES == 3

        s = _make_scheduler(tmp_path)
        s.add_job("hourly sweep", self.HOURLY, job_id="hb")
        # Pin the row to a known slot so every claim timestamp below is deterministic.
        self._pin_next_fire(s, "hb", datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200, payload=_failed_body()))
        )

        # Rounds 1 & 2 fail below the threshold — no backoff, so each claim just rolls the
        # row to the next hourly slot (00:00 → 01:00 → 02:00).
        await self._claim_fire_settle(s, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        await self._claim_fire_settle(s, datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
        assert self._row(s, "hb")["consecutive_failures"] == 2
        assert self._row(s, "hb")["next_fire"] == "2026-01-01T02:00:00+00:00"

        # Round 3: claim at 02:00 advances the row to 03:00 (the already-claimed next slot),
        # then the fire fails with streak 3 ⇒ 1 backoff slot. The delay must land BEYOND
        # 03:00, at 04:00 — the stale-base bug recomputed 03:00 and deferred nothing.
        await self._claim_fire_settle(s, datetime(2026, 1, 1, 2, 0, tzinfo=UTC))
        row = self._row(s, "hb")
        assert row["consecutive_failures"] == 3
        assert row["next_fire"] == "2026-01-01T04:00:00+00:00", "1-slot backoff must defer past the claimed slot"

        # Round 4: claim at 04:00 advances to 05:00, fire fails with streak 4 ⇒ 2 backoff
        # slots. Deferring the EXACT logged count past the claimed slot lands at 07:00.
        await self._claim_fire_settle(s, datetime(2026, 1, 1, 4, 0, tzinfo=UTC))
        row = self._row(s, "hb")
        assert row["consecutive_failures"] == 4
        assert row["next_fire"] == "2026-01-01T07:00:00+00:00", "2-slot backoff must defer exactly two slots"

    @pytest.mark.asyncio
    async def test_successful_cron_claim_advances_exactly_one_slot(self, tmp_path, monkeypatch):
        """A cron fire that SUCCEEDS still lands on the ordinary next slot — the backoff
        path (and its post-claim base) must not touch a healthy job's cadence."""
        import httpx

        s = _make_scheduler(tmp_path)
        s.add_job("hourly sweep", self.HOURLY, job_id="ok")
        self._pin_next_fire(s, "ok", datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        # Default payload is a COMPLETED task ⇒ a successful turn.
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))

        await self._claim_fire_settle(s, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))

        row = self._row(s, "ok")
        assert row["consecutive_failures"] == 0
        assert row["next_fire"] == "2026-01-01T01:00:00+00:00"  # one slot, no backoff

    @pytest.mark.asyncio
    async def test_one_shot_deleted_through_the_claim_path(self, tmp_path, monkeypatch):
        """One-shot behaviour is untouched by the cron backoff fix: a delivered one-shot
        is deleted through the same claim → fire → settle path — no reschedule, no
        backoff (the ``is_cron`` guard keeps the post-claim base out of the one-shot
        path entirely)."""
        import httpx

        s = _make_scheduler(tmp_path)
        past = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        s.add_job("one and done", past.isoformat(), job_id="os")
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))

        await self._claim_fire_settle(s, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))

        assert self._row(s, "os") is None  # fired once, removed
