"""Tests for the inbound inbox: store, storm guard, tool, route (ADR 0003)."""

from __future__ import annotations

import asyncio
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
    assert msg["contextId"] == ACTIVITY_CONTEXT
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


# ── check_inbox tool ─────────────────────────────────────────────────────────


def test_check_inbox_tool_returns_and_marks_delivered(tmp_path):
    from tools.lg_tools import _build_inbox_tools

    s = _store(tmp_path)
    s.add("ping one", priority="next", source="webhook")
    s.add("ping two", priority="now")
    (check_inbox,) = _build_inbox_tools(s)

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
    (check_inbox,) = _build_inbox_tools(store)

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

    (check_inbox,) = _build_inbox_tools(store)
    assert "bg done" in await check_inbox.ainvoke({"priority_floor": "later"})


# ── startup recovery for pre-existing now backlog (#3351) ───────────────────


def test_claim_now_recovery_batch_reserves_bounded_and_is_idempotent(tmp_path):
    """The claim atomically stamps a bounded in-flight marker on now items, so a
    second claim (a concurrent consumer / a restart) sees nothing to re-fire."""
    store = _store(tmp_path)
    a = store.add("now a", priority="now")
    b = store.add("now b", priority="now")
    c = store.add("now c", priority="now")
    store.add("next d", priority="next")  # excluded: not now-priority

    claimed = store.claim_now_recovery_batch(limit=2, retry_after_s=3600)
    assert [r["id"] for r in claimed] == [a["id"], b["id"]]  # bounded to the batch limit
    # Claimed rows drop out of the pending pool: no concurrent double-delivery.
    assert [r["id"] for r in store.list(priority_floor="now")] == [c["id"]]
    reserved = store.list(priority_floor="now", include_delivered=True)
    for row in reserved:
        if row["id"] in {a["id"], b["id"]}:
            assert row["delivered_at"] is None
            assert row["recovery_claimed_at"] is not None
            assert row["recovery_attempted_at"] is not None
    # A repeat claim within the retry window returns only the still-unclaimed item.
    again = store.claim_now_recovery_batch(limit=2, retry_after_s=3600)
    assert [r["id"] for r in again] == [c["id"]]


def test_restore_recovery_failure_reopens_pending_with_evidence(tmp_path):
    """Restoring an unfired claim clears ``recovery_claimed_at`` (pull fallback works)
    while keeping ``recovery_attempted_at`` (restart backoff) and recording evidence."""
    store = _store(tmp_path)
    item = store.add("boom", priority="now")
    [claimed] = store.claim_now_recovery_batch(limit=8)
    assert claimed["id"] == item["id"]
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


def test_recovery_claim_does_not_expire_into_pending_or_reclaim(tmp_path):
    """Regression (review): recovery claims are not finite leases. A slow delivery
    must not let a later serially claimed item become visible for concurrent redelivery."""
    store = _store(tmp_path)
    item = store.add("re-claimed", priority="now")

    # Claim far enough in the past that the old finite lease and retry cooldown would
    # have expired. The row must still remain reserved until explicit restore/delivery.
    stale = datetime.now(UTC) - timedelta(seconds=4000)
    [first] = store.claim_now_recovery_batch(limit=8, retry_after_s=3600, now=stale)

    assert store.list(priority_floor="now") == []
    assert store.claim_now_recovery_batch(limit=8, retry_after_s=3600) == []
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["recovery_claimed_at"] == first["recovery_claimed_at"]


@pytest.mark.asyncio
async def test_startup_recovery_does_not_reopen_a_concurrently_delivered_item(tmp_path, monkeypatch):
    """End-to-end: when recovery reports 'not accepted' after another owner delivered
    the page, the driver must leave the item delivered rather than un-delivering it."""
    import operator_api.console_handlers as ch
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

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_then_deliver_concurrently)

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

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)
    monkeypatch.setattr(store, "mark_delivered", _mark_delivered_raises)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    assert store.list(priority_floor="now") == []  # still withheld from pull fallback
    [row] = store.list(priority_floor="now", include_delivered=True)
    assert row["id"] == item["id"]
    assert row["delivered_at"] is None
    assert row["recovery_claimed_at"] is not None
    assert (
        row["recovery_error"]
        == "accepted delivery could not be marked delivered: sqlite write failed"
    )


@pytest.mark.asyncio
async def test_startup_recovery_accepts_preexisting_now_backlog(tmp_path, monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    old_now = store.add("before repair", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(item):
        fired.append(item["id"])
        return True

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

    result = await recover_pending_now_inbox_items(limit=8)

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == [old_now["id"]]
    assert store.list(priority_floor="later") == []


@pytest.mark.asyncio
async def test_startup_recovery_reserves_delivery_during_fire_to_block_double_delivery(tmp_path, monkeypatch):
    """The claim stamps ``recovery_claimed_at`` before the awaited fire, so a
    concurrent inbox consumer (a ``check_inbox`` pull or a fresh now-post) can't
    grab the same still-pending page and deliver it twice. On acceptance only
    then marks it delivered."""
    import operator_api.console_handlers as ch
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

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

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
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    items = [store.add(f"now {i}", priority="now") for i in range(3)]
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[int] = []

    async def _fire_ok(item):
        fired.append(item["id"])
        return True

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

    result = await recover_pending_now_inbox_items(limit=2)

    assert result == {"claimed": 2, "accepted": 2, "failed": 0}
    assert fired == [items[0]["id"], items[1]["id"]]
    assert [row["id"] for row in store.list(priority_floor="now")] == [items[2]["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_failure_stays_pending_with_evidence(tmp_path, monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("not accepted", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_fail(_item):
        return False

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_fail)

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
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    item = store.add("raise and keep", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _fire_raise(_item):
        raise RuntimeError("a2a unavailable")

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_raise)

    result = await recover_pending_now_inbox_items(limit=8)
    [pending] = store.list(priority_floor="now")

    assert result == {"claimed": 1, "accepted": 0, "failed": 1}
    assert pending["id"] == item["id"]
    assert pending["delivered_at"] is None
    assert pending["recovery_claimed_at"] is None
    assert pending["recovery_attempted_at"]
    assert pending["recovery_error"] == "delivery raised: a2a unavailable"


@pytest.mark.asyncio
async def test_startup_recovery_retry_cooldown_prevents_restart_replay(tmp_path, monkeypatch):
    import operator_api.console_handlers as ch
    import runtime.state as rs
    from server.agent_init import recover_pending_now_inbox_items

    store = _store(tmp_path)
    store.add("retry later", priority="now")
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    fired: list[str] = []

    async def _fire_fail(item):
        fired.append(item["text"])
        return False

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_fail)

    first = await recover_pending_now_inbox_items(limit=8, retry_after_s=3600)
    second = await recover_pending_now_inbox_items(limit=8, retry_after_s=3600)

    assert first == {"claimed": 1, "accepted": 0, "failed": 1}
    assert second == {"claimed": 0, "accepted": 0, "failed": 0}
    assert fired == ["retry later"]
    assert len(store.list(priority_floor="now")) == 1


@pytest.mark.asyncio
async def test_startup_recovery_excludes_next_and_later(tmp_path, monkeypatch):
    import operator_api.console_handlers as ch
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

    monkeypatch.setattr(ch, "_fire_activity_from_inbox", _fire_ok)

    result = await recover_pending_now_inbox_items(limit=8)
    pending = store.list(priority_floor="later")

    assert result == {"claimed": 1, "accepted": 1, "failed": 0}
    assert fired == ["run now"]
    assert [row["id"] for row in pending] == [next_item["id"], later_item["id"]]


@pytest.mark.asyncio
async def test_startup_recovery_lifecycle_schedules_once_on_running_loop(monkeypatch):
    import server.agent_init as agent_init
    import runtime.state as rs

    called = asyncio.Event()
    results: list[dict[str, int]] = []

    async def _recover():
        results.append({"claimed": 1, "accepted": 1, "failed": 0})
        called.set()

    monkeypatch.setattr(agent_init, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(agent_init, "recover_pending_now_inbox_items", _recover)
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
    import runtime.state as rs

    async def _recover():
        raise AssertionError("recovery must not start before accepted delivery is registered")

    monkeypatch.setattr(agent_init, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(agent_init, "recover_pending_now_inbox_items", _recover)
    monkeypatch.setattr(rs.STATE, "inbox_store", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", None, raising=False)
    monkeypatch.setattr(rs.STATE, "main_loop", asyncio.get_running_loop(), raising=False)

    agent_init._start_inbox_now_recovery_once()
    await asyncio.sleep(0)

    assert agent_init._INBOX_NOW_RECOVERY_STARTED is False


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
