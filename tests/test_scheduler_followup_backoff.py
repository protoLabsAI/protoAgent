"""Recurring follow-up backoff while a chat waits on the operator (ADR 0129 S14, #5270).

A recurring follow-up that keeps reporting nothing new while its origin chat is waiting
on the operator (a ``waiting`` task or a pending decision) backs off to one fire per hour
and posts a single "Still waiting on you" card instead of re-reporting the same parked
work every slot. Two halves:

* server (``server.a2a``): counts unchanged runs, applies the waiting test, arms the
  backoff and substitutes the one-time card;
* scheduler (``scheduler.local``): skips a due occurrence while the backoff holds, with
  no claim, no failure and no change to the stored schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import server
from scheduler import followup_backoff
from server import a2a
from tests.test_scheduled_delivery import _Job, _Outcome, _Scheduler


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    # All the backoff state is in-process module state — isolate every piece per test.
    monkeypatch.setattr(a2a, "_SCHEDULED_CARD_HASHES", {})
    monkeypatch.setattr(a2a, "_SCHEDULED_UNCHANGED_RUNS", {})
    followup_backoff.clear_all()
    yield
    followup_backoff.clear_all()


# ── fakes for the waiting test (both stores are plain sqlite — sync) ──────────


class _FakeTasks:
    def __init__(self, *, waiting_for: str | None = None, raises: bool = False):
        self._waiting_for = waiting_for
        self._raises = raises

    def list(self, *, session_id=None, include_closed=True):
        if self._raises:
            raise RuntimeError("tasks store unavailable")
        if session_id == self._waiting_for:
            return [{"id": "t1", "status": "waiting", "session_id": session_id}]
        return []


class _FakeDecisions:
    def __init__(self, *, pending_for: str | None = None, raises: bool = False):
        self._pending_for = pending_for
        self._raises = raises

    def list(self, *, state=None, session_id=None, limit=200):
        if self._raises:
            raise RuntimeError("decision store unavailable")
        if state == "pending" and session_id == self._pending_for:
            return [{"id": "d1", "session_id": session_id}]
        return []


@pytest.fixture
def _events(monkeypatch):
    events: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda ev, d, **_kw: events.append((ev, d)))
    return events


def _cards(events):
    return [d for ev, d in events if ev == "scheduler.completed"]


def _setup(monkeypatch, *, job=None, tasks=None, decisions=None):
    monkeypatch.setattr(server.STATE, "scheduler", _Scheduler(job or _Job()))
    monkeypatch.setattr(server.STATE, "tasks_store", tasks, raising=False)
    monkeypatch.setattr(server.STATE, "decision_store", decisions, raising=False)


def _run(**kw):
    # The full sync delivery path (no running loop → inline publish), like the bd-x66jt
    # tests, so the backoff side effects + card substitution run end to end.
    a2a._handle_scheduled_delivery(_Outcome(**kw))


# ── server: unchanged-run counting, waiting test, backoff arming ──────────────


def test_two_unchanged_runs_while_waiting_back_off_and_post_one_card(monkeypatch, _events):
    # r1: a recurring follow-up that reports nothing new twice while its chat waits on the
    # operator backs off and posts exactly ONE "still waiting" card — not a third, fourth…
    _setup(monkeypatch, tasks=_FakeTasks(waiting_for="chat-42"))
    _run(text="NO_UPDATE")  # run 1: unchanged, count 1 — no card yet
    _run(text="NO_UPDATE")  # run 2: unchanged, count 2 — arm + the one card
    _run(text="NO_UPDATE")  # run 3: still quiet-and-waiting — extends the deadline, no 2nd card
    cards = _cards(_events)
    assert len(cards) == 1
    assert cards[0]["summary"] == a2a._FOLLOWUP_WAITING_SUMMARY
    assert followup_backoff.backoff_until("job-1", "") is not None


def test_pending_decision_also_counts_as_waiting(monkeypatch, _events):
    # The waiting test is satisfied by a pending decision for the session too, not only a
    # waiting task.
    _setup(monkeypatch, tasks=_FakeTasks(), decisions=_FakeDecisions(pending_for="chat-42"))
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    assert [c["summary"] for c in _cards(_events)] == [a2a._FOLLOWUP_WAITING_SUMMARY]
    assert followup_backoff.backoff_until("job-1", "") is not None


def test_unchanged_runs_without_waiting_never_back_off(monkeypatch, _events):
    # r2: nothing new, but the chat is NOT waiting on the operator → no backoff, no card,
    # however many times it runs quiet.
    _setup(monkeypatch, tasks=_FakeTasks(), decisions=_FakeDecisions())
    for _ in range(4):
        _run(text="NO_UPDATE")
    assert _cards(_events) == []
    assert followup_backoff.backoff_until("job-1", "") is None


def test_new_content_clears_the_backoff(monkeypatch, _events):
    # r2: once backed off, a run that reports something new clears the backoff and posts
    # its content card like any normal fire.
    _setup(monkeypatch, tasks=_FakeTasks(waiting_for="chat-42"))
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    assert followup_backoff.backoff_until("job-1", "") is not None
    _run(text="One review landed on #88.")
    assert followup_backoff.backoff_until("job-1", "") is None
    assert [c["summary"] for c in _cards(_events)] == [a2a._FOLLOWUP_WAITING_SUMMARY, "One review landed on #88."]


def test_answering_the_waiting_task_clears_the_backoff(monkeypatch, _events):
    # r2: the waiting task is answered (no longer waiting) between fires → the next quiet
    # run clears the backoff rather than extending it.
    tasks = _FakeTasks(waiting_for="chat-42")
    _setup(monkeypatch, tasks=tasks)
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    assert followup_backoff.backoff_until("job-1", "") is not None
    tasks._waiting_for = None  # operator answered — the session no longer waits
    _run(text="NO_UPDATE")
    assert followup_backoff.backoff_until("job-1", "") is None


def test_one_shot_jobs_never_back_off(monkeypatch, _events):
    # r2: a one-shot (ISO schedule) is never a backoff candidate, even quiet and waiting.
    _setup(monkeypatch, job=_Job(schedule="2026-01-01T09:00:00+00:00"),
           tasks=_FakeTasks(waiting_for="chat-42"))
    for _ in range(4):
        _run(text="NO_UPDATE")
    assert _cards(_events) == []
    assert followup_backoff.backoff_until("job-1", "") is None


def test_wait_resume_never_backs_off(monkeypatch, _events):
    # r2: a wait: resume runs IN its chat and is never counted toward backoff.
    _setup(monkeypatch, job=_Job(id="wait:chat-42"), tasks=_FakeTasks(waiting_for="chat-42"))
    for _ in range(4):
        _run(trigger="wait:chat-42", text="NO_UPDATE")
    assert _cards(_events) == []
    assert followup_backoff.backoff_until("wait:chat-42", "") is None


def test_a_raising_store_means_no_backoff(monkeypatch, _events):
    # r2: an unreadable tasks/decision store fails quiet — treated as "not waiting", so a
    # backoff is never armed on a store we merely couldn't read.
    _setup(monkeypatch, tasks=_FakeTasks(raises=True), decisions=_FakeDecisions(raises=True))
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    assert _cards(_events) == []
    assert followup_backoff.backoff_until("job-1", "") is None


def test_waiting_helper_reads_both_stores(monkeypatch):
    # The waiting test proper: a waiting task OR a pending decision, each alone, is enough;
    # an unrelated session is not; a raising store is quiet-false.
    monkeypatch.setattr(server.STATE, "tasks_store", _FakeTasks(waiting_for="chat-1"), raising=False)
    monkeypatch.setattr(server.STATE, "decision_store", _FakeDecisions(pending_for="chat-2"), raising=False)
    assert a2a._followup_waiting_on_operator("chat-1") is True
    assert a2a._followup_waiting_on_operator("chat-2") is True
    assert a2a._followup_waiting_on_operator("chat-3") is False
    monkeypatch.setattr(server.STATE, "tasks_store", _FakeTasks(raises=True), raising=False)
    assert a2a._followup_waiting_on_operator("chat-1") is False


# ── scheduler: skip a backed-off due occurrence ───────────────────────────────


@pytest.mark.asyncio
async def test_backed_off_due_occurrence_is_skipped_without_a_failure(tmp_path, monkeypatch):
    # r3: while backed off, a recurring job's due slot is skipped — nothing is claimed or
    # fired, no failure is recorded, and the stored next_fire is left exactly where it was.
    from scheduler.local import LocalScheduler

    fired: list = []
    scheduler = LocalScheduler("backoff", invoke_url="http://test", db_dir=tmp_path,
                               event_publish=lambda topic, data: fired.append((topic, data)))
    job = scheduler.add_job("follow up", "*/5 * * * *")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with scheduler._connect() as db:
        db.execute("UPDATE jobs SET next_fire = ?", (past,))
    before = scheduler.get_job(job.id)
    assert scheduler._claim_due_jobs(datetime.now(UTC)), "precondition: the job is due"

    followup_backoff.set_backoff(job.id, job.created_at, (datetime.now(UTC) + timedelta(hours=1)).timestamp())
    await scheduler._tick()

    after = scheduler.get_job(job.id)
    assert after.next_fire == before.next_fire  # stored schedule unchanged
    assert after.consecutive_failures == 0  # a skip is not a failure
    assert not any(topic == "scheduler.fired" for topic, _ in fired)
    with scheduler._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 0  # nothing claimed


def test_followup_backed_off_gate(tmp_path):
    # The scheduler's skip gate: only a recurring (cron) job is ever backed off — a
    # one-shot with the same key armed is never skipped; an elapsed deadline fires.
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("gate", invoke_url="http://test", db_dir=tmp_path)
    cron = scheduler.add_job("follow up", "*/5 * * * *")
    oneshot = scheduler.add_job("do once", (datetime.now(UTC) + timedelta(days=1)).isoformat())
    now = datetime.now(UTC)

    followup_backoff.set_backoff(cron.id, cron.created_at, (now + timedelta(hours=1)).timestamp())
    assert scheduler._followup_backed_off(cron, now) is True
    # One-shot: even with an entry armed under its key, an ISO schedule is never skipped.
    followup_backoff.set_backoff(oneshot.id, oneshot.created_at, (now + timedelta(hours=1)).timestamp())
    assert scheduler._followup_backed_off(oneshot, now) is False
    # Elapsed deadline → fire normally again.
    followup_backoff.set_backoff(cron.id, cron.created_at, (now - timedelta(seconds=1)).timestamp())
    assert scheduler._followup_backed_off(cron, now) is False


def test_backed_off_gate_clears_when_chat_stops_waiting(tmp_path):
    # r2 (the blocking fix): a backed-off slot must re-test whether the chat is STILL
    # waiting — not wait out the whole hour. With the server's waiting predicate injected,
    # the gate holds while the chat waits, then clears the backoff and fires the instant the
    # operator answers (the predicate flips to "not waiting"), well before the deadline.
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("rewait", invoke_url="http://test", db_dir=tmp_path)
    cron = scheduler.add_job("follow up", "*/5 * * * *", origin_session="chat-42")
    now = datetime.now(UTC)
    deadline = (now + timedelta(hours=1)).timestamp()
    followup_backoff.set_backoff(cron.id, cron.created_at, deadline)

    waiting = {"on": True}
    followup_backoff.set_waiting_check(lambda sid: waiting["on"])
    # Still waiting, deadline far off → skip.
    assert scheduler._followup_backed_off(cron, now) is True
    assert followup_backoff.backoff_until(cron.id, cron.created_at) == deadline

    # Operator answered → not waiting. The gate clears the backoff and fires now.
    waiting["on"] = False
    assert scheduler._followup_backed_off(cron, now) is False
    assert followup_backoff.backoff_until(cron.id, cron.created_at) is None


def test_backed_off_gate_without_a_predicate_holds_on_the_deadline(tmp_path):
    # No predicate registered (scheduler-only process / before any arm): the re-test is a
    # no-op and the backoff holds purely on its deadline — never stays stuck, never fires early.
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("nopred", invoke_url="http://test", db_dir=tmp_path)
    cron = scheduler.add_job("follow up", "*/5 * * * *", origin_session="chat-42")
    now = datetime.now(UTC)
    followup_backoff.set_backoff(cron.id, cron.created_at, (now + timedelta(hours=1)).timestamp())
    # still_waiting → None (no predicate) → hold on the deadline.
    assert followup_backoff.still_waiting("chat-42") is None
    assert scheduler._followup_backed_off(cron, now) is True


def test_arming_registers_the_scheduler_waiting_recheck(monkeypatch, _events):
    # End-to-end wiring: arming the backoff (server path) registers the live waiting test as
    # the scheduler's re-check seam, so the scheduler gate reads the real store state. When
    # the operator answers, the same seam the gate calls now reports "not waiting".
    tasks = _FakeTasks(waiting_for="chat-42")
    _setup(monkeypatch, tasks=tasks)
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")  # arms the backoff — and registers the re-check
    assert followup_backoff.still_waiting("chat-42") is True
    tasks._waiting_for = None  # operator answered the waiting task
    assert followup_backoff.still_waiting("chat-42") is False


def test_editing_a_job_clears_its_backoff(tmp_path):
    # r2: an explicit edit is the operator engaging with the job — the follow-up backoff
    # it carried clears so it fires fresh.
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("edit", invoke_url="http://test", db_dir=tmp_path)
    job = scheduler.add_job("follow up", "*/5 * * * *")
    followup_backoff.set_backoff(job.id, job.created_at, (datetime.now(UTC) + timedelta(hours=1)).timestamp())
    scheduler.update_job(job.id, prompt="follow up harder", schedule="*/5 * * * *")
    assert followup_backoff.backoff_until(job.id, job.created_at) is None


@pytest.mark.parametrize("release", ["deadline", "answered", "edit"])
def test_backoff_survives_restart_and_release_is_persisted(tmp_path, monkeypatch, release):
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("durable", invoke_url="http://test", db_dir=tmp_path)
    job = scheduler.add_job("follow up", "*/5 * * * *", origin_session="chat-42")
    now = datetime.now(UTC)
    deadline = (now + timedelta(hours=1)).timestamp()
    followup_backoff.set_backoff(job.id, job.created_at, deadline)
    followup_backoff.clear_all()  # Simulate losing all process state, not the database.
    scheduler = LocalScheduler("durable", invoke_url="http://test", db_dir=tmp_path)
    assert followup_backoff.backoff_until(job.id, job.created_at) == deadline
    assert followup_backoff.is_backed_off(job.id, job.created_at, now.timestamp())
    assert scheduler._followup_backed_off(job, now)

    if release == "deadline":
        assert not scheduler._followup_backed_off(job, now + timedelta(hours=1))
    elif release == "answered":
        # A real restart re-wires the waiting re-test from the server at BOOT (not by hand):
        # the chat is no longer waiting, so the reloaded backoff clears early on the next tick.
        monkeypatch.setattr(server.STATE, "tasks_store", _FakeTasks(), raising=False)
        monkeypatch.setattr(server.STATE, "decision_store", _FakeDecisions(), raising=False)
        a2a.register_followup_waiting_check()
        assert not scheduler._followup_backed_off(job, now)
    else:
        scheduler.update_job(job.id, "updated follow up", "*/5 * * * *")
    expected = deadline if release == "deadline" else None
    assert followup_backoff.backoff_until(job.id, job.created_at) == expected
    with scheduler._connect() as db:
        assert db.execute(
            "SELECT followup_backoff_until FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0] == expected
    followup_backoff.clear_all()
    scheduler = LocalScheduler("durable", invoke_url="http://test", db_dir=tmp_path)
    assert followup_backoff.backoff_until(job.id, job.created_at) == expected
    assert not followup_backoff.is_backed_off(job.id, job.created_at, deadline)


def test_restart_reregisters_waiting_check_so_backoff_clears_early(tmp_path, monkeypatch):
    # The blocking fix: after a restart the deadline is reloaded from the store, but the
    # waiting re-test lives in the server (it owns the stores — import layering) and must be
    # re-wired at BOOT, not only when a fresh backoff is armed. Otherwise still_waiting is None
    # and a reloaded backoff whose chat was already answered stalls to the whole-hour deadline.
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("reboot", invoke_url="http://test", db_dir=tmp_path)
    job = scheduler.add_job("follow up", "*/5 * * * *", origin_session="chat-42")
    now = datetime.now(UTC)
    followup_backoff.set_backoff(job.id, job.created_at, (now + timedelta(hours=1)).timestamp())

    # Restart: process state gone, DB intact. With no predicate re-wired yet, still_waiting is
    # None and the gate would stall on the deadline — the bug.
    followup_backoff.clear_all()
    scheduler = LocalScheduler("reboot", invoke_url="http://test", db_dir=tmp_path)
    assert followup_backoff.still_waiting("chat-42") is None
    assert scheduler._followup_backed_off(job, now) is True

    # The server's boot registration re-wires its waiting test against the live stores
    # (ADR 0131 P1). While the chat is still waiting the gate still holds...
    tasks = _FakeTasks(waiting_for="chat-42")
    monkeypatch.setattr(server.STATE, "tasks_store", tasks, raising=False)
    monkeypatch.setattr(server.STATE, "decision_store", _FakeDecisions(), raising=False)
    a2a.register_followup_waiting_check()
    assert scheduler._followup_backed_off(job, now) is True

    # ...and the instant the operator answers, the reloaded backoff clears early — with no
    # manual set_waiting_check — and that clear is persisted through to the store.
    tasks._waiting_for = None
    assert scheduler._followup_backed_off(job, now) is False
    assert followup_backoff.backoff_until(job.id, job.created_at) is None
    with scheduler._connect() as db:
        assert db.execute(
            "SELECT followup_backoff_until FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0] is None


def test_recreated_job_does_not_inherit_backoff_or_stale_writes(tmp_path):
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("recreate", invoke_url="http://test", db_dir=tmp_path)
    old = scheduler.add_job("follow up", "*/5 * * * *", job_id="same-id")
    deadline = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
    followup_backoff.set_backoff(old.id, old.created_at, deadline)
    assert scheduler.cancel_job(old.id)
    assert followup_backoff.backoff_until(old.id, old.created_at) is None
    new = scheduler.add_job("replacement", "*/5 * * * *", job_id=old.id)
    assert new.created_at != old.created_at
    followup_backoff.set_backoff(old.id, old.created_at, deadline)
    followup_backoff.clear_all()
    scheduler = LocalScheduler("recreate", invoke_url="http://test", db_dir=tmp_path)
    assert not followup_backoff.is_backed_off(new.id, new.created_at, deadline - 1)
    followup_backoff.set_backoff(new.id, new.created_at, deadline)
    followup_backoff.clear(old.id, old.created_at)
    assert followup_backoff.backoff_until(new.id, new.created_at) == deadline


def test_backoff_column_migrates_existing_jobs(tmp_path):
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("legacy", invoke_url="http://test", db_dir=tmp_path)
    job = scheduler.add_job("existing follow up", "*/5 * * * *")
    with scheduler._connect() as db:
        db.execute("ALTER TABLE jobs DROP COLUMN followup_backoff_until")
    followup_backoff.clear_all()
    scheduler = LocalScheduler("legacy", invoke_url="http://test", db_dir=tmp_path)
    assert scheduler.get_job(job.id).prompt == job.prompt
    assert followup_backoff.backoff_until(job.id, job.created_at) is None
    deadline = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
    followup_backoff.set_backoff(job.id, job.created_at, deadline)
    followup_backoff.clear_all()
    scheduler = LocalScheduler("legacy", invoke_url="http://test", db_dir=tmp_path)
    assert followup_backoff.is_backed_off(job.id, job.created_at, deadline - 1)


def test_failed_store_load_does_not_poison_registry(tmp_path):
    import sqlite3

    from scheduler.local import LocalScheduler

    broken = LocalScheduler("broken", invoke_url="http://test", db_dir=tmp_path / "broken")
    with broken._connect() as db:
        db.execute("ALTER TABLE jobs DROP COLUMN followup_backoff_until")
    followup_backoff.clear_all()
    with pytest.raises(sqlite3.OperationalError, match="followup_backoff_until"):
        followup_backoff._register_store(broken)

    # Failed registration leaves the in-memory fallback usable.
    followup_backoff.set_backoff("memory", "generation", 100)
    assert followup_backoff.is_backed_off("memory", "generation", 99)
    followup_backoff.clear("memory")
    assert followup_backoff.backoff_until("memory", "generation") is None

    healthy = LocalScheduler("healthy", invoke_url="http://test", db_dir=tmp_path / "healthy")
    job = healthy.add_job("follow up", "*/5 * * * *")
    followup_backoff.set_backoff(job.id, job.created_at, 100)
    assert followup_backoff.is_backed_off(job.id, job.created_at, 99)
    followup_backoff.clear(job.id, job.created_at)
    assert followup_backoff.backoff_until(job.id, job.created_at) is None


@pytest.mark.parametrize("persistent", [False, True])
def test_elapsed_backoff_does_not_repeat_waiting_card(tmp_path, monkeypatch, _events, persistent):
    from scheduler.local import LocalScheduler

    if persistent:
        scheduler = LocalScheduler("cards", invoke_url="http://test", db_dir=tmp_path)
        job = scheduler.add_job("follow up", "*/5 * * * *", job_id="job-1", origin_session="chat-42")
    else:
        job = _Job()
    _setup(monkeypatch, job=job, tasks=_FakeTasks(waiting_for="chat-42"))
    _run(text="NO_UPDATE")
    _run(text="NO_UPDATE")
    generation = getattr(job, "created_at", "") or ""
    deadline = followup_backoff.backoff_until(job.id, generation)
    assert not followup_backoff.is_backed_off(job.id, generation, deadline)
    assert followup_backoff.backoff_until(job.id, generation) == deadline
    if persistent:
        followup_backoff.clear_all()
        scheduler = LocalScheduler("cards", invoke_url="http://test", db_dir=tmp_path)
    _run(text="NO_UPDATE")
    assert [c["summary"] for c in _cards(_events)] == [a2a._FOLLOWUP_WAITING_SUMMARY]
