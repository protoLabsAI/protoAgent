"""Telegram communication plugin — the reference ChatAdapter (ADR 0029)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from plugins.telegram import TelegramAdapter


def test_manifest_is_a_comms_plugin():
    m = yaml.safe_load(Path("plugins/telegram/protoagent.plugin.yaml").read_text())
    assert m["id"] == "telegram" and m["config_section"] == "telegram"
    assert "bot_token" in m["secrets"]
    keys = {s["key"] for s in m["settings"]}
    assert {"enabled", "bot_token", "admin_ids"} <= keys


def test_configured():
    a = TelegramAdapter()
    assert a.configured({"bot_token": "t"}) is True
    assert a.configured({"bot_token": "  "}) is False
    assert a.configured({}) is False


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _fake_client_class(updates):
    instances = []

    class FakeClient:
        def __init__(self, *a, **k):
            self.posts = []
            self._update_calls = 0
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if "getMe" in url:
                return _Resp({"ok": True, "result": {"username": "mybot"}})
            self._update_calls += 1  # getUpdates
            if self._update_calls == 1:
                return _Resp({"ok": True, "result": updates})
            raise asyncio.CancelledError()  # 2nd poll → surface cancelled

        async def post(self, url, json=None):
            self.posts.append(json)
            return _Resp({"ok": True})

    return FakeClient, instances


@pytest.mark.asyncio
async def test_validate_ok(monkeypatch):
    FakeClient, _ = _fake_client_class([])
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    ok, who, err = await TelegramAdapter().validate({"bot_token": "t"})
    assert ok and who == "mybot" and err is None


@pytest.mark.asyncio
async def test_validate_no_token():
    ok, who, err = await TelegramAdapter().validate({})
    assert not ok and "No bot token" in err


@pytest.mark.asyncio
async def test_run_dispatches_inbound_and_reply_sends(monkeypatch):
    update = {"update_id": 1, "message": {"text": "hi", "chat": {"id": 7}, "from": {"id": 42}}}
    FakeClient, instances = _fake_client_class([update])
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    captured = []

    async def handle(msg):
        captured.append(msg)
        await msg.reply("pong")

    with pytest.raises(asyncio.CancelledError):
        await TelegramAdapter().run(handle, cfg={"bot_token": "t"}, host=None)

    assert len(captured) == 1
    m = captured[0]
    assert m.text == "hi" and m.user_id == "42" and m.channel_id == "7"
    assert instances[-1].posts == [{"chat_id": 7, "text": "pong"}]
