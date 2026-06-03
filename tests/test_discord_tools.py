"""Tests for the outbound Discord tools (ADR 0015).

httpx is faked the same way the scheduler tests do it — patch
``httpx.AsyncClient`` with a fake that records the request and replays a canned
response. No network, no token needed beyond the env the test sets.
"""

from __future__ import annotations

import httpx
import pytest

import tools.discord_tools as dt


class _Resp:
    def __init__(self, status_code=200, payload=None, text="", content=b"x"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_client(capture: list, resp):
    """A fake ``httpx.AsyncClient`` (async ctx mgr) whose ``.request`` records the
    call and returns ``resp`` (or, if a list, one per call in order)."""

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def request(self, method, url, headers=None, json=None):
            capture.append({"method": method, "url": url, "headers": headers, "json": json})
            return resp.pop(0) if isinstance(resp, list) else resp

    return _Client


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")


# ── gate ──────────────────────────────────────────────────────────────────────


def test_discord_configured_reflects_token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "abc")
    assert dt.discord_configured() is True
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert dt.discord_configured() is False
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "   ")
    assert dt.discord_configured() is False  # whitespace-only ⇒ not configured


@pytest.mark.asyncio
async def test_no_token_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    out = await dt.discord_send.ainvoke({"channel_id": "123", "content": "hi"})
    assert "DISCORD_BOT_TOKEN" in out


# ── send ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_posts_and_returns_id(monkeypatch):
    cap: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(cap, _Resp(200, {"id": "999"})))
    out = await dt.discord_send.ainvoke({"channel_id": "123", "content": "hello"})
    assert "OK: posted 1 message" in out and "999" in out
    assert cap[0]["method"] == "POST"
    assert cap[0]["url"].endswith("/channels/123/messages")
    assert cap[0]["json"] == {"content": "hello"}
    assert cap[0]["headers"]["Authorization"] == "Bot test-token"


@pytest.mark.asyncio
async def test_send_splits_long_content(monkeypatch):
    cap: list = []
    # two posts ⇒ two canned responses
    monkeypatch.setattr(
        httpx, "AsyncClient", _fake_client(cap, [_Resp(200, {"id": "a"}), _Resp(200, {"id": "b"})])
    )
    body = ("x" * 1500 + "\n") + ("y" * 1500)  # > 2000, splits at the newline
    out = await dt.discord_send.ainvoke({"channel_id": "123", "content": body})
    assert "posted 2 message(s)" in out
    assert len(cap) == 2
    assert all(len(c["json"]["content"]) <= dt._MAX_MESSAGE_LEN for c in cap)


@pytest.mark.asyncio
async def test_send_surfaces_http_error(monkeypatch):
    cap: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(cap, _Resp(403, text="Forbidden")))
    out = await dt.discord_send.ainvoke({"channel_id": "123", "content": "hi"})
    assert "Error: HTTP 403" in out


@pytest.mark.asyncio
async def test_send_validates_inputs(monkeypatch):
    assert "channel_id is required" in await dt.discord_send.ainvoke({"channel_id": "", "content": "x"})
    assert "content is empty" in await dt.discord_send.ainvoke({"channel_id": "1", "content": "  "})


# ── read ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_formats_messages(monkeypatch):
    cap: list = []
    payload = [
        {"author": {"username": "kj"}, "timestamp": "2026-06-03T01:02:03.000Z", "content": "yo"},
        {"author": {"username": "bot", "bot": True}, "timestamp": "2026-06-03T01:02:04.000Z", "content": "beep"},
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(cap, _Resp(200, payload)))
    out = await dt.discord_read.ainvoke({"channel_id": "123", "limit": 5})
    assert "2 message(s) in channel 123" in out
    assert "@kj: yo" in out and "@bot [bot]: beep" in out
    assert "limit=5" in cap[0]["url"]


@pytest.mark.asyncio
async def test_read_clamps_limit(monkeypatch):
    cap: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(cap, _Resp(200, [])))
    await dt.discord_read.ainvoke({"channel_id": "1", "limit": 9999})
    assert "limit=100" in cap[0]["url"]  # clamped to Discord's max


# ── react ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_react_encodes_emoji(monkeypatch):
    cap: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(cap, _Resp(204, content=b"")))
    out = await dt.discord_react.ainvoke({"channel_id": "1", "message_id": "2", "emoji": "✅"})
    assert "OK: reacted" in out
    assert cap[0]["method"] == "PUT"
    assert "/reactions/" in cap[0]["url"] and "%E2%9C%85" in cap[0]["url"]  # URL-encoded ✅


# ── rate limit ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_surfaced(monkeypatch):
    cap: list = []
    monkeypatch.setattr(
        httpx, "AsyncClient", _fake_client(cap, _Resp(429, {"retry_after": 1.5}, text="slow down"))
    )
    out = await dt.discord_send.ainvoke({"channel_id": "1", "content": "hi"})
    assert "HTTP 429" in out and "retry_after=1.5" in out
