"""Scheduled result cards: no card when a run has nothing new to report (bd-x66jt) — the
quiet-reply guard (empty / ``NO_UPDATE``), the per-job-generation repeat guard, the
delivered-cards collapse flag, and the async delivery path's fingerprint bookkeeping."""

from __future__ import annotations

import pytest

import server
from a2a_impl.executor import TurnOutcome
from events import ACTIVITY_CONTEXT
from server import a2a
from tests.test_scheduled_delivery import _Job, _Outcome, _Scheduler


@pytest.fixture(autouse=True)
def _fresh_guard(monkeypatch):  # the repeat guard's memory is module state — isolate per test
    monkeypatch.setattr(a2a, "_SCHEDULED_CARD_HASHES", {})


@pytest.fixture
def _sched(monkeypatch):
    return lambda job: monkeypatch.setattr(server.STATE, "scheduler", _Scheduler(job))


@pytest.fixture
def _events(monkeypatch):
    events: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda ev, d, **_kw: events.append((ev, d)))
    return events


def _fire(**kw):
    # The full sync delivery path (no loop → inline publish), so remember-on-publish runs.
    a2a._handle_scheduled_delivery(_Outcome(**kw))


def _cards(events):
    return [d for ev, d in events if ev == "scheduler.completed"]


def _liveness(value):
    async def _f(*_a, **_kw):
        return value

    return _f


@pytest.mark.parametrize("text", ["", "   ", "NO_UPDATE", "Checked CI — still running, nothing merged.\n\nNO_UPDATE"])
def test_quiet_completed_run_posts_no_card(_sched, text):
    # r1: a completed answer that is empty, exactly NO_UPDATE, or ends with one → no card.
    _sched(_Job())
    assert a2a._scheduled_delivery_payload(_Outcome(text=text)) is None


def test_no_update_mid_answer_or_on_a_failed_run_still_posts(_sched):
    # Only a FINAL NO_UPDATE line is a quiet reply; a failed run always posts (r3).
    _sched(_Job())
    assert a2a._scheduled_delivery_payload(_Outcome(text="Documented NO_UPDATE.\nShipped it.")) is not None
    assert a2a._scheduled_delivery_payload(_Outcome(state="failed", text="NO_UPDATE"))["status"] == "failed"


def test_suppressed_run_still_lands_on_the_activity_feed(_sched, _events, monkeypatch):
    # Only the chat card is suppressed — the run still surfaces on the Activity feed.
    _sched(_Job())
    monkeypatch.setattr(server.STATE, "activity_log", None)
    server._a2a_terminal(
        TurnOutcome(task_id="t", context_id=ACTIVITY_CONTEXT, state="completed",
                    text="Nothing changed.\nNO_UPDATE", origin="scheduler", trigger="job-1")
    )
    topics = [ev for ev, _ in _events]
    assert "scheduler.completed" not in topics and "activity.message" in topics


def test_repeat_cards_suppressed_until_the_answer_changes(_sched, _events):
    # r2: runs saying the same thing modulo whitespace/timestamps → one card; change posts.
    _sched(_Job())
    _fire(text="As of 2026-11-12T09:10:00Z: CI green,  nothing merged.")
    _fire(text="As of 2026-11-12 11:48: CI green,\nnothing merged.")
    _fire(text="As of 11:58: CI green, nothing merged.")
    _fire(text="One new review landed on #88.")
    assert ["CI green" in c["summary"] for c in _cards(_events)] == [True, False]
    # The delivery bookkeeping never rides the event (sync no-loop path).
    assert all("card_fingerprint" not in c for c in _cards(_events))


def test_a_new_job_generation_posts_the_same_text_again(_sched, _events):
    # Cancel-and-recreate on the same id (fresh created_at) is never a "repeat" (r2).
    old, new = _Job(), _Job()
    old.created_at, new.created_at = "2026-11-11T17:40:00+00:00", "2026-11-12T08:00:00+00:00"
    for job in (old, old, new):
        _sched(job)
        _fire(text="Review queue is empty.")
    assert len(_cards(_events)) == 2


async def test_delivery_bookkeeping_feeds_the_guard_only_after_a_publish(_sched, _events, monkeypatch):
    # The production async path (_deliver_scheduled): a stale-session skip delivers and
    # records nothing, so the same result still posts once the chat wakes; a publish pops
    # the bookkeeping fingerprint off the event and records it AFTER the publish, so only
    # then does the next identical run read as a repeat.
    _sched(_Job())
    monkeypatch.setattr(a2a, "_session_recently_active", _liveness(False))
    assert await a2a._deliver_scheduled(a2a._scheduled_delivery_payload(_Outcome(text="All green."))) is False
    assert _cards(_events) == []
    monkeypatch.setattr(a2a, "_session_recently_active", _liveness(True))
    payload = a2a._scheduled_delivery_payload(_Outcome(text="All green."))
    assert payload is not None  # not a repeat — the skipped card was never delivered
    assert await a2a._deliver_scheduled(payload) is True
    (card,) = _cards(_events)
    assert "card_fingerprint" not in card
    assert a2a._scheduled_delivery_payload(_Outcome(text="All green.")) is None  # a repeat now


def test_first_delivered_card_is_full_even_after_quiet_fires(_sched, _events):
    # The collapse flag counts DELIVERED cards, not fires: quiet early runs leave the
    # first visible card full; the next delivered card collapses to a chip.
    _sched(_Job(fire_count=5))
    _fire(text="NO_UPDATE")
    _fire(text="One review landed on #88.")
    _fire(text="Two reviews landed on #88.")
    assert [c["collapse"] for c in _cards(_events)] == [False, True]


def test_failed_runs_always_post_and_reset_the_repeat_guard(_sched, _events):
    # r3: identical consecutive failures all post; a recovery repeating the last good answer posts.
    _sched(_Job())
    for state, error in [("completed", ""), ("failed", "gateway 524"), ("failed", "gateway 524"), ("completed", "")]:
        _fire(state=state, text="All green." if state == "completed" else "", error=error)
    assert [c["status"] for c in _cards(_events)] == ["completed", "failed", "failed", "completed"]


def test_wait_resume_and_same_chat_suppression_unchanged(_sched):
    # r4: a wait resume / a one-shot that ran IN its chat still post no card, as before.
    _sched(_Job(id="wait:chat-42"))
    assert a2a._scheduled_delivery_payload(_Outcome(trigger="wait:chat-42")) is None
    _sched(_Job())
    assert a2a._scheduled_delivery_payload(_Outcome(context_id="chat-42")) is None


def test_tool_docstrings_mention_no_update():
    # r5: schedule_task tells a recurring run to end with NO_UPDATE — its fires post
    # result cards this guard can suppress. A watch reaction replies as a visible turn
    # in its own chat (origin `watch`; nothing strips the sentinel), so create_watch
    # documents the convention the other way around: it warns NOT to use it there.
    from tools.scheduler_tools import _build_scheduler_tools, _build_watch_tools

    tools = {t.name: t for t in _build_scheduler_tools(object()) + _build_watch_tools()}
    assert "NO_UPDATE" in tools["schedule_task"].description
    watch_doc = tools["create_watch"].description
    assert "NO_UPDATE" in watch_doc
    assert "do NOT tell" in watch_doc  # warns against the sentinel; never instructs it
