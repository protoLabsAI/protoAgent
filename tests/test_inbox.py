"""Tests for the inbound inbox: store, storm guard, tool, route (ADR 0003)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inbox.store import InboxStore, StormGuard
from operator_api.routes import register_operator_routes


# ── InboxStore ───────────────────────────────────────────────────────────────


def _store(tmp_path):
    return InboxStore(str(tmp_path / "inbox.db"))


def test_add_and_list_roundtrip(tmp_path):
    s = _store(tmp_path)
    item = s.add("hello", priority="next", source="webhook")
    assert item["text"] == "hello" and item["priority"] == "next" and item["source"] == "webhook"
    rows = s.list(priority_floor="next")
    assert [r["text"] for r in rows] == ["hello"]


def test_priority_floor_filters_tiers(tmp_path):
    s = _store(tmp_path)
    s.add("n", priority="now")
    s.add("x", priority="next")
    s.add("l", priority="later")
    assert {r["text"] for r in s.list(priority_floor="now")} == {"n"}
    assert {r["text"] for r in s.list(priority_floor="next")} == {"n", "x"}
    assert {r["text"] for r in s.list(priority_floor="later")} == {"n", "x", "l"}


def test_list_orders_now_before_next(tmp_path):
    s = _store(tmp_path)
    s.add("later-added-next", priority="next")
    s.add("earlier-added-now", priority="now")
    rows = s.list(priority_floor="later")
    assert rows[0]["priority"] == "now"  # now sorts ahead regardless of insert order


def test_dedup_within_window(tmp_path):
    s = _store(tmp_path)
    first = s.add("dup", dedup_key="k1")
    again = s.add("dup", dedup_key="k1")
    assert first is not None
    assert again is None  # deduped
    # A different key is not deduped.
    assert s.add("dup", dedup_key="k2") is not None


def test_dedup_expires_after_window(tmp_path):
    s = InboxStore(str(tmp_path / "inbox.db"), dedup_window_s=60)
    old = datetime.now(UTC) - timedelta(seconds=120)
    s.add("dup", dedup_key="k1", now=old)  # outside the window now
    assert s.add("dup", dedup_key="k1") is not None  # not deduped against the stale row


def test_mark_delivered_removes_from_pending(tmp_path):
    s = _store(tmp_path)
    a = s.add("a", priority="next")
    s.add("b", priority="next")
    assert s.pending_count() == 2
    assert s.mark_delivered([a["id"]]) == 1
    assert s.pending_count() == 1
    assert s.mark_delivered([a["id"]]) == 0  # already delivered


def test_mark_pending_restores_to_queue(tmp_path):
    """Un-deliver puts an item back in the pending queue (restore-on-failed-fire, #1375)."""
    s = _store(tmp_path)
    a = s.add("a", priority="now")
    assert s.mark_delivered([a["id"]]) == 1
    assert s.list(priority_floor="later") == []  # delivered → out of the queue
    assert s.mark_pending([a["id"]]) == 1
    assert len(s.list(priority_floor="later")) == 1  # back in the queue


def test_add_rejects_empty_and_bad_priority(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.add("   ")
    with pytest.raises(ValueError):
        s.add("hi", priority="urgent")


# ── InboxStore.prune ─────────────────────────────────────────────────────────


def test_prune_inbox_removes_old_delivered_only(tmp_path):
    """Only delivered items older than keep_days are removed; pending items
    (undelivered) survive regardless of age."""
    s = _store(tmp_path)
    old = datetime(2024, 1, 1, tzinfo=UTC)
    now = datetime(2024, 3, 1, tzinfo=UTC)
    # Old delivered item — should be pruned.
    item_old = s.add("old delivered", priority="next", now=old)
    s.mark_delivered([item_old["id"]], now=old)
    # Old pending item — should survive (never prune pending).
    s.add("old pending", priority="next", now=old)
    removed = s.prune(keep_days=30, now=now)
    assert removed == 1
    remaining = s.list(priority_floor="later", include_delivered=True)
    assert len(remaining) == 1
    assert remaining[0]["text"] == "old pending"


def test_prune_inbox_keeps_recent_delivered(tmp_path):
    """A recently delivered item within keep_days survives pruning."""
    s = _store(tmp_path)
    now = datetime(2024, 3, 1, tzinfo=UTC)
    recent = now - timedelta(days=10)
    item = s.add("recent delivered", priority="next", now=recent)
    s.mark_delivered([item["id"]], now=recent)
    removed = s.prune(keep_days=30, now=now)
    assert removed == 0
    remaining = s.list(priority_floor="later", include_delivered=True)
    assert len(remaining) == 1


def test_prune_inbox_keep_all_zero(tmp_path):
    """keep_days=0 means keep forever — no rows are removed."""
    s = _store(tmp_path)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    item = s.add("ancient", priority="next", now=old)
    s.mark_delivered([item["id"]], now=old)
    removed = s.prune(keep_days=0, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert removed == 0
    assert len(s.list(priority_floor="later", include_delivered=True)) == 1


# ── StormGuard ───────────────────────────────────────────────────────────────


def test_storm_guard_caps_then_recovers():
    g = StormGuard(max_fires=3, window_s=10.0)
    assert [g.allow(t) for t in (0.0, 0.1, 0.2)] == [True, True, True]
    assert g.allow(0.3) is False  # 4th within window suppressed
    # After the window passes, the old fires expire and it allows again.
    assert g.allow(11.0) is True


# ── now-item A2A acceptance ─────────────────────────────────────────────────


def test_a2a_send_acceptance_requires_owned_task():
    import server.a2a as a2a
    from runtime.state import STATE

    a2a.install_inbox_now_delivery()
    assert STATE.inbox_now_delivery is a2a._fire_activity_from_inbox
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_SUBMITTED"}}}) is True
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_WORKING"}}}) is True
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_INPUT_REQUIRED"}}}) is True
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}) is True
    assert a2a._a2a_send_accepted({"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}}}) is True
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_FAILED"}}}) is False
    assert a2a._a2a_send_accepted({"result": {"status": {"state": "TASK_STATE_AUTH_REQUIRED"}}}) is False
    assert a2a._a2a_send_accepted({"error": {"code": -32603, "message": "boom"}}) is False
    assert a2a._a2a_send_accepted({"result": {"text": "not a task"}}) is False


@pytest.mark.asyncio
async def test_now_inbox_fire_uses_mounted_a2a_app_without_loopback_port(monkeypatch):
    import server.a2a as a2a
    from events import ACTIVITY_CONTEXT
    from runtime.state import STATE

    app = FastAPI()
    captured: dict = {}

    @app.post("/a2a")
    async def _a2a(body: dict):
        captured.update(body)
        return {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}

    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)
    monkeypatch.setattr(STATE, "active_port", 9, raising=False)

    ok = await a2a._fire_activity_from_inbox(
        {"id": 17, "text": "ship the priority page", "priority": "now", "source": "test"}
    )

    assert ok is True
    assert captured["method"] == "SendMessage"
    msg = captured["params"]["message"]
    # ADR 0129: the item executes on its own isolated job session, never the shared
    # Activity thread (which is a summary/feed surface). Item provenance is unchanged.
    assert msg["contextId"].startswith("job:")
    assert msg["contextId"] != ACTIVITY_CONTEXT
    assert msg["parts"] == [{"text": "ship the priority page"}]
    assert msg["metadata"]["origin"] == "inbox"
    assert msg["metadata"]["inbox_id"] == 17
    assert msg["metadata"]["priority"] == "now"


@pytest.mark.asyncio
async def test_now_inbox_fire_rejects_failed_a2a_response(monkeypatch):
    import server.a2a as a2a
    from runtime.state import STATE

    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(_body: dict):
        return {"result": {"status": {"state": "TASK_STATE_FAILED"}}}

    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)

    ok = await a2a._fire_activity_from_inbox({"id": 18, "text": "not yet", "priority": "now"})

    assert ok is False


@pytest.mark.asyncio
async def test_now_inbox_fire_accepts_a2a_task_ownership_before_terminal(monkeypatch):
    import server.a2a as a2a
    from runtime.state import STATE

    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(_body: dict):
        return {"result": {"status": {"state": "TASK_STATE_SUBMITTED"}}}

    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)

    ok = await a2a._fire_activity_from_inbox({"id": 19, "text": "accepted", "priority": "now"})

    assert ok is True


@pytest.mark.asyncio
async def test_now_inbox_fire_rejects_jsonrpc_error(monkeypatch):
    import server.a2a as a2a
    from runtime.state import STATE

    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(_body: dict):
        return {"error": {"code": -32603, "message": "boom"}}

    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)

    ok = await a2a._fire_activity_from_inbox({"id": 20, "text": "noisy response", "priority": "now"})

    assert ok is False


# ── late-delivery age note (#5241) ───────────────────────────────────────────


def _inbox_capture_app(captured: dict) -> FastAPI:
    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(body: dict):
        captured.update(body)
        return {"result": {"status": {"state": "TASK_STATE_SUBMITTED"}}}

    return app


def test_inbox_late_delivery_notice_helper():
    """The pure age-formatting helper: a stale item gets a one-line note; a fresh item,
    a missing stamp and an unparseable stamp all return None (unchanged delivery)."""
    import server.a2a as a2a

    now = datetime(2026, 11, 13, 16, 49, tzinfo=UTC)
    queued = (now - timedelta(hours=32)).isoformat()
    notice = a2a._inbox_late_delivery_notice(queued, now=now)
    assert notice == (
        "[Queued 2026-11-12 08:49 UTC — delivered 32h late. "
        "Newer instructions may replace it; check before acting.]"
    )

    # Within the one-hour threshold → unchanged.
    assert a2a._inbox_late_delivery_notice((now - timedelta(minutes=30)).isoformat(), now=now) is None
    # Exactly at the threshold is NOT "older than" → unchanged.
    assert a2a._inbox_late_delivery_notice((now - timedelta(hours=1)).isoformat(), now=now) is None
    # A naive (tz-less) stamp is read as UTC, not crashed on.
    assert a2a._inbox_late_delivery_notice("2026-11-12T08:49:00", now=now).startswith("[Queued 2026-11-12 08:49 UTC")
    # Missing / unparseable → None, never raises.
    assert a2a._inbox_late_delivery_notice(None, now=now) is None
    assert a2a._inbox_late_delivery_notice("", now=now) is None
    assert a2a._inbox_late_delivery_notice("not-a-timestamp", now=now) is None
    assert a2a._inbox_late_delivery_notice(1762937340, now=now) is None


@pytest.mark.asyncio
async def test_now_inbox_fire_prefixes_a_stale_item_and_keeps_identity(tmp_path, monkeypatch):
    """A page queued well over the threshold is delivered with a leading age note; the
    original text follows unchanged after a blank line, metadata carries created_at, and
    the prefix leaves the message identity (messageId/contextId) untouched."""
    import server.a2a as a2a
    from runtime.state import STATE

    store = InboxStore(str(tmp_path / "inbox.db"))
    item = store.add(
        "cancel the nightly backup schedule",
        priority="now",
        source="ops",
        now=datetime.now(UTC) - timedelta(hours=32),
    )
    captured: dict = {}
    monkeypatch.setattr(STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(STATE, "fastapi_app", _inbox_capture_app(captured), raising=False)
    monkeypatch.setattr(STATE, "active_port", 9, raising=False)

    ok = await a2a._fire_activity_from_inbox(item)

    assert ok is True
    msg = captured["params"]["message"]
    text = msg["parts"][0]["text"]
    first_line, blank, rest = text.partition("\n\n")
    assert first_line.startswith("[Queued ")
    assert "UTC" in first_line and "late" in first_line
    assert "Newer instructions may replace it" in first_line
    assert blank == "\n\n"
    # The original text follows byte-for-byte.
    assert rest == "cancel the nightly backup schedule"
    assert msg["metadata"]["created_at"] == item["created_at"]
    # The prefix is cosmetic: identity is the durable one the store assigned.
    identity = store.execution_identity(item["id"])
    assert msg["messageId"] == identity["message_id"]
    assert msg["contextId"] == identity["context_id"]


@pytest.mark.asyncio
async def test_now_inbox_fire_leaves_a_fresh_item_unchanged(tmp_path, monkeypatch):
    """An item within the threshold is delivered byte-for-byte unchanged, but still
    carries created_at in its metadata."""
    import server.a2a as a2a
    from runtime.state import STATE

    store = InboxStore(str(tmp_path / "inbox.db"))
    item = store.add("fresh page", priority="now", source="ops")
    captured: dict = {}
    monkeypatch.setattr(STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(STATE, "fastapi_app", _inbox_capture_app(captured), raising=False)
    monkeypatch.setattr(STATE, "active_port", 9, raising=False)

    ok = await a2a._fire_activity_from_inbox(item)

    assert ok is True
    msg = captured["params"]["message"]
    assert msg["parts"] == [{"text": "fresh page"}]
    assert msg["metadata"]["created_at"] == item["created_at"]


@pytest.mark.asyncio
async def test_now_inbox_fire_without_created_at_is_unchanged(monkeypatch):
    """A missing or unparseable created_at delivers the text unchanged and does not raise;
    metadata still carries the (None / raw) created_at value for every item."""
    import server.a2a as a2a
    from runtime.state import STATE

    captured: dict = {}
    monkeypatch.setattr(STATE, "inbox_store", None, raising=False)
    monkeypatch.setattr(STATE, "fastapi_app", _inbox_capture_app(captured), raising=False)
    monkeypatch.setattr(STATE, "active_port", 9, raising=False)

    # No created_at at all.
    ok = await a2a._fire_activity_from_inbox({"id": 71, "text": "no stamp here", "priority": "now"})
    assert ok is True
    msg = captured["params"]["message"]
    assert msg["parts"] == [{"text": "no stamp here"}]
    assert msg["metadata"]["created_at"] is None

    # An unparseable stamp is also delivered unchanged.
    captured.clear()
    ok = await a2a._fire_activity_from_inbox(
        {"id": 72, "text": "garbage stamp", "priority": "now", "created_at": "not-a-timestamp"}
    )
    assert ok is True
    msg = captured["params"]["message"]
    assert msg["parts"] == [{"text": "garbage stamp"}]
    assert msg["metadata"]["created_at"] == "not-a-timestamp"


# ── check_inbox tool ─────────────────────────────────────────────────────────


def test_check_inbox_tool_returns_and_marks_delivered(tmp_path):
    from tools.lg_tools import _build_inbox_tools

    s = _store(tmp_path)
    s.add("ping one", priority="next", source="webhook")
    s.add("ping two", priority="now")
    check_inbox = _build_inbox_tools(s)[0]

    out = asyncio.run(check_inbox.ainvoke({"priority_floor": "next", "limit": 10}))
    assert "ping one" in out and "ping two" in out
    assert "(from webhook)" in out
    # Delivered items don't come back a second time.
    assert asyncio.run(check_inbox.ainvoke({"priority_floor": "next"})) == "Inbox empty."


# ── now-item fire marks delivered (bd-jus) ───────────────────────────────────


@pytest.mark.asyncio
async def test_fired_now_item_is_marked_delivered(tmp_path, monkeypatch):
    """A now-item whose Activity turn fired must be marked delivered, not left
    pending to be re-surfaced (and re-acted-on) by the next check_inbox."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    store = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_ok(_item):
        return True

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

    res = await ch._operator_inbox_add({"text": "bg done", "priority": "now", "source": "background"})
    assert res["fired"] is True
    assert store.list(priority_floor="later") == []  # delivered → nothing pending


@pytest.mark.asyncio
async def test_accepted_now_item_does_not_require_or_survive_manual_check_inbox(tmp_path, monkeypatch):
    """Accepted event delivery is the owning turn; check_inbox must not see the same trigger."""
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from tools.lg_tools import _build_inbox_tools

    store = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[str] = []

    async def _fire_ok(item):
        fired.append(item["text"])
        return True

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

    res = await ch._operator_inbox_add({"text": "new now page", "priority": "now"})
    check_inbox = _build_inbox_tools(store)[0]

    assert res["fired"] is True
    assert fired == ["new now page"]
    assert await check_inbox.ainvoke({"priority_floor": "later"}) == "Inbox empty."


@pytest.mark.asyncio
async def test_failed_now_fire_stays_pending(tmp_path, monkeypatch):
    """A now-item whose fire FAILED stays pending so check_inbox is the fallback."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    store = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_fail(_item):
        return False

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_fail)

    res = await ch._operator_inbox_add({"text": "bg done", "priority": "now"})
    assert res["fired"] is False
    assert len(store.list(priority_floor="later")) == 1  # restored to pending for check_inbox

    from tools.lg_tools import _build_inbox_tools

    check_inbox = _build_inbox_tools(store)[0]
    assert "bg done" in await check_inbox.ainvoke({"priority_floor": "later"})


# ── startup recovery for pre-existing now backlog (#3351) ───────────────────


def test_claim_now_recovery_batch_reserves_bounded_and_is_idempotent(tmp_path):
    """The claim atomically stamps a bounded in-flight marker on now items, so a
    second claim from the same process does not re-fire the same rows."""
    store = _store(tmp_path)
    a = store.add("now a", priority="now")
    b = store.add("now b", priority="now")
    c = store.add("now c", priority="now")
    store.add("next d", priority="next")  # excluded: not now-priority

    claimed = store.claim_now_recovery_batch(limit=2, retry_after_s=3600)
    assert [r["id"] for r in claimed] == [a["id"], b["id"]]  # bounded to the batch limit
    # Claimed rows drop out of the pending pool: no concurrent pull double-delivery.
    assert [r["id"] for r in store.list(priority_floor="now")] == [c["id"]]
    reserved = store.list(priority_floor="now", include_delivered=True)
    for row in reserved:
        if row["id"] in {a["id"], b["id"]}:
            assert row["delivered_at"] is None
            assert row["recovery_claimed_at"] is not None
            assert row["recovery_attempted_at"] is not None
    # A repeat claim returns only the still-unclaimed item.
    again = store.claim_now_recovery_batch(limit=2, retry_after_s=3600)
    assert [r["id"] for r in again] == [c["id"]]


def test_claim_now_recovery_batch_skips_items_older_than_max_age(tmp_path):
    """A pending now item older than max_age (24h) is NOT claimed to fire a turn of its
    own; a younger one still is (#5256)."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    old = store.add("stale page", priority="now", now=now - timedelta(hours=48))
    recent = store.add("fresh page", priority="now", now=now - timedelta(hours=1))

    claimed = store.claim_now_recovery_batch(limit=8, now=now)

    assert [r["id"] for r in claimed] == [recent["id"]]  # the stale one is left behind
    # Boundary: an item exactly at the 24h edge is still inside the window and claimed.
    _store_edge = _store(tmp_path / "edge")
    edge = _store_edge.add("edge page", priority="now", now=now - timedelta(hours=24))
    assert [r["id"] for r in _store_edge.claim_now_recovery_batch(limit=8, now=now)] == [edge["id"]]
    assert old is not None  # (kept for the stale-visibility test below)


def test_claim_now_recovery_batch_claims_stale_accepted_completion(tmp_path):
    """A stale item carrying a durable recovery_accepted_at is still claimed — the
    completion path fires nothing and must never be stranded by the age filter."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    item = store.add("accepted but unmarked", priority="now", now=now - timedelta(hours=48))
    # Record accepted-but-unmarked delivery: this stamps recovery_accepted_at.
    assert store.record_recovery_mark_delivered_failure(item["id"], "mark failed") == 1

    claimed = store.claim_now_recovery_batch(limit=8, now=now)

    assert [r["id"] for r in claimed] == [item["id"]]
    assert claimed[0]["recovery_accepted_at"] is not None


def test_stale_unclaimed_item_stays_visible_to_pull_fallback(tmp_path):
    """A stale item the age filter skips stays pending and undelivered, so the
    check_inbox pull (store.list) still returns it — in one call, no waking turn each."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    old = store.add("stale page", priority="now", now=now - timedelta(hours=48))

    claimed = store.claim_now_recovery_batch(limit=8, now=now)
    assert claimed == []  # nothing fired

    [pending] = store.list(priority_floor="now")
    assert pending["id"] == old["id"]
    assert pending["delivered_at"] is None
    assert pending["recovery_claimed_at"] is None
    assert store.count_stale_recovery_items(now=now) == 1


def test_claim_now_recovery_batch_max_age_none_claims_stale(tmp_path):
    """max_age=None restores the age-blind first-slice behaviour: a stale item is claimed."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    old = store.add("stale page", priority="now", now=now - timedelta(hours=48))

    claimed = store.claim_now_recovery_batch(limit=8, max_age=None, now=now)

    assert [r["id"] for r in claimed] == [old["id"]]
    # And nothing is "stale" when the ceiling is disabled.
    assert store.count_stale_recovery_items(max_age=None, now=now) == 0


def test_restore_recovery_failure_reopens_pending_with_evidence(tmp_path):
    """Restoring an unfired claim clears ``recovery_claimed_at`` (pull fallback works)
    while keeping ``recovery_attempted_at`` (restart backoff) and recording evidence."""
    store = _store(tmp_path)
    item = store.add("boom", priority="now")
    [claimed_item] = store.claim_now_recovery_batch(limit=8)
    assert claimed_item["id"] == item["id"]
    assert store.list(priority_floor="now") == []  # claimed out of the pending pool

    assert store.restore_recovery_failure(item["id"], "delivery was not accepted") == 1

    [pending] = store.list(priority_floor="now")
    assert pending["id"] == item["id"]
    assert pending["delivered_at"] is None
    assert pending["recovery_claimed_at"] is None
    assert pending["recovery_attempted_at"]
    assert pending["recovery_error"] == "delivery was not accepted"


def test_restore_recovery_failure_does_not_reopen_a_concurrently_delivered_item(tmp_path):
    """A *late* recovery failure must not un-deliver a page already delivered by
    another owner."""
    store = _store(tmp_path)
    item = store.add("delivered mid-flight", priority="now")
    [claimed] = store.claim_now_recovery_batch(limit=8)

    # A concurrent consumer delivers the page while recovery is still awaiting its turn.
    assert store.mark_delivered([item["id"]]) == 1

    # The straggling recovery now reports failure — scoped to its own claim, it is a no-op.
    assert (
        store.restore_recovery_failure(
            item["id"], "delivery was not accepted", claimed_at=claimed["recovery_claimed_at"]
        )
        == 0
    )

    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["delivered_at"] is not None  # stays delivered — not reopened
    assert store.list(priority_floor="now") == []  # not resurrected into the pending pool


def test_recovery_claim_does_not_expire_into_pull_fallback(tmp_path):
    """A recovery claim remains hidden from normal inbox pulls even after the retry
    window; stale leases are recovered by the startup recovery owner, not check_inbox."""
    store = _store(tmp_path)
    store.add("claimed", priority="now")

    stale = datetime.now(UTC) - timedelta(seconds=4000)
    store.claim_now_recovery_batch(limit=8, retry_after_s=3600, now=stale)

    assert store.list(priority_floor="now") == []


def test_startup_claim_reset_reclaims_crash_lease_without_retry_window(tmp_path):
    """Regression (review): if a process exits after committing a recovery claim but
    before delivery/restore, startup must release the in-process lease immediately
    instead of stranding the page behind a wall-clock retry window."""
    store = _store(tmp_path)
    item = store.add("re-claimed after crash", priority="now")

    stale = datetime(2026, 1, 1, tzinfo=UTC)
    [first] = store.claim_now_recovery_batch(limit=8, retry_after_s=3600, now=stale)
    assert (
        store.claim_now_recovery_batch(
            limit=8,
            retry_after_s=3600,
            now=stale + timedelta(seconds=120),
        )
        == []
    )
    assert store.reset_stale_recovery_claims() == 1

    [second] = store.claim_now_recovery_batch(
        limit=8,
        retry_after_s=3600,
        now=stale + timedelta(seconds=121),
    )
    assert second["id"] == item["id"]
    assert second["recovery_claimed_at"] != first["recovery_claimed_at"]
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is None
    assert row["recovery_claimed_at"] == second["recovery_claimed_at"]


@pytest.mark.asyncio
async def test_startup_recovery_retries_stale_claim_left_by_crashed_recovery(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("crashed after claim", priority="now")
    stale = datetime(2026, 1, 1, tzinfo=UTC)
    store.claim_now_recovery_batch(limit=8, retry_after_s=3600, now=stale)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(fired_item):
        fired.append(fired_item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    recovered = await recover_pending_now_inbox_items(limit=8, retry_after_s=10 * 365 * 24 * 60 * 60)

    assert recovered == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [item["id"]]
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is not None
    assert row["recovery_claimed_at"] is None


@pytest.mark.asyncio
async def test_startup_recovery_retries_expired_claim_left_by_crashed_recovery(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("expired crash claim", priority="now")
    stale = datetime(2026, 1, 1, tzinfo=UTC)
    store.claim_now_recovery_batch(limit=8, retry_after_s=3600, now=stale)
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(fired_item):
        fired.append(fired_item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    recovered = await recover_pending_now_inbox_items(
        limit=8,
        retry_after_s=3600,
        now=stale + timedelta(seconds=3601),
    )

    assert recovered == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [item["id"]]
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is not None


def test_recovery_claimed_items_are_operator_visible_when_requested(tmp_path):
    """Active recovery claims stay out of agent pulls, but operator views can include
    them so undelivered recovery evidence is inspectable."""
    store = _store(tmp_path)
    item = store.add("inspect me", priority="now")

    [claimed] = store.claim_now_recovery_batch(limit=8)

    assert store.list(priority_floor="now") == []
    [visible] = store.list(priority_floor="now", include_recovery_claimed=True)
    assert visible["id"] == item["id"]
    assert visible["delivered_at"] is None
    assert visible["recovery_claimed_at"] == claimed["recovery_claimed_at"]


@pytest.mark.asyncio
async def test_startup_recovery_does_not_reopen_a_concurrently_delivered_item(tmp_path, monkeypatch):
    """End-to-end: when recovery reports 'not accepted' after another owner delivered
    the page, the driver must leave the item delivered rather than un-delivering it."""
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("delivered mid-flight", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_then_deliver_concurrently(fired_item):
        # Simulate another owner delivering the page mid-fire, then this recovery
        # failing to be accepted.
        store.mark_delivered([fired_item["id"]])
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_then_deliver_concurrently, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is not None  # stays delivered — not reopened by the late failure
    assert store.list(priority_floor="now") == []  # not resurrected into the pending pool


@pytest.mark.asyncio
async def test_startup_recovery_accepted_but_mark_delivered_failure_stays_claimed(tmp_path, monkeypatch):
    """Regression (review): once delivery is accepted, a mark-delivered failure must
    not clear the recovery claim and make the accepted page eligible for redelivery."""
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("accepted but unmarked", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_ok(_item):
        return True

    def _mark_delivered_raises(_ids):
        raise RuntimeError("sqlite write failed")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)
    monkeypatch.setattr(store, "mark_delivered", _mark_delivered_raises)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    assert store.list(priority_floor="now") == []  # still withheld from pull fallback
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is None
    assert row["recovery_claimed_at"] is not None
    # The durable accepted-delivery marker (#3351 review): survives a restart's claim reset,
    # so this recorded mark-failure is completed — not re-fired — on the next pass.
    assert row["recovery_accepted_at"] is not None
    assert row["recovery_error"] == "accepted delivery could not be marked delivered: sqlite write failed"

    listed = await ch._operator_inbox_list("now", include_delivered=False)
    assert [visible["id"] for visible in listed["items"]] == [item["id"]]
    assert listed["items"][0]["recovery_error"] == row["recovery_error"]


@pytest.mark.asyncio
async def test_startup_recovery_completes_accepted_page_after_restart_without_refiring(tmp_path, monkeypatch):
    """Regression (#3351 review, blocking): an Activity delivery that was accepted but whose
    ``mark_delivered`` write failed must NOT be delivered again after a restart. The durable
    ``recovery_accepted_at`` marker makes the next recovery pass COMPLETE the page (mark it
    delivered) instead of re-firing a duplicate turn."""
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("accepted but unmarked", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    fires: list[int] = []

    async def _fire_ok(delivered):
        fires.append(delivered["id"])
        return True

    def _mark_delivered_raises(_ids):
        raise RuntimeError("sqlite write failed")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)
    monkeypatch.setattr(store, "mark_delivered", _mark_delivered_raises)

    # Pass 1: delivery is accepted (fired once), but the mark write fails.
    first = await recover_pending_now_inbox_items(limit=8)
    assert first == {"claimed": 1, "accepted": 0, "failed": 1}
    assert fires == [item["id"]]
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["delivered_at"] is None and row["recovery_accepted_at"] is not None

    # A restart clears every in-flight claim — the durable accepted marker persists.
    assert store.reset_stale_recovery_claims() == 1

    # Pass 2: mark_delivered now works, and the delivery hook must NOT be called again.
    monkeypatch.undo()
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_must_not_run(_delivered):
        raise AssertionError("an accepted-but-unmarked page must be completed, not re-fired")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_must_not_run, raising=False)

    second = await recover_pending_now_inbox_items(limit=8)
    assert second == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fires == [item["id"]]  # never fired a second time
    [done] = store.list(priority_floor="now", include_delivered=True)
    assert done["delivered_at"] is not None
    assert store.list(priority_floor="now") == []  # nothing left pending


def test_accepted_but_unmarked_now_page_is_hidden_and_claimable_for_completion(tmp_path):
    """Store-level: after an accepted delivery whose mark failed, the page is hidden from the
    pull fallback (a check_inbox pull must not re-fire it) yet remains claimable for
    completion even once the attempt budget is spent — completing it fires no turn, so it can
    never be stranded by ``max_attempts`` nor cause a replay storm."""
    store = InboxStore(str(tmp_path / "inbox.db"))
    item = store.add("accepted but unmarked", priority="now")
    [claimed] = store.claim_now_recovery_batch(limit=8, max_attempts=1)  # attempt budget now spent
    store.record_recovery_mark_delivered_failure(
        item["id"], "accepted delivery could not be marked delivered", claimed_at=claimed["recovery_claimed_at"]
    )

    # A durable accepted marker is set and the row is withheld from the pull fallback…
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["recovery_accepted_at"] is not None and row["delivered_at"] is None
    assert store.list(priority_floor="now") == []

    # …but a restart's claim reset does not lose it: recovery reclaims it for completion
    # despite max_attempts being exhausted, and the claimed row carries the accepted marker.
    assert store.reset_stale_recovery_claims() == 1
    [reclaimed] = store.claim_now_recovery_batch(limit=8, max_attempts=1)
    assert reclaimed["id"] == item["id"]
    assert reclaimed["recovery_accepted_at"] is not None


@pytest.mark.asyncio
async def test_startup_recovery_accepts_preexisting_now_backlog(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    old_now = store.add("before repair", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(item):
        fired.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [old_now["id"]]
    assert store.list(priority_floor="later") == []


@pytest.mark.asyncio
async def test_startup_recovery_leaves_stale_backlog_for_check_inbox_and_logs_once(tmp_path, monkeypatch, caplog):
    """A pending now item older than 24h fires no self-A2A turn: it is left pending for
    the check_inbox pull, and the pass logs the stale count once (#5256)."""
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    now = datetime.now(UTC)
    stale = store.add("ancient pr-reviewer alarm", priority="now", now=now - timedelta(days=73))
    fresh = store.add("recent page", priority="now", now=now - timedelta(hours=1))
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(item):
        fired.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    with caplog.at_level(logging.INFO, logger="protoagent.server"):
        result = await recover_pending_now_inbox_items(limit=8, now=now)

    # Only the fresh page fired a turn; the 73-day-old page was never delivered.
    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [fresh["id"]]
    # The stale page stays pending and is still returned by the pull fallback (store.list).
    [pending] = store.list(priority_floor="now")
    assert pending["id"] == stale["id"]
    assert pending["delivered_at"] is None
    # The stale count is logged exactly once this pass.
    stale_logs = [r for r in caplog.records if "left 1 item(s) older than 24h for check_inbox" in r.getMessage()]
    assert len(stale_logs) == 1


@pytest.mark.asyncio
async def test_startup_recovery_uses_registered_accepted_delivery_hook(tmp_path, monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("use server hook", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _server_delivery(delivered_item):
        fired.append(delivered_item["id"])
        return True

    async def _console_wrapper_should_not_run(_item):
        raise AssertionError("startup recovery must use STATE.inbox_now_delivery directly")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _server_delivery, raising=False)
    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _console_wrapper_should_not_run)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [item["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_reserves_delivery_during_fire_to_block_double_delivery(tmp_path, monkeypatch):
    """The claim stamps ``recovery_claimed_at`` before the awaited fire, so a
    concurrent inbox consumer (a ``check_inbox`` pull or a fresh now-post) can't
    grab the same still-pending page and deliver it twice. On acceptance only
    then marks it delivered."""
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    old_now = store.add("reserve before fire", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    during_fire: list[tuple[list[int], str | None, str | None, str | None]] = []

    async def _fire_ok(item):
        # A concurrent pending consumer sees NOTHING while we deliver: the row has an
        # active recovery claim, so it cannot be picked up and double-delivered.
        pending_now = [r["id"] for r in store.list(priority_floor="now")]
        [reserved] = store.list(priority_floor="now", include_delivered=True)
        during_fire.append(
            (
                pending_now,
                reserved["delivered_at"],
                reserved["recovery_claimed_at"],
                reserved["recovery_attempted_at"],
            )
        )
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)
    [row] = store.list(priority_floor="now", include_delivered=True)

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert len(during_fire) == 1
    pending_now, reserved_delivered, reserved_claimed, reserved_attempted = during_fire[0]
    assert pending_now == []  # nothing left for a concurrent consumer to grab
    assert reserved_delivered is None  # not falsely delivered before acceptance
    assert reserved_claimed is not None
    assert reserved_attempted is not None
    # After acceptance the item is explicitly marked delivered; the claim and error clear.
    assert row["id"] == old_now["id"]
    assert row["delivered_at"]
    assert row["recovery_claimed_at"] is None
    assert row["recovery_error"] is None
    assert store.list(priority_floor="now") == []


@pytest.mark.asyncio
async def test_startup_recovery_is_bounded(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    items = [store.add(f"now {i}", priority="now") for i in range(3)]
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(item):
        fired.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    result = await recover_pending_now_inbox_items(limit=2)

    assert result == {"claimed": 2, "accepted": 2, "failed": 0}
    assert fired == [items[0]["id"], items[1]["id"]]
    assert [row["id"] for row in store.list(priority_floor="now")] == [items[2]["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_uses_storm_guarded_delivery_path(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    first = store.add("allowed now", priority="now")
    second = store.add("storm held", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", StormGuard(max_fires=1, window_s=60.0), raising=False)
    delivered: list[int] = []

    async def _delivery(item):
        delivered.append(item["id"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _delivery, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)
    pending = store.list(priority_floor="now")

    assert result == {"claimed": 2, "accepted": 1, "failed": 1}
    assert delivered == [first["id"]]
    assert [row["id"] for row in pending] == [second["id"]]
    assert pending[0]["recovery_error"] == "delivery was not accepted"


@pytest.mark.asyncio
async def test_startup_recovery_failure_stays_pending_with_evidence(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("not accepted", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_fail(_item):
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_fail, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)
    pending = store.list(priority_floor="now")

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    assert [row["id"] for row in pending] == [item["id"]]
    assert pending[0]["delivered_at"] is None
    assert pending[0]["recovery_claimed_at"] is None
    assert pending[0]["recovery_attempted_at"]
    assert pending[0]["recovery_error"] == "delivery was not accepted"


@pytest.mark.asyncio
async def test_startup_recovery_exception_stays_pending_with_evidence(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("raise and keep", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_raise(_item):
        raise RuntimeError("a2a unavailable")

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_raise, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)
    [pending] = store.list(priority_floor="now")

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    assert pending["id"] == item["id"]
    assert pending["delivered_at"] is None
    assert pending["recovery_claimed_at"] is None
    assert pending["recovery_attempted_at"]
    assert pending["recovery_error"] == "delivery raised: a2a unavailable"


@pytest.mark.asyncio
async def test_startup_recovery_cancellation_restores_entire_claimed_batch(tmp_path, monkeypatch):
    """Regression (review): cancelling mid-batch must hand the WHOLE claimed batch back to
    pending — the in-flight item AND every later already-claimed item. The batch claims all
    rows up front, so restoring only the current one strands the tail: hidden from pull
    fallback (``list`` excludes claimed ``now`` items) and from future recovery (the next
    claim requires ``recovery_claimed_at IS NULL``)."""
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    items = [store.add(f"now {i}", priority="now") for i in range(3)]
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_cancel_on_second(item):
        fired.append(item["id"])
        if item["id"] == items[1]["id"]:
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_cancel_on_second, raising=False)

    with pytest.raises(asyncio.CancelledError):
        await recover_pending_now_inbox_items(limit=8)

    # Item 0 was accepted before the cancel → delivered and out of the pending pool.
    # Items 1 (in-flight) and 2 (later, already claimed) are BOTH restored to pending.
    assert fired == [items[0]["id"], items[1]["id"]]  # cancel aborted before item 2 fired
    delivered = store.list(priority_floor="now", include_delivered=True)
    by_id = {row["id"]: row for row in delivered}
    assert by_id[items[0]["id"]]["delivered_at"] is not None
    pending = [row["id"] for row in store.list(priority_floor="now")]
    assert pending == [items[1]["id"], items[2]["id"]]  # nothing stranded
    for item_id in (items[1]["id"], items[2]["id"]):
        assert by_id[item_id]["recovery_claimed_at"] is None
        assert by_id[item_id]["recovery_error"] == "delivery was cancelled"
    # And the restored tail is re-claimable by a subsequent recovery pass (not permanently hidden).
    reclaim = store.claim_now_recovery_batch(limit=8, retry_after_s=0, now=datetime.now(UTC) + timedelta(seconds=10))
    assert [r["id"] for r in reclaim] == [items[1]["id"], items[2]["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_attempt_count_prevents_restart_replay_storm(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    store.add("retry later", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[str] = []

    async def _fire_fail(item):
        fired.append(item["text"])
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_fail, raising=False)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    results = [
        await recover_pending_now_inbox_items(limit=8, max_attempts=3, retry_after_s=3600, now=base),
        await recover_pending_now_inbox_items(
            limit=8,
            max_attempts=3,
            retry_after_s=3600,
            now=base + timedelta(seconds=120),
        ),
        await recover_pending_now_inbox_items(
            limit=8,
            max_attempts=3,
            retry_after_s=3600,
            now=base + timedelta(seconds=3601),
        ),
        await recover_pending_now_inbox_items(
            limit=8,
            max_attempts=3,
            retry_after_s=3600,
            now=base + timedelta(seconds=7202),
        ),
        await recover_pending_now_inbox_items(
            limit=8,
            max_attempts=3,
            retry_after_s=3600,
            now=base + timedelta(seconds=10803),
        ),
    ]

    assert [r["claimed"] for r in results] == [1, 1, 1, 0, 0]  # count-bounded, not clock-bound
    assert results[0] == {"claimed": 1, "accepted": 0, "failed": 1}
    assert fired == ["retry later"] * 3
    # Exhausted, but NOT lost: the pull fallback still surfaces it to an operator.
    assert len(store.list(priority_floor="now")) == 1


@pytest.mark.asyncio
async def test_startup_recovery_excludes_next_and_later(tmp_path, monkeypatch):
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    store.add("run now", priority="now")
    next_item = store.add("wait next", priority="next")
    later_item = store.add("wait later", priority="later")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[str] = []

    async def _fire_ok(item):
        fired.append(item["text"])
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _fire_ok, raising=False)

    result = await recover_pending_now_inbox_items(limit=8)
    pending = store.list(priority_floor="later")

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == ["run now"]
    assert [row["id"] for row in pending] == [next_item["id"], later_item["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_lifecycle_schedules_once_on_running_loop(monkeypatch):
    import server.agent_init as agent_init
    import server.stores as stores
    import runtime.state as rs

    called = asyncio.Event()
    results: list[dict[str, int]] = []

    async def _recover():
        results.append({"claimed": 1, "accepted": 1, "failed": 0})
        called.set()

    monkeypatch.setattr(stores, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(stores, "recover_pending_now_inbox_items", _recover)
    monkeypatch.setattr(rs.STATE, "inbox_store", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", lambda _item: True, raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", asyncio.get_running_loop(), raising=False)

    agent_init._start_inbox_now_recovery_once()
    agent_init._start_inbox_now_recovery_once()
    await asyncio.wait_for(called.wait(), timeout=1)
    await asyncio.sleep(0)

    assert results == [{"claimed": 1, "accepted": 1, "failed": 0}]


@pytest.mark.asyncio
async def test_startup_recovery_lifecycle_waits_for_delivery_hook(monkeypatch):
    import server.agent_init as agent_init
    import server.stores as stores
    import runtime.state as rs

    async def _recover():
        raise AssertionError("recovery must not start before accepted delivery is registered")

    monkeypatch.setattr(stores, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(stores, "recover_pending_now_inbox_items", _recover)
    monkeypatch.setattr(rs.STATE, "inbox_store", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", None, raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", asyncio.get_running_loop(), raising=False)

    agent_init._start_inbox_now_recovery_once()
    await asyncio.sleep(0)

    assert stores._INBOX_NOW_RECOVERY_STARTED is False


def test_startup_recovery_lifecycle_no_loop_keeps_recovery_eligible(monkeypatch):
    import server.agent_init as agent_init
    import server.stores as stores
    import runtime.state as rs

    monkeypatch.setattr(stores, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(rs.STATE, "inbox_store", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", lambda _item: True, raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", None, raising=False)

    agent_init._start_inbox_now_recovery_once()

    assert stores._INBOX_NOW_RECOVERY_STARTED is False


# ── badge dedup: inbox.item fires only for items that land in the queue (#1375) ──


def _capture_inbox_events(monkeypatch):
    import operator_api.console_handlers as ch

    published: list[str] = []
    monkeypatch.setattr(ch._event_bus, "publish", lambda topic, payload=None: published.append(topic))
    return published


@pytest.mark.asyncio
async def test_fired_now_item_does_not_publish_inbox_item(tmp_path, monkeypatch):
    """A fired now-item is an Activity event (activity.message), not an inbox-queue arrival —
    so it must NOT publish inbox.item, which would double-bump the Inbox + Activity badges."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "inbox_store", _store(tmp_path), raising=False)
    published = _capture_inbox_events(monkeypatch)

    async def _fire_ok(_item):
        return True

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)
    await ch._operator_inbox_add({"text": "x", "priority": "now"})
    assert "inbox.item" not in published


@pytest.mark.asyncio
async def test_queued_item_publishes_inbox_item(tmp_path, monkeypatch):
    """A next/later item lands in the queue → publishes inbox.item (one badge)."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "inbox_store", _store(tmp_path), raising=False)
    published = _capture_inbox_events(monkeypatch)
    await ch._operator_inbox_add({"text": "x", "priority": "next"})
    assert "inbox.item" in published


@pytest.mark.asyncio
async def test_failed_now_fire_publishes_inbox_item(tmp_path, monkeypatch):
    """A now-item whose fire FAILED is pending again → it DOES publish inbox.item (the
    check_inbox fallback path needs the operator to see it)."""
    import operator_api.console_handlers as ch
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "inbox_store", _store(tmp_path), raising=False)
    published = _capture_inbox_events(monkeypatch)

    async def _fire_fail(_item):
        return False

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_fail)
    await ch._operator_inbox_add({"text": "x", "priority": "now"})
    assert "inbox.item" in published


# ── POST /api/inbox route ────────────────────────────────────────────────────


def _app_with_inbox(add_impl, *, token="secret"):
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        inbox_add=add_impl,
        inbox_authorized=lambda t: (t == token) if token else True,
    )
    return TestClient(app)


def test_inbox_route_rejects_bad_token():
    async def add(_payload):
        return {"ok": True}

    client = _app_with_inbox(add)
    r = client.post("/api/inbox", json={"text": "hi"})  # no Authorization header
    assert r.status_code == 401
    r2 = client.post("/api/inbox", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"})
    assert r2.status_code == 401


def test_inbox_list_and_deliver_routes():
    captured = {}

    async def inbox_list(floor, include_delivered):
        captured["floor"] = floor
        captured["include_delivered"] = include_delivered
        return {"items": [{"id": 1, "priority": "now", "text": "x"}]}

    async def inbox_deliver(item_id):
        captured["delivered_id"] = item_id
        return {"ok": True, "delivered": 1}

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        inbox_list=inbox_list,
        inbox_deliver=inbox_deliver,
    )
    client = TestClient(app)

    r = client.get("/api/inbox?floor=next&include_delivered=true")
    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == 1
    assert captured["floor"] == "next" and captured["include_delivered"] is True

    r2 = client.post("/api/inbox/7/deliver")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True, "delivered": 1}
    assert captured["delivered_id"] == 7


def test_inbox_route_accepts_with_token():
    seen = []

    async def add(payload):
        seen.append(payload)
        return {"ok": True, "item": {"id": 1, **payload}}

    client = _app_with_inbox(add)
    r = client.post(
        "/api/inbox",
        json={"text": "deploy done", "priority": "now", "source": "ci"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert seen[0]["text"] == "deploy done" and seen[0]["priority"] == "now"


async def _unused(*_a, **_k):  # pragma: no cover - placeholder callable
    return ""


# ── the crash window: recoverability must not depend on wall-clock (#3351 r3) ─────


def test_a_claim_left_by_a_dead_process_is_released_at_startup(tmp_path):
    """The finding this closes: a crash between claim and deliver stranded the page.

    The claim is an in-process lease. Left set, the row is invisible to BOTH the recovery
    selector (which skips claimed rows) and the `check_inbox` pull fallback (which skips
    claimed `now` rows) — so an operator alert went quiet for the whole retry window, and a
    replacement process booting inside that window could not reclaim it at all.
    """
    store = InboxStore(str(tmp_path / "inbox.db"))
    store.add("board card bd-x is blocked", priority="now")
    claimed = store.claim_now_recovery_batch(limit=8)
    assert len(claimed) == 1
    # …the process dies here: no deliver, no restore.
    assert store.list(priority_floor="now") == []  # invisible to pull, as designed in-flight

    released = store.reset_stale_recovery_claims()  # what startup now does first

    assert released == 1
    assert len(store.list(priority_floor="now")) == 1  # pull fallback can see it again
    assert len(store.claim_now_recovery_batch(limit=8)) == 1  # and recovery can reclaim it
    # No clock was advanced: reclaim is immediate, not after retry_after_s.


def test_storm_protection_is_a_bounded_count_not_a_clock(tmp_path):
    """Replaces the wall-clock gate. A page that keeps refusing delivery stops being
    retried after `max_attempts` — but a crashed page is eligible again on the very next
    boot rather than after an hour, because the two concerns are now separate."""
    store = InboxStore(str(tmp_path / "inbox.db"))
    store.add("undeliverable page", priority="now")
    for _ in range(3):
        assert len(store.claim_now_recovery_batch(limit=8, max_attempts=3)) == 1
        store.reset_stale_recovery_claims()  # simulate a boot after each failed attempt
    # Budget spent: recovery stops re-firing it…
    assert store.claim_now_recovery_batch(limit=8, max_attempts=3) == []
    # …but it is NOT lost — the pull fallback still surfaces it to an operator.
    assert len(store.list(priority_floor="now")) == 1


def test_delivery_is_at_least_once_across_an_unrecorded_crash(tmp_path):
    """When the process DIES between an accepted delivery and any durable record, the page IS
    re-fired. That residual at-least-once is unavoidable, not a defect: no reader can
    distinguish it from a claim that never fired without a two-phase commit against the
    Activity path, and a duplicate feed line beats a lost operator alert (#3351 stranded 29
    for four days). Distinct from the RECORDED mark-failure case — where the process survives
    and ``record_recovery_mark_delivered_failure`` persists ``recovery_accepted_at`` so the
    next pass completes the page instead of re-firing (see the completion test above). If
    this ever fails, the unrecorded-crash guarantee changed and the docstring must too."""
    store = InboxStore(str(tmp_path / "inbox.db"))
    store.add("page whose mark_delivered lost the race", priority="now")
    assert len(store.claim_now_recovery_batch(limit=8)) == 1
    # Delivery was accepted, but the process died before ANYTHING recorded it — no
    # recovery_accepted_at, so the row is indistinguishable from one that never fired.
    store.reset_stale_recovery_claims()
    assert len(store.claim_now_recovery_batch(limit=8)) == 1  # fired again: at-least-once


# ── a now-item returns while the Activity thread is busy (#5106 / ADR 0129) ───


@pytest.mark.asyncio
async def test_now_inbox_post_returns_while_a_turn_holds_the_activity_thread(tmp_path, monkeypatch):
    """``POST /api/inbox {priority: "now"}`` answers as soon as the fired turn is
    ACCEPTED (#5106) — and the turn itself runs on the item's own isolated ``job:``
    session (ADR 0129), so a busy Activity thread no longer even delays it.

    The pre-ADR-0129 fire targeted the shared Activity context, so the fired turn
    queued behind whatever turn held that thread (a decision raised there once sat
    unseen for 70 minutes). Now the fire's context is the item's durable execution
    session: the turn runs and COMPLETES while the Activity thread stays held, and
    the Activity context gains no task — it is a summary/feed surface.

    Real a2a-sdk app + ``ProtoAgentExecutor`` + the real per-thread lock; only the turn
    body (the model) is a fake. The Activity lock is held for the whole test.
    """
    import httpx

    import operator_api.console_handlers as ch
    import runtime.state as rs
    import server.a2a as a2a
    from events import ACTIVITY_CONTEXT
    from server import turn_control
    from tests._a2a_app import A2A_HEADERS, drain_handlers, poll_terminal
    from tests._a2a_app import build_app as build_a2a_app

    ran: list[tuple[str, str]] = []

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        # Mirrors the chat driver: a turn takes ITS OWN thread's lock before it yields.
        async with turn_control._thread_lock(f"a2a:{ctx}"):
            ran.append((text, ctx))
            yield ("text", "<output>handled</output>")

    import weakref

    # Locks bind to the loop that first contends them; never share one across tests.
    monkeypatch.setattr(turn_control, "_THREAD_LOCKS", weakref.WeakValueDictionary())
    a2a_app = build_a2a_app(stream)
    inbox = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", inbox, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", None, raising=False)
    monkeypatch.setattr(rs.STATE, "fastapi_app", a2a_app, raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 9, raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", a2a._fire_activity_from_inbox, raising=False)
    published: list = []
    monkeypatch.setattr(ch._event_bus, "publish", lambda topic, payload: published.append(topic))

    operator = FastAPI()
    register_operator_routes(
        operator,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        inbox_add=ch._operator_inbox_add,
    )

    running_turn = turn_control._thread_lock(f"a2a:{ACTIVITY_CONTEXT}")
    await running_turn.acquire()  # a long job is running on the Activity thread
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=operator), base_url="http://test") as c:
            post = asyncio.ensure_future(
                c.post("/api/inbox", json={"text": "deploy done", "priority": "now", "source": "ci"})
            )
            # Wait WITHOUT cancelling: the SDK answers a cancelled blocking send with the
            # task as it stands, so a `wait_for` here would pass on the blocking code too.
            done, _ = await asyncio.wait({post}, timeout=5.0)
            if not done:
                running_turn.release()  # let the blocked send finish before failing
                await post
                pytest.fail("POST /api/inbox (now) did not answer while the Activity thread was busy")
            r = post.result()
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["fired"] is True
        assert isinstance(body["item"]["id"], int)
        # Accepted means owned: nothing is left for check_inbox to process a second time.
        assert inbox.list(priority_floor="later") == []
        assert "inbox.item" not in published

        # The turn runs — once — WHILE the Activity thread stays held (ADR 0129
        # isolation; it used to queue behind that lock), on the item's own session.
        for _ in range(200):
            if ran:
                break
            await asyncio.sleep(0.01)
        assert len(ran) == 1 and "deploy done" in ran[0][0]
        fired_ctx = ran[0][1]
        assert fired_ctx.startswith("job:") and fired_ctx != ACTIVITY_CONTEXT
        # The durable identity the store assigned is the context the fire used.
        assert inbox.execution_identity(body["item"]["id"])["context_id"] == fired_ctx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a2a_app), base_url="http://test") as c:
            tasks = await c.post(
                "/a2a",
                headers=A2A_HEADERS,
                json={"jsonrpc": "2.0", "id": "l", "method": "ListTasks", "params": {"contextId": fired_ctx}},
            )
            listed = tasks.json()["result"]["tasks"]
            assert len(listed) == 1
            final = await poll_terminal(c, listed[0]["id"])
            # The work never executed on the shared Activity context.
            on_activity = await c.post(
                "/a2a",
                headers=A2A_HEADERS,
                json={"jsonrpc": "2.0", "id": "l2", "method": "ListTasks", "params": {"contextId": ACTIVITY_CONTEXT}},
            )
            assert on_activity.json()["result"].get("tasks") in (None, [])
        assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    finally:
        if running_turn.locked():
            running_turn.release()
    await drain_handlers()


def test_now_inbox_fire_asks_the_a2a_layer_to_return_on_acceptance(monkeypatch):
    """The self-send's ``returnImmediately`` is what keeps ``POST /api/inbox`` prompt (#5106)."""
    import server.a2a as a2a
    from runtime.state import STATE

    app = FastAPI()
    captured: dict = {}

    @app.post("/a2a")
    async def _a2a(body: dict):
        captured.update(body)
        return {"result": {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}}}

    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)
    assert asyncio.run(a2a._fire_activity_from_inbox({"id": 21, "text": "x", "priority": "now"})) is True
    assert captured["params"]["configuration"] == {"returnImmediately": True}


async def _now_post_with_turn(tmp_path, monkeypatch, stream):
    """POST a now-item through the real route into a real a2a-sdk app whose turn is
    ``stream``; the executor's terminal hook is the real ``server.a2a._a2a_terminal``.
    Returns (response body, inbox store, published topics). The fired turn runs on the
    item's own ``job:`` session (ADR 0129), so a test that needs to hold it mid-turn
    gates inside its own ``stream`` (the Activity lock no longer delays it)."""
    import httpx

    import operator_api.console_handlers as ch
    import runtime.state as rs
    import server.a2a as a2a
    from a2a_impl.executor import set_terminal_hook
    from server import turn_control
    from tests._a2a_app import build_app as build_a2a_app

    import weakref

    # Locks bind to the loop that first contends them; never share one across tests.
    monkeypatch.setattr(turn_control, "_THREAD_LOCKS", weakref.WeakValueDictionary())
    a2a_app = build_a2a_app(stream)
    inbox = _store(tmp_path)
    for attr, val in {
        "inbox_store": inbox,
        "storm_guard": None,
        "fastapi_app": a2a_app,
        "active_port": 9,
        "inbox_now_delivery": a2a._fire_activity_from_inbox,
        "telemetry_store": None,
        "activity_log": None,
        "scheduler": None,
        "background_mgr": None,
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)
    published: list = []
    monkeypatch.setattr(ch._event_bus, "publish", lambda topic, payload: published.append(topic))
    monkeypatch.setattr(a2a._event_bus, "publish", lambda topic, payload: published.append(topic))
    set_terminal_hook(a2a._a2a_terminal)

    operator = FastAPI()
    register_operator_routes(
        operator,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        inbox_add=ch._operator_inbox_add,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=operator), base_url="http://test") as c:
        post = asyncio.ensure_future(c.post("/api/inbox", json={"text": "deploy done", "priority": "now", "source": "ci"}))
        done, _ = await asyncio.wait({post}, timeout=5.0)
        if not done:
            await post
            set_terminal_hook(None)
            pytest.fail("POST /api/inbox (now) did not answer promptly")
        body = post.result().json()
    return body, inbox, published


async def _until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


@pytest.mark.asyncio
async def test_a_now_item_whose_accepted_turn_fails_is_restored_to_pending(tmp_path, monkeypatch):
    """Returning on acceptance must not lose the old recovery (#5106 review): the fired
    turn fails at the model boundary AFTER the route answered, and the item goes back to
    the pending queue — once — for check_inbox, badged like an unfired item. The turn
    runs on the item's own ``job:`` session (ADR 0129), held open by the gate so the
    accepted-then-failed order is deterministic."""
    from a2a_impl.executor import set_terminal_hook
    from server import turn_control
    from tests._a2a_app import drain_handlers

    calls: list[str] = []
    gate = asyncio.Event()

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        # Mirrors the chat driver: the turn takes its thread's lock, then the model fails.
        async with turn_control._thread_lock(f"a2a:{ctx}"):
            calls.append(text)
            await gate.wait()
            yield ("text", "looking…")
            raise RuntimeError("gateway 502: upstream model unavailable")

    try:
        body, inbox, published = await _now_post_with_turn(tmp_path, monkeypatch, stream)
        assert body["fired"] is True
        assert inbox.list(priority_floor="later") == []  # delivered while the turn still runs
        gate.set()
        assert await _until(lambda: [i["id"] for i in inbox.list(priority_floor="later")] == [body["item"]["id"]])
        assert len(calls) == 1
        # The restore and its badge event are separate steps: wait for the event too, or a
        # loaded xdist worker reads the list between them (CI flake on #5146).
        assert await _until(lambda: published.count("inbox.item") >= 1)
        assert published.count("inbox.item") == 1  # restored once, badged once
        await asyncio.sleep(0.1)
        assert published.count("inbox.item") == 1
    finally:
        gate.set()
        set_terminal_hook(None)
        await drain_handlers()


@pytest.mark.asyncio
async def test_a_now_item_whose_accepted_turn_completes_stays_delivered(tmp_path, monkeypatch):
    from a2a_impl.executor import set_terminal_hook
    from tests._a2a_app import drain_handlers

    ended = asyncio.Event()

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "<output>done</output>")
        ended.set()

    try:
        body, inbox, published = await _now_post_with_turn(tmp_path, monkeypatch, stream)
        assert body["fired"] is True
        await asyncio.wait_for(ended.wait(), 5)
        await asyncio.sleep(0.2)
        assert inbox.list(priority_floor="later") == []
        assert "inbox.item" not in published
    finally:
        set_terminal_hook(None)
        await drain_handlers()


def test_a_terminal_that_beats_the_watch_registration_still_restores(tmp_path, monkeypatch):
    """A turn that fails before the send's answer is read: its terminal is remembered
    and settled when the watch registers — and only once."""
    from types import SimpleNamespace

    import runtime.state as rs
    import server.a2a as a2a

    inbox = _store(tmp_path)
    monkeypatch.setattr(rs.STATE, "inbox_store", inbox, raising=False)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *_a: None)
    item = inbox.add("x", priority="now", source="t")
    inbox.mark_delivered([item["id"]])
    a2a._inbox_turn_ended(SimpleNamespace(origin="inbox", task_id="t-fast", state="failed"))
    a2a._watch_inbox_turn("t-fast", item)
    assert [i["id"] for i in inbox.list(priority_floor="later")] == [item["id"]]
    inbox.mark_delivered([item["id"]])
    a2a._watch_inbox_turn("t-fast", item)  # consumed: no second restore
    assert inbox.list(priority_floor="later") == []


@pytest.mark.asyncio
async def test_startup_recovery_restores_a_page_whose_turn_fails_before_it_is_marked_delivered(tmp_path, monkeypatch):
    """#5116 review: recovery marks a page delivered only AFTER its fire is accepted. A turn
    that fails instantly settles first — its restore finds nothing delivered — and the later
    delivered mark used to bury the failure, losing the page. The restore is now owed to that
    mark and settled right after it. Real a2a-sdk app + executor + ``_a2a_terminal``; the
    turn fails at the model boundary before saying anything."""
    import weakref

    import runtime.state as rs
    import server.a2a as a2a
    from a2a_impl.executor import set_terminal_hook
    from server import turn_control
    from server.agent_init import recover_pending_now_inbox_items
    from tests._a2a_app import build_app as build_a2a_app
    from tests._a2a_app import drain_handlers

    monkeypatch.setattr(turn_control, "_THREAD_LOCKS", weakref.WeakValueDictionary())

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        raise RuntimeError("gateway 401: key revoked")
        yield  # pragma: no cover — an async generator that fails on its first step

    store = _store(tmp_path)
    page = store.add("prod is down", priority="now", source="pager")
    for attr, val in {
        "inbox_store": store,
        "storm_guard": None,
        "fastapi_app": build_a2a_app(stream),
        "active_port": 9,
        "telemetry_store": None,
        "activity_log": None,
        "scheduler": None,
        "background_mgr": None,
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)
    published: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda topic, payload: published.append(topic))

    ended = asyncio.Event()

    def terminal(outcome):
        a2a._a2a_terminal(outcome)
        ended.set()

    async def fire_then_lose_the_race(item):
        ok = await a2a._fire_activity_from_inbox(item)
        await asyncio.wait_for(ended.wait(), 5)  # the turn has already failed…
        await asyncio.sleep(0.1)  # …and its restore ran, finding nothing delivered yet
        return ok

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", fire_then_lose_the_race, raising=False)
    set_terminal_hook(terminal)
    try:
        result = await recover_pending_now_inbox_items(limit=8)
        assert result["accepted"] == 1
        assert [i["id"] for i in store.list(priority_floor="later")] == [page["id"]]
        assert published.count("inbox.item") == 1
    finally:
        set_terminal_hook(None)
        await drain_handlers()
