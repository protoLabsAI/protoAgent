"""A scheduled result that posts a card also sends ONE typed notice (ADR 0135 D8/D11,
#5537): the notice rides the card's own post point, so a run that posts no card (empty,
``NO_UPDATE``, a repeat, a one-shot's stale chat) sends no notice either."""

from __future__ import annotations

import pytest

import server
from graph.plugins import chat_surface
from server import a2a
from tests.test_scheduled_delivery import _Job, _Outcome, _Scheduler


@pytest.fixture(autouse=True)
def _fresh_guard(monkeypatch):  # the guards' memory is module state — isolate per test
    monkeypatch.setattr(a2a, "_SCHEDULED_CARD_HASHES", {})
    monkeypatch.setattr(a2a, "_SCHEDULED_UNCHANGED_RUNS", {})
    monkeypatch.setattr(server.STATE, "scheduler", _Scheduler(_Job()))


@pytest.fixture
def cards(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        a2a._event_bus, "publish", lambda ev, d, **_kw: events.append(d) if ev == "scheduler.completed" else None
    )
    return events


@pytest.fixture
def notices(monkeypatch):
    # A live surface's dispatcher on the real seam: what an adapter would be handed.
    seen: list[chat_surface.ChatNotice] = []
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": seen.append})
    return seen


def _fire(**kw):
    a2a._handle_scheduled_delivery(_Outcome(**kw))  # no loop → the inline post path


def _liveness(value):
    async def _f(*_a, **_kw):
        return value

    return _f


def test_a_posted_card_sends_exactly_one_typed_notice_linking_its_chat(cards, notices):
    _fire(text="Swept the inbox: 3 new threads.")
    assert len(cards) == 1
    (notice,) = notices
    assert (notice.kind, notice.importance) == ("review", "normal")
    assert cards[0]["notice_id"] == notice.notice_id
    assert "job-1" in notice.text and "Swept the inbox: 3 new threads." in notice.text
    assert notice.link.endswith("/app/?session=chat-42")


@pytest.mark.parametrize("text", ["", "NO_UPDATE", "Checked CI, nothing merged.\n\nNO_UPDATE"])
def test_a_quiet_run_sends_no_notice(cards, notices, text):
    _fire(text=text)
    assert cards == [] and notices == []


def test_a_repeat_of_the_last_delivered_result_sends_no_notice(cards, notices):
    _fire(text="As of 09:10: CI green, nothing merged.")
    _fire(text="As of 11:48: CI green,  nothing merged.")
    assert len(cards) == 1 and len(notices) == 1
    _fire(text="One new review landed on #88.")  # news again → a second card, a second notice
    assert len(cards) == 2 and len(notices) == 2


def test_a_failed_run_posts_a_card_and_a_notice_naming_the_failure(cards, notices):
    _fire(state="failed", text="", error="Stopped while waiting for its thread.")
    assert len(cards) == 1
    (notice,) = notices
    assert "failed" in notice.text and "Stopped while waiting" in notice.text


@pytest.mark.parametrize("schedule, delivered", [("0 9 * * *", True), ("2026-11-20T09:00:00Z", False)])
async def test_idle_chat_gets_one_typed_notice_only_for_recurring_jobs(cards, notices, monkeypatch, schedule, delivered):
    # #5601: reading a daily brief without replying must not silence the next day's notice.
    monkeypatch.setattr(server.STATE, "scheduler", _Scheduler(_Job(schedule=schedule)))
    monkeypatch.setattr(a2a, "_session_recently_active", _liveness(False))
    assert await a2a._deliver_scheduled(a2a._scheduled_delivery_payload(_Outcome(text="All green."))) is delivered
    assert len(cards) == len(notices) == int(delivered)
    if delivered:
        assert (notices[0].kind, notices[0].importance) == ("review", "normal")
        assert cards[0]["notice_id"] == notices[0].notice_id
        assert notices[0].link.endswith("/app/?session=chat-42")


def test_a_notice_failure_never_loses_the_card_or_its_repeat_guard(cards, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("surface down")

    monkeypatch.setattr(chat_surface, "notify_chat_surfaces", _boom)
    _fire(text="All green.")
    assert len(cards) == 1
    assert a2a._scheduled_delivery_payload(_Outcome(text="All green.")) is None  # recorded as delivered
