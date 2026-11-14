"""Pending priority-``next`` inbox items reach an IDLE agent (#5216).

#5187 made an undelivered priority-``now`` page retry when the agent is free. A
priority-``next`` item still never reached an idle agent: it waited forever for a
``check_inbox`` pull that an idle agent never makes (one sat 2 h with ``delivered_at``
null while the agent idled, yet the same text at ``now`` ran within a minute). Now the
retry-when-free path (the turn-settle kick in ``turn_control._turn_ended`` plus the
periodic sweep) also delivers pending next items — in priority-then-age order, firing a
turn the same way a now page does — but ONLY when the agent is free, so a next item runs
AFTER the current work, never interrupting it.

These tests use the real ``InboxStore`` and the real turn bracket; only the delivery
hook (the self-A2A seam) is substituted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import operator_api.console_handlers as ch
import runtime.state as rs
from inbox.store import InboxStore
from server import stores, turn_control


@pytest.fixture
def inbox(tmp_path, monkeypatch) -> InboxStore:
    store = InboxStore(str(tmp_path / "inbox.db"))
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", None, raising=False)
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
async def test_idle_agent_runs_a_pending_next_item_on_the_retry_pass(inbox, monkeypatch):
    """r1: a next item added while the agent is idle is delivered (fires a turn) by the
    retry-when-free pass, exactly once, and is then marked delivered."""
    fires: list[tuple[int, str]] = []

    async def _deliver(item):
        assert turn_control.active_turns() == 0  # only ever fired while free
        fires.append((item["id"], item["priority"]))
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)
    item = inbox.add("summarise the overnight digest", priority="next")
    assert inbox.maybe_pending_fireable() is True  # the add armed the retry hint

    assert await stores.retry_pending_now_inbox_items_when_free() == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fires == [(item["id"], "next")]  # ran as a turn, like a now page
    assert inbox.list(priority_floor="next") == []
    [done] = inbox.list(priority_floor="next", include_delivered=True)
    assert done["delivered_at"] is not None


@pytest.mark.asyncio
async def test_next_item_added_mid_turn_runs_after_the_turn_not_during_it(inbox, monkeypatch):
    """r2: a next item posted during a running turn is NOT delivered while the turn holds
    the agent; the turn-settle kick delivers it once the turn ends."""
    fires: list[int] = []

    async def _deliver(item):
        assert turn_control.active_turns() == 0, "a next item ran during a turn"
        fires.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)

    turn_control._turn_started("chat-busy")
    try:
        res = await ch._operator_inbox_add({"text": "process this when free", "priority": "next", "source": "cron"})
        assert res["fired"] is False  # a next item never fires on add
        # A sweep tick that lands while the turn holds the agent spends nothing, fires nothing.
        assert await stores.retry_pending_now_inbox_items_when_free() == NONE
        assert fires == []
        [row] = inbox.list(priority_floor="next")
        assert row["delivered_at"] is None
    finally:
        turn_control._turn_ended("chat-busy")  # the turn settles → the kick fires

    assert await _drain_retry_tasks() == 1
    assert fires == [row["id"]]  # delivered after the turn, exactly once
    assert inbox.list(priority_floor="next") == []


@pytest.mark.asyncio
async def test_later_items_are_never_delivered_by_the_idle_pass(inbox, monkeypatch):
    """r3: a ``later`` item is background — the idle pass neither fires nor even claims it,
    while a next item beside it is delivered normally."""
    fires: list[int] = []

    async def _deliver(item):
        fires.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)
    later = inbox.add("background reading", priority="later")
    nxt = inbox.add("run me when free", priority="next")

    assert await stores.retry_pending_now_inbox_items_when_free() == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fires == [nxt["id"]]  # only the next item, never the later one

    still = inbox.get(later["id"])
    assert still["delivered_at"] is None  # left untouched
    assert still["recovery_claimed_at"] is None  # never even claimed


@pytest.mark.asyncio
async def test_idle_delivery_is_priority_then_age_order(inbox, monkeypatch):
    """A single pass delivers now before next, and oldest before newest within a tier."""
    order: list[tuple[str, int]] = []

    async def _deliver(item):
        order.append((item["priority"], item["id"]))
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _deliver, raising=False)
    # Anchor the rows to the real clock: the retry-when-free pass claims against
    # datetime.now(UTC), and the RECOVERY_MAX_AGE (24h) filter would drop a row fixed to a
    # far-past date. Keep all three inside the window so this stays a pure ordering check.
    t0 = datetime.now(UTC) - timedelta(hours=1)
    old_next = inbox.add("older next", priority="next", now=t0)
    new_next = inbox.add("newer next", priority="next", now=t0 + timedelta(minutes=5))
    page = inbox.add("now page", priority="now", now=t0 + timedelta(minutes=10))

    assert await stores.retry_pending_now_inbox_items_when_free() == {"claimed": 3, "accepted": 3, "failed": 0}
    assert order == [("now", page["id"]), ("next", old_next["id"]), ("next", new_next["id"])]


def test_a_claimed_next_item_is_hidden_from_the_check_inbox_pull(inbox):
    """Exactly once: while the idle path holds a next item mid-delivery (claimed, not yet
    marked delivered), a concurrent ``check_inbox`` pull must not re-offer it."""
    item = inbox.add("deliver only once", priority="next")
    [claimed] = inbox.claim_now_recovery_batch(priorities=("now", "next"))
    assert claimed["id"] == item["id"]
    assert inbox.list(priority_floor="next") == []  # the pull view hides the in-flight claim


def test_a_thread_item_is_never_claimed_by_the_idle_pass(inbox):
    """A direct message (``delivery='thread'``, stored ``next``) belongs to the live chat,
    not the idle fire path: it arms no retry hint and is never claimed here."""
    inbox.add("note for the operator's chat", source="peer", delivery="thread")
    assert inbox.maybe_pending_fireable() is False
    assert inbox.claim_now_recovery_batch(priorities=("now", "next")) == []
