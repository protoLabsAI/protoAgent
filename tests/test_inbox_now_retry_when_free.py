"""Undelivered priority-now inbox items retry when the agent is free (bd-rbk7e).

Before this, ``recover_pending_now_inbox_items`` ran exactly once, at boot
(``_INBOX_NOW_RECOVERY_STARTED``), so a now page whose live delivery failed while the
agent was mid-turn sat pending until the next restart — surfaced only as a dismissable
"undelivered page" alert. Now the page is retried right after the last in-flight turn
settles (``turn_control._turn_ended`` → ``stores.kick_inbox_now_retry``) and by a
bounded periodic sweep, both gated on the REAL busy signal (the process-wide turn
bracket), with the existing claim/accept safety (exactly once). While waiting it is a
QUEUED row (``queued_since``/``attempts`` on /api/inbox), not an operator alert; only
``needs_human`` pages page anyone, and spent budgets leave a failed Activity row.

These tests use the real ``InboxStore`` and the real turn bracket — no fake stores, no
fake locks; only the delivery hook (the self-A2A seam) is substituted.
"""

from __future__ import annotations

import asyncio

import pytest

import operator_api.console_handlers as ch
import runtime.state as rs
from inbox.store import InboxStore
from server import stores, turn_control


def _store(tmp_path) -> InboxStore:
    return InboxStore(str(tmp_path / "inbox.db"))


@pytest.fixture
def inbox(tmp_path, monkeypatch) -> InboxStore:
    store = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", None, raising=False)
    # Belt and braces: never inherit a stuck single-flight latch from another test.
    monkeypatch.setattr(stores, "_INBOX_NOW_RETRY_ACTIVE", False)
    return store


async def _drain_retry_tasks() -> int:
    """Await every retry task the settle kick scheduled; returns how many there were."""
    tasks = list(stores._INBOX_NOW_RETRY_TASKS)
    if tasks:
        await asyncio.gather(*tasks)
    return len(tasks)


NONE = {"claimed": 0, "accepted": 0, "failed": 0}


@pytest.mark.asyncio
async def test_now_item_arriving_mid_turn_delivers_exactly_once_when_turn_settles(inbox, monkeypatch):
    """r1: live delivery fails while a real turn holds the busy lock; the settle kick
    delivers it automatically — no restart, no operator action, exactly once."""
    fires: list[int] = []

    async def _deliver(item):
        if turn_control.active_turns() > 0:
            return False  # the real failure mode: the target refuses while busy
        fires.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)

    turn_control._turn_started("chat-busy")
    try:
        res = await ch._operator_inbox_add({"text": "deploy failed — act", "priority": "now", "source": "ci"})
        assert res["fired"] is False
        assert fires == []
        [row] = inbox.list(priority_floor="now")  # queued, not delivered, not an alert
        assert row["delivered_at"] is None
        assert res["item"]["queued_since"] == row["created_at"]
    finally:
        turn_control._turn_ended("chat-busy")  # the turn settles → the kick fires

    assert await _drain_retry_tasks() == 1
    assert fires == [row["id"]]  # exactly once
    assert inbox.list(priority_floor="now") == []
    [done] = inbox.list(priority_floor="now", include_delivered=True)
    assert done["delivered_at"] is not None


@pytest.mark.asyncio
async def test_sweep_spends_nothing_while_busy_and_delivers_once_free(inbox, monkeypatch):
    """r2: with the target busy across several sweep ticks (a 3-minute turn), the sweep
    neither fires nor burns the attempt budget; the first free tick delivers."""
    item = inbox.add("busy-stretch page", priority="now")
    fires: list[int] = []

    async def _deliver(it):
        assert turn_control.active_turns() == 0, "a retry fired while the target was busy"
        fires.append(it["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)
    # Keep the settle kick out of this test: the SWEEP must do the delivery.
    monkeypatch.setattr(stores, "kick_inbox_now_retry", lambda: None)

    turn_control._turn_started("chat-long")
    try:
        for _ in range(3):  # three one-minute ticks of the bounded sweep
            assert await stores.retry_pending_now_inbox_items_when_free() == NONE
    finally:
        turn_control._turn_ended("chat-long")

    assert fires == []
    [row] = inbox.list(priority_floor="now")
    assert row["recovery_attempts"] == 0  # waiting out a busy stretch costs no attempts

    assert await stores.retry_pending_now_inbox_items_when_free() == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fires == [item["id"]]
    assert inbox.list(priority_floor="now") == []


@pytest.mark.asyncio
async def test_mid_pass_busy_skip_restores_claim_and_refunds_attempt(inbox, monkeypatch):
    """A turn that takes the agent between the idle gate and the fire hands the batch
    back untouched: claim released, attempt refunded, nothing fired."""
    item = inbox.add("raced page", priority="now")

    async def _must_not_fire(_it):
        raise AssertionError("a busy pass must not fire")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _must_not_fire, raising=False)

    result = await stores.recover_pending_now_inbox_items(limit=8, is_busy=lambda: True)

    assert result == {"claimed": 1, "accepted": 0, "failed": 0}
    [row] = inbox.list(priority_floor="now")
    assert row["id"] == item["id"]
    assert row["recovery_claimed_at"] is None  # back in the pull/retry queue
    assert row["recovery_attempts"] == 0  # the busy skip refunded the charged attempt
    assert "busy" in (row["recovery_error"] or "")


@pytest.mark.asyncio
async def test_api_rows_carry_queued_since_attempts_and_needs_human(inbox, monkeypatch):
    """r3 (server half): /api/inbox rows say how long a page has queued, how often it
    was retried, and whether it genuinely needs the operator."""

    async def _refuse(_it):
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _refuse, raising=False)

    queued = inbox.add("late page", priority="now")
    assert await stores.retry_pending_now_inbox_items_when_free() == {"claimed": 1, "accepted": 0, "failed": 1}

    flagged = await ch._operator_inbox_add(
        {"text": "board block — operator must decide", "priority": "now", "needs_human": True}
    )
    assert flagged["item"]["needs_human"] is True

    rows = {r["id"]: r for r in (await ch._operator_inbox_list("now", False))["items"]}
    assert rows[queued["id"]]["queued_since"] == queued["created_at"]
    assert rows[queued["id"]]["attempts"] == 1
    assert rows[queued["id"]]["needs_human"] is False
    assert rows[flagged["item"]["id"]]["needs_human"] is True

    one = await ch._operator_inbox_get(queued["id"])
    assert one["item"]["queued_since"] == queued["created_at"]
    assert one["item"]["attempts"] == 1

    # A delivered row is no longer queued.
    inbox.mark_delivered([queued["id"]])
    delivered = await ch._operator_inbox_get(queued["id"])
    assert delivered["item"]["queued_since"] is None


@pytest.mark.asyncio
async def test_exhausted_non_busy_failures_record_visible_activity_failure(tmp_path, inbox, monkeypatch):
    """After the attempt budget is spent on genuine (non-busy) failures, the item's
    Activity feed carries ONE visible failed row with the reason — and the item stays a
    queued inbox row (pull fallback still reads it); nothing pages the operator."""
    from activity import ActivityLog

    feed = ActivityLog(str(tmp_path / "activity.db"))
    monkeypatch.setattr(rs.STATE, "activity_log", feed, raising=False)
    item = inbox.add("refused page", priority="now", source="webhook:ci")

    async def _refuse(_it):
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _refuse, raising=False)

    for _ in range(2):
        assert await stores.retry_pending_now_inbox_items_when_free(max_attempts=2) == {
            "claimed": 1,
            "accepted": 0,
            "failed": 1,
        }
    # The budget is spent: later passes claim nothing (and record nothing twice).
    assert await stores.retry_pending_now_inbox_items_when_free(max_attempts=2) == NONE

    failed = [r for r in feed.recent() if r["state"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["origin"] == "inbox"
    assert failed[0]["trigger"] == "webhook:ci"
    assert "2 attempt(s)" in failed[0]["text"]
    assert "delivery was not accepted" in failed[0]["text"]
    assert failed[0]["stimulus"] == "refused page"

    [row] = inbox.list(priority_floor="now")  # still visible to check_inbox — never dropped
    assert row["id"] == item["id"]
    assert row["recovery_attempts"] == 2


def test_turn_ended_kick_is_safe_without_a_running_loop(inbox, monkeypatch):
    """The settle kick must never break the turn bracket — including on a bare thread
    with no event loop (the bracket is called from every driver)."""

    async def _deliver(_it):  # pragma: no cover — must not be reached without a loop
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)
    inbox.add("no-loop page", priority="now")

    turn_control._turn_started("chat-x")
    turn_control._turn_ended("chat-x")  # must not raise

    [row] = inbox.list(priority_floor="now")
    assert row["delivered_at"] is None  # still queued for the sweep/next kick
