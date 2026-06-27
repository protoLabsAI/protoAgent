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
