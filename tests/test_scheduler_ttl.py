"""Auto-expiry for scheduled jobs (#2992): ``ttl`` and ``max_fires``.

Recurring schedules shouldn't run indefinitely by default. A job carries an
optional ``ttl`` (duration from ``created_at``) and ``max_fires`` (firing cap);
the fire loop bumps ``fire_count`` after each successful cron fire and cancels
the job once either bound is reached. The ``schedule_task`` tool applies a
default TTL of "7d" to cron schedules — one-shots are unaffected (they fire
once and are removed).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scheduler.interface import parse_ttl
from scheduler.local import LocalScheduler
from tools.lg_tools import _build_scheduler_tools

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
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stubs ``httpx.AsyncClient`` so a unit test exercises the fire path
    without a live A2A endpoint."""

    def __init__(self, response, **_kw):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        return self._response


def _stub_fire(monkeypatch, status_code: int = 200):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(status_code)))


def _rewrite_created_at(s: LocalScheduler, job_id: str, created_at: str) -> None:
    db = sqlite3.connect(str(s.path))
    db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job_id))
    db.commit()
    db.close()


# ── parse_ttl ───────────────────────────────────────────────────────────────


class TestParseTtl:
    def test_shorthand_days(self):
        assert parse_ttl("7d") == timedelta(days=7)

    def test_shorthand_hours(self):
        assert parse_ttl("24h") == timedelta(hours=24)

    def test_shorthand_weeks(self):
        assert parse_ttl("2w") == timedelta(weeks=2)

    def test_shorthand_minutes_and_seconds(self):
        assert parse_ttl("30m") == timedelta(minutes=30)
        assert parse_ttl("90s") == timedelta(seconds=90)

    def test_shorthand_case_and_whitespace(self):
        assert parse_ttl(" 7D ") == timedelta(days=7)

    def test_iso_duration_days(self):
        assert parse_ttl("P7D") == timedelta(days=7)

    def test_iso_duration_time_component(self):
        assert parse_ttl("PT12H") == timedelta(hours=12)
        assert parse_ttl("P1DT6H") == timedelta(days=1, hours=6)

    def test_iso_duration_weeks(self):
        assert parse_ttl("P2W") == timedelta(weeks=2)

    @pytest.mark.parametrize("bad", ["abc", "", "  ", "7x", "d7", "P", "PT", "7", "P7"])
    def test_nonsense_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_ttl(bad)

    def test_zero_duration_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            parse_ttl("0d")


# ── add_job: storage + validation ───────────────────────────────────────────


class TestAddJobExpiryFields:
    def test_ttl_stored_from_creation_time(self, tmp_path):
        # r1: a cron with ttl="7d" is created with a 7-day TTL from creation.
        s = _make_scheduler(tmp_path)
        before = datetime.now(UTC)
        job = s.add_job("check", "0 * * * *", ttl="7d")
        created = datetime.fromisoformat(job.created_at)
        assert job.ttl == "7d"
        assert before <= created <= datetime.now(UTC)
        # Round-trips through the DB with fire_count starting at 0.
        stored = s.list_jobs()[0]
        assert stored.ttl == "7d" and stored.max_fires is None and stored.fire_count == 0

    def test_max_fires_stored(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("check", "0 * * * *", max_fires=5)
        stored = s.list_jobs()[0]
        assert stored.max_fires == 5 and stored.ttl is None and stored.fire_count == 0

    def test_unparseable_ttl_rejected(self, tmp_path):
        s = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match="unparseable ttl"):
            s.add_job("check", "0 * * * *", ttl="abc")
        assert s.list_jobs() == []

    def test_nonpositive_max_fires_rejected(self, tmp_path):
        s = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match="max_fires"):
            s.add_job("check", "0 * * * *", max_fires=0)

    def test_defaults_are_none_when_omitted(self, tmp_path):
        s = _make_scheduler(tmp_path)
        s.add_job("check", "0 * * * *")
        stored = s.list_jobs()[0]
        assert stored.ttl is None and stored.max_fires is None and stored.fire_count == 0


class TestMigration:
    def test_pre_expiry_db_migrates_cleanly(self, tmp_path):
        # r9: a store created before ttl/max_fires/fire_count existed gets the
        # columns via the lazy ALTER-TABLE migration; old rows read as unset.
        s = _make_scheduler(tmp_path)
        old_schema = (
            "DROP TABLE jobs;"
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, "
            "schedule TEXT NOT NULL, agent_name TEXT NOT NULL, next_fire TEXT NOT NULL, "
            "last_fire TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
            "timezone TEXT, context_id TEXT);"
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
        assert len(jobs) == 1
        assert jobs[0].ttl is None and jobs[0].max_fires is None and jobs[0].fire_count == 0
        # New rows in the migrated store carry the new fields.
        s2.add_job("new", "0 * * * *", job_id="new", ttl="7d", max_fires=3)
        got = {j.id: j for j in s2.list_jobs()}
        assert got["new"].ttl == "7d" and got["new"].max_fires == 3


# ── fire loop: fire_count + auto-expiry ─────────────────────────────────────


class TestFireCountAndExpiry:
    async def test_successful_cron_fire_increments_fire_count(self, tmp_path, monkeypatch):
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1")
        job = s.list_jobs()[0]

        await s._fire_and_settle(job, cron=True)

        stored = s.list_jobs()[0]
        assert stored.fire_count == 1

    async def test_failed_fire_does_not_increment(self, tmp_path, monkeypatch):
        _stub_fire(monkeypatch, 500)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1", max_fires=1)
        job = s.list_jobs()[0]

        await s._fire_and_settle(job, cron=True)

        stored = s.list_jobs()[0]  # not expired — the fire never landed
        assert stored.fire_count == 0

    async def test_max_fires_cancels_after_n_firings(self, tmp_path, monkeypatch, caplog):
        # r3: max_fires=2 → survives the first fire, cancelled after the second.
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1", max_fires=2)

        await s._fire_and_settle(s.list_jobs()[0], cron=True)
        assert s.list_jobs()[0].fire_count == 1  # still alive

        with caplog.at_level("INFO", logger="scheduler.local"):
            await s._fire_and_settle(s.list_jobs()[0], cron=True)
        assert s.list_jobs() == []
        assert "expired: reached max_fires" in caplog.text

    async def test_ttl_cancels_once_elapsed(self, tmp_path, monkeypatch, caplog):
        # r2: a job with ttl="7d" whose created_at is 8 days back is cancelled
        # after its next successful fire, with a log line.
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1", ttl="7d")
        _rewrite_created_at(s, "c1", (datetime.now(UTC) - timedelta(days=8)).isoformat())

        with caplog.at_level("INFO", logger="scheduler.local"):
            await s._fire_and_settle(s.list_jobs()[0], cron=True)

        assert s.list_jobs() == []
        assert "expired: reached TTL" in caplog.text

    async def test_ttl_not_yet_elapsed_keeps_the_job(self, tmp_path, monkeypatch):
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1", ttl="7d")

        await s._fire_and_settle(s.list_jobs()[0], cron=True)

        stored = s.list_jobs()[0]
        assert stored.fire_count == 1  # fired and survived

    async def test_iso_ttl_form_expires_too(self, tmp_path, monkeypatch):
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("hourly", "0 * * * *", job_id="c1", ttl="P7D")
        _rewrite_created_at(s, "c1", (datetime.now(UTC) - timedelta(days=8)).isoformat())

        await s._fire_and_settle(s.list_jobs()[0], cron=True)

        assert s.list_jobs() == []


# ── schedule_task / list_schedules tools ────────────────────────────────────


class _FakeJob:
    def __init__(self, next_fire: str):
        self.id = "job-1"
        self.next_fire = next_fire


class _FakeScheduler:
    """Records the expiry kwargs the tool passes through to ``add_job``."""

    def __init__(self):
        self.last_ttl: str | None = "__unset__"
        self.last_max_fires: int | None = "__unset__"

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None, ttl=None, max_fires=None):
        self.last_ttl = ttl
        self.last_max_fires = max_fires
        return _FakeJob(schedule)

    def cancel_job(self, job_id):
        return False

    def list_jobs(self):
        return []


def _tool(sched, name: str):
    return next(t for t in _build_scheduler_tools(sched) if t.name == name)


class TestScheduleTaskTool:
    async def test_cron_without_ttl_gets_the_7d_default(self):
        # r4: a recurring cron created without an explicit ttl defaults to "7d".
        sched = _FakeScheduler()
        out = await _tool(sched, "schedule_task").ainvoke({"prompt": "check", "when": "0 * * * *"})
        assert "Scheduled job" in out
        assert sched.last_ttl == "7d"

    async def test_one_shot_gets_no_default_ttl(self):
        # r5: one-shots fire once and are removed — no TTL is applied.
        sched = _FakeScheduler()
        out = await _tool(sched, "schedule_task").ainvoke(
            {"prompt": "check", "when": "2099-05-01T15:00:00+00:00", "state": {"session_id": "chat-1"}}
        )
        assert "Scheduled job" in out
        assert sched.last_ttl is None

    async def test_explicit_ttl_overrides_the_default(self):
        sched = _FakeScheduler()
        await _tool(sched, "schedule_task").ainvoke({"prompt": "check", "when": "0 * * * *", "ttl": "30d"})
        assert sched.last_ttl == "30d"

    async def test_max_fires_passed_through(self):
        # r3 (tool side): max_fires rides the job to the backend.
        sched = _FakeScheduler()
        out = await _tool(sched, "schedule_task").ainvoke(
            {"prompt": "check", "when": "0 * * * *", "max_fires": 5}
        )
        assert "Scheduled job" in out
        assert sched.last_max_fires == 5

    async def test_unparseable_ttl_returns_validation_error(self):
        # r7: "abc" is rejected before anything is scheduled.
        sched = _FakeScheduler()
        out = await _tool(sched, "schedule_task").ainvoke(
            {"prompt": "check", "when": "0 * * * *", "ttl": "abc"}
        )
        assert out.startswith("Error:") and "ttl" in out
        assert sched.last_ttl == "__unset__"  # add_job never called

    async def test_nonpositive_max_fires_returns_error(self):
        sched = _FakeScheduler()
        out = await _tool(sched, "schedule_task").ainvoke(
            {"prompt": "check", "when": "0 * * * *", "max_fires": 0}
        )
        assert out.startswith("Error:") and "max_fires" in out
        assert sched.last_max_fires == "__unset__"


class TestListSchedulesTool:
    async def test_shows_ttl_max_fires_and_fire_count(self, tmp_path):
        # r6: expiry state is visible per job, against the real backend.
        s = _make_scheduler(tmp_path)
        s.add_job("bounded sweep", "0 * * * *", job_id="bounded", ttl="7d", max_fires=10)
        s.add_job("plain sweep", "0 9 * * *", job_id="plain")

        out = await _tool(s, "list_schedules").ainvoke({})

        bounded = next(line for line in out.splitlines() if line.startswith("bounded"))
        assert "ttl=7d" in bounded and "max_fires=10" in bounded and "fires=0" in bounded
        plain = next(line for line in out.splitlines() if line.startswith("plain"))
        assert "ttl=-" in plain and "max_fires=-" in plain and "fires=0" in plain

    async def test_fire_count_reflected_after_a_fire(self, tmp_path, monkeypatch):
        _stub_fire(monkeypatch, 200)
        s = _make_scheduler(tmp_path)
        s.add_job("sweep", "0 * * * *", job_id="c1", ttl="7d")
        await s._fire_and_settle(s.list_jobs()[0], cron=True)

        out = await _tool(s, "list_schedules").ainvoke({})
        assert "fires=1" in out
