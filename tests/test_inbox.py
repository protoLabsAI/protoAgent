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


def test_add_rejects_empty_and_bad_priority(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.add("   ")
    with pytest.raises(ValueError):
        s.add("hi", priority="urgent")


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
