"""Scheduled-task result delivery to the originating chat session (#2990).

When a scheduled fire completes, its result is delivered back to the chat that CREATED
the schedule as a ``scheduler.completed`` event (the console renders a ScheduledReportCard
/ ScheduledChip). These cover the server-side shaping + guards:

* which fires deliver (a cron with an origin chat) vs which don't (no origin, a `wait`
  resume, a one-shot that already ran in its chat, an Activity-origin schedule);
* the recurring-collapse flag (first fire → full card; re-fires → chip);
* the stale-session guard (skip delivery to a chat idle > 24h).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import server
from a2a_impl.executor import TurnOutcome
from events import ACTIVITY_CONTEXT
from server import a2a


@dataclass
class _Outcome:
    origin: str = "scheduler"
    trigger: str = "job-1"
    context_id: str = ACTIVITY_CONTEXT
    text: str = "Swept the inbox: 3 new threads, 1 needs a reply."
    state: str = "completed"
    task_id: str = "task-abc"


@dataclass
class _Job:
    id: str = "job-1"
    origin_session: str | None = "chat-42"
    fire_count: int = 0
    schedule: str = "0 9 * * *"


class _Scheduler:
    """Minimal stand-in for STATE.scheduler exposing get_job."""

    def __init__(self, job: _Job | None):
        self._job = job

    def get_job(self, job_id):
        return self._job if (self._job and self._job.id == job_id) else None


@pytest.fixture
def _sched(monkeypatch):
    def _set(job: _Job | None):
        monkeypatch.setattr(server.STATE, "scheduler", _Scheduler(job))

    return _set


# ── payload shaping + guards (pure, sync) ────────────────────────────────────


def test_payload_built_for_cron_with_origin_session(_sched):
    # r2: a cron fire with an origin chat produces a delivery payload carrying the job
    # id, a fire timestamp, and the result summary.
    _sched(_Job())
    payload = a2a._scheduled_delivery_payload(_Outcome())
    assert payload is not None
    assert payload["job_id"] == "job-1"
    assert payload["origin_session"] == "chat-42"
    assert payload["fired_at"]  # ISO timestamp
    assert "Swept the inbox" in payload["summary"]
    assert payload["status"] == "completed"
    assert payload["activity_context"] == ACTIVITY_CONTEXT


def test_no_delivery_without_origin_session(_sched):
    # r6: a schedule created outside a chat carries no origin_session → no delivery.
    _sched(_Job(origin_session=None))
    assert a2a._scheduled_delivery_payload(_Outcome()) is None


def test_no_delivery_for_non_scheduler_turn(_sched):
    _sched(_Job())
    assert a2a._scheduled_delivery_payload(_Outcome(origin="operator")) is None
    assert a2a._scheduled_delivery_payload(_Outcome(origin="a2a")) is None


def test_no_delivery_for_wait_resume(_sched):
    # A `wait:` resume already runs IN its chat and surfaces via chat.resumed — no card.
    _sched(_Job(id="wait:chat-42"))
    assert a2a._scheduled_delivery_payload(_Outcome(trigger="wait:chat-42")) is None


def test_no_delivery_when_fire_ran_in_origin_session(_sched):
    # A one-shot resume runs IN the origin chat (context_id == origin) — chat.resumed
    # covers it, so no duplicate card.
    _sched(_Job(origin_session="chat-42"))
    assert a2a._scheduled_delivery_payload(_Outcome(context_id="chat-42")) is None


def test_no_delivery_when_origin_is_activity(_sched):
    # A schedule created FROM Activity would stamp Activity as origin — nothing to
    # surface there.
    _sched(_Job(origin_session=ACTIVITY_CONTEXT))
    assert a2a._scheduled_delivery_payload(_Outcome()) is None


def test_summary_truncated_to_cap(_sched):
    _sched(_Job())
    long_text = "x" * 2000
    payload = a2a._scheduled_delivery_payload(_Outcome(text=long_text))
    assert payload is not None
    assert len(payload["summary"]) <= a2a._SCHEDULED_SUMMARY_CAP + 1  # +1 for the ellipsis
    assert payload["summary"].endswith("…")


def test_failed_turn_marks_status_failed(_sched):
    _sched(_Job())
    payload = a2a._scheduled_delivery_payload(_Outcome(state="failed", text="boom"))
    assert payload is not None
    assert payload["status"] == "failed"


# ── recurring collapse (r4) ───────────────────────────────────────────────────


def test_first_fire_is_a_full_card(_sched):
    # fire_count 0 (pre-fire value) → first fire → full card (collapse False).
    _sched(_Job(fire_count=0))
    payload = a2a._scheduled_delivery_payload(_Outcome())
    assert payload is not None
    assert payload["collapse"] is False


def test_subsequent_fire_collapses_to_chip(_sched):
    # fire_count >= 1 → a recurring re-fire into the same session → compact chip.
    _sched(_Job(fire_count=1))
    payload = a2a._scheduled_delivery_payload(_Outcome())
    assert payload is not None
    assert payload["collapse"] is True


# ── stale-session guard (r3) ──────────────────────────────────────────────────


def test_is_recent_threshold():
    now = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
    assert a2a._is_recent(now - timedelta(hours=1), now, 24 * 3600) is True
    assert a2a._is_recent(now - timedelta(hours=48), now, 24 * 3600) is False
    # Naive timestamps read as UTC; a missing timestamp counts as recent.
    assert a2a._is_recent((now - timedelta(hours=1)).replace(tzinfo=None), now, 24 * 3600) is True
    assert a2a._is_recent(None, now, 24 * 3600) is True


@pytest.mark.asyncio
async def test_recently_active_defaults_true_without_task_store(monkeypatch):
    # Best-effort: no engine → can't prove staleness → don't suppress delivery.
    monkeypatch.setattr(server.STATE, "a2a_task_engine", None, raising=False)
    assert await a2a._session_recently_active("chat-42") is True


@pytest.mark.asyncio
async def test_deliver_publishes_when_session_active(monkeypatch):
    # r2: an active origin session gets the card.
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    monkeypatch.setattr(a2a, "_session_recently_active", _async_return(True))
    ok = await a2a._deliver_scheduled({"job_id": "job-1", "origin_session": "chat-42", "summary": "s"})
    assert ok is True
    assert [ev for ev, _ in published] == ["scheduler.completed"]


@pytest.mark.asyncio
async def test_deliver_skips_when_session_stale(monkeypatch):
    # r3: a chat idle > 24h is skipped — no ghost session, the result stays in Activity.
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    monkeypatch.setattr(a2a, "_session_recently_active", _async_return(False))
    ok = await a2a._deliver_scheduled({"job_id": "job-1", "origin_session": "chat-old", "summary": "s"})
    assert ok is False
    assert published == []


# ── end-to-end wiring through the terminal hook (sync, no loop) ───────────────


def test_terminal_hook_publishes_scheduler_completed(_sched, monkeypatch):
    # r2: driving the real terminal hook for a cron fire publishes scheduler.completed
    # (the no-loop path publishes inline; the stale guard runs only under a live loop).
    _sched(_Job())
    monkeypatch.setattr(server.STATE, "activity_log", None)
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    server._a2a_terminal(
        TurnOutcome(
            task_id="t",
            context_id=ACTIVITY_CONTEXT,
            state="completed",
            text="Swept the inbox.",
            origin="scheduler",
            trigger="job-1",
        )
    )
    completed = next((d for ev, d in published if ev == "scheduler.completed"), None)
    assert completed is not None
    assert completed["origin_session"] == "chat-42"
    assert completed["job_id"] == "job-1"


def test_terminal_hook_no_card_for_plain_operator_turn(_sched, monkeypatch):
    # r6: an ordinary chat turn (not a scheduler fire) never emits a card.
    _sched(_Job())
    monkeypatch.setattr(server.STATE, "activity_log", None)
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    server._a2a_terminal(
        TurnOutcome(task_id="t", context_id="chat-42", state="completed", text="hi", origin="operator")
    )
    assert not any(ev == "scheduler.completed" for ev, _ in published)


def _async_return(value):
    async def _f(*_a, **_kw):
        return value

    return _f
