"""Tests for Discord return-address capture + delivery (ADR 0015, slice 3).

The gateway records the operator's DM channel; reactive Activity-thread output
(``activity.message`` on the bus) is forwarded to it. The store path is pointed
at a tmp file via ``DISCORD_RETURN_ADDRESS_PATH``.
"""

from __future__ import annotations

import pytest

from surfaces.discord import gateway as gw
from surfaces.discord import return_address as ra


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_RETURN_ADDRESS_PATH", str(tmp_path / "ra.json"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("DISCORD_ADMIN_IDS", raising=False)
    gw._message_buffers.clear()
    gw._conversations._conversations.clear()
    gw._invoke = None
    yield
    for e in gw._message_buffers.values():
        if e.get("timer"):
            e["timer"].cancel()
    gw._message_buffers.clear()


# ── store ──────────────────────────────────────────────────────────────────────


def test_record_and_get_roundtrip():
    assert ra.get() is None
    ra.record("chan-123")
    assert ra.get() == "chan-123"


def test_record_is_idempotent_and_updatable():
    ra.record("a")
    ra.record("a")  # no-op
    assert ra.get() == "a"
    ra.record("b")  # newest DM wins
    assert ra.get() == "b"


def test_record_ignores_empty():
    ra.record("")
    assert ra.get() is None


# ── capture (via the gateway) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_captures_return_address(monkeypatch):
    async def fake_api(method, path, body=None):
        return None

    monkeypatch.setattr(gw, "_api", fake_api)
    d = {
        "channel_id": "dm-chan",
        "id": "m1",
        "content": "hey",
        "author": {"id": "u1", "username": "kj"},
        "guild_id": None,
        "mentions": [],
    }
    await gw._handle_message(d, "bot")
    assert ra.get() == "dm-chan"
    for e in gw._message_buffers.values():
        if e.get("timer"):
            e["timer"].cancel()


@pytest.mark.asyncio
async def test_guild_message_does_not_capture(monkeypatch):
    async def fake_api(method, path, body=None):
        return None

    monkeypatch.setattr(gw, "_api", fake_api)
    d = {
        "channel_id": "guild-chan",
        "id": "m1",
        "content": "<@bot> hi",
        "author": {"id": "u1", "username": "kj"},
        "guild_id": "g1",
        "mentions": [{"id": "bot"}],
    }
    await gw._handle_message(d, "bot")
    assert ra.get() is None  # guild channels are not a private return address
    for e in gw._message_buffers.values():
        if e.get("timer"):
            e["timer"].cancel()


# ── delivery ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_message_delivers_to_dm(monkeypatch):
    sent: list = []

    async def fake_api(method, path, body=None):
        sent.append({"method": method, "path": path, "body": body})
        return {"id": "x"}

    monkeypatch.setattr(gw, "_api", fake_api)
    ra.record("dm-chan")

    delivered = await gw._deliver_event({"event": "activity.message", "data": {"text": "reminder: standup at 10"}})
    assert delivered is True
    post = next(c for c in sent if c["method"] == "POST" and c["path"].endswith("/messages"))
    assert post["path"] == "/channels/dm-chan/messages"
    assert post["body"]["content"] == "reminder: standup at 10"
    assert "message_reference" not in post["body"]  # DM, no reply-quote


@pytest.mark.asyncio
async def test_no_delivery_without_return_address(monkeypatch):
    sent: list = []

    async def fake_api(method, path, body=None):
        sent.append(path)
        return None

    monkeypatch.setattr(gw, "_api", fake_api)
    # no ra.record() — nothing captured
    delivered = await gw._deliver_event({"event": "activity.message", "data": {"text": "hi"}})
    assert delivered is False and sent == []


@pytest.mark.asyncio
async def test_non_activity_events_ignored(monkeypatch):
    sent: list = []

    async def fake_api(method, path, body=None):
        sent.append(path)
        return None

    monkeypatch.setattr(gw, "_api", fake_api)
    ra.record("dm-chan")
    assert await gw._deliver_event({"event": "inbox.item", "data": {"text": "x"}}) is False
    assert await gw._deliver_event({"event": "activity.message", "data": {"text": "  "}}) is False
    assert sent == []
