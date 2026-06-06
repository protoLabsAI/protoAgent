"""Tests for the inbound Discord gateway (ADR 0015).

The WebSocket loop itself isn't unit-tested (it's thin glue over Discord's
gateway); the message-handling logic is. ``_api`` (the REST helper every
send/react/thread routes through) is mocked to record calls, and the agent
invoker is a fake — so routing, debounce buffering, burst combining, reactions,
and auto-threading are all exercised without network or LLM.
"""

from __future__ import annotations


import pytest

from surfaces.discord import gateway as gw

BOT = "botid"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("DISCORD_ADMIN_IDS", raising=False)
    gw._message_buffers.clear()
    gw._conversations._conversations.clear()
    gw._invoke = None
    gw._publish = None
    gw._turn_log = False  # disable long-window warming here — covered in its own test
    yield
    gw._turn_log = None
    for e in gw._message_buffers.values():
        if e.get("timer"):
            e["timer"].cancel()
    gw._message_buffers.clear()


def _api_recorder(monkeypatch):
    calls: list = []

    async def fake_api(method, path, body=None):
        calls.append({"method": method, "path": path, "body": body})
        if method == "POST" and path.endswith("/messages"):
            return {"id": "reply1"}
        if path.endswith("/threads"):
            return {"id": "thread1"}
        return None

    monkeypatch.setattr(gw, "_api", fake_api)
    return calls


def _msg(*, content, channel="chan", mid="m1", user="u1", dm=True, mentions=()):
    return {
        "channel_id": channel, "id": mid, "content": content,
        "author": {"id": user, "username": "kj"},
        "guild_id": None if dm else "g1",
        "mentions": [{"id": m} for m in mentions],
    }


def _cancel_timers():
    for e in gw._message_buffers.values():
        if e.get("timer"):
            e["timer"].cancel()


# ── routing ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_is_buffered(monkeypatch):
    _api_recorder(monkeypatch)
    await gw._handle_message(_msg(content="hi", dm=True), BOT)
    assert "chan:u1" in gw._message_buffers
    _cancel_timers()


@pytest.mark.asyncio
async def test_guild_without_mention_is_ignored(monkeypatch):
    _api_recorder(monkeypatch)
    await gw._handle_message(_msg(content="hello channel", dm=False), BOT)
    assert gw._message_buffers == {}


@pytest.mark.asyncio
async def test_guild_mention_is_buffered_and_stripped(monkeypatch):
    _api_recorder(monkeypatch)
    await gw._handle_message(
        _msg(content=f"<@{BOT}> what's up", dm=False, mentions=[BOT]), BOT)
    entry = gw._message_buffers.get("chan:u1")
    assert entry and entry["messages"][0]["content"] == "what's up"  # mention stripped
    _cancel_timers()


@pytest.mark.asyncio
async def test_bot_and_self_messages_ignored(monkeypatch):
    _api_recorder(monkeypatch)
    d = _msg(content="hi")
    d["author"]["bot"] = True
    await gw._handle_message(d, BOT)
    self_msg = _msg(content="hi", user=BOT)
    await gw._handle_message(self_msg, BOT)
    assert gw._message_buffers == {}


@pytest.mark.asyncio
async def test_admin_allowlist_blocks_others(monkeypatch):
    monkeypatch.setenv("DISCORD_ADMIN_IDS", "admin1, admin2")
    _api_recorder(monkeypatch)
    await gw._handle_message(_msg(content="hi", user="stranger"), BOT)
    assert gw._message_buffers == {}
    await gw._handle_message(_msg(content="hi", user="admin1"), BOT)
    assert "chan:admin1" in gw._message_buffers
    _cancel_timers()


# ── flush / invocation ──────────────────────────────────────────────────────────


def _seed_buffer(*, channel="chan", user="u1", is_dm=True, is_new=True, contents=("hi",)):
    cid, _new, _t = gw._conversations.get_or_create(channel, user, timeout_s=900)
    gw._message_buffers[f"{channel}:{user}"] = {
        "messages": [{"id": f"m{i}", "content": c} for i, c in enumerate(contents)],
        "channel_id": channel, "user_id": user, "is_dm": is_dm,
        "conversation_id": cid, "is_new_conversation": is_new, "timer": None,
    }


@pytest.mark.asyncio
async def test_flush_combines_burst_and_tags_session(monkeypatch):
    calls = _api_recorder(monkeypatch)
    seen: dict = {}

    async def fake_invoke(prompt, session_id):
        seen["prompt"], seen["session_id"] = prompt, session_id
        return "answer"

    gw._invoke = fake_invoke
    _seed_buffer(contents=("first", "second", "third"))
    await gw._flush_burst("chan:u1")

    assert seen["prompt"] == "first\n\nsecond\n\nthird"  # burst combined
    assert seen["session_id"].startswith("discord-dm:")  # surface-tagged session
    posts = [c for c in calls if c["method"] == "POST" and c["path"].endswith("/messages")]
    assert posts and posts[0]["body"]["content"] == "answer"
    assert "message_reference" not in posts[0]["body"]  # DM: no reply-quote


@pytest.mark.asyncio
async def test_guild_reply_quotes_and_auto_threads(monkeypatch):
    calls = _api_recorder(monkeypatch)
    gw._invoke = lambda p, s: _coro("ok")
    _seed_buffer(is_dm=False, is_new=True, contents=("question here",))
    await gw._flush_burst("chan:u1")

    post = next(c for c in calls if c["method"] == "POST" and c["path"].endswith("/messages"))
    assert post["body"]["message_reference"] == {"message_id": "m0"}  # guild reply quotes
    thread = next((c for c in calls if c["path"].endswith("/threads")), None)
    assert thread and thread["body"]["name"] == "question here"  # auto-threaded on first reply


@pytest.mark.asyncio
async def test_empty_reply_gets_graceful_fallback(monkeypatch):
    calls = _api_recorder(monkeypatch)
    gw._invoke = lambda p, s: _coro("   ")
    _seed_buffer(contents=("hi",))
    await gw._flush_burst("chan:u1")
    post = next(c for c in calls if c["method"] == "POST" and c["path"].endswith("/messages"))
    assert "lost the thread" in post["body"]["content"]


# ── slow reaction ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_reaction_places_eyes_for_guild(monkeypatch):
    monkeypatch.setenv("DISCORD_SLOW_REACTION_S", "0")
    calls = _api_recorder(monkeypatch)
    state: dict = {"placed": False}
    await gw._slow_reaction_arm("chan", [{"id": "m1"}, {"id": "m2"}], False, state)
    assert state["placed"] is True
    reacts = [c for c in calls if "/reactions/" in c["path"] and c["method"] == "PUT"]
    assert len(reacts) == 2  # 👀 on both burst messages


@pytest.mark.asyncio
async def test_slow_reaction_skips_dm(monkeypatch):
    calls = _api_recorder(monkeypatch)
    state: dict = {"placed": False}
    await gw._slow_reaction_arm("chan", [{"id": "m1"}], True, state)
    assert state["placed"] is False and calls == []


# ── opt-in gate ──────────────────────────────────────────────────────────────────


def test_start_in_background_off_without_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert gw.start_in_background(lambda p, s: _coro("x")) is None


def _coro(value):
    async def _c():
        return value
    return _c()
