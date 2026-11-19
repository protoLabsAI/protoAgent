"""Telegram communication plugin — the reference ChatAdapter (ADR 0029)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from plugins.telegram import TelegramAdapter


def test_manifest_is_a_comms_plugin():
    m = yaml.safe_load(Path("plugins/telegram/protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert m["id"] == "telegram" and m["config_section"] == "telegram"
    assert "bot_token" in m["secrets"]
    keys = {s["key"] for s in m["settings"]}
    assert {"enabled", "bot_token", "admin_ids", "owner_chat_id"} <= keys


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner, failure",
    [
        ("42", None),
        ("42", "network"),
        ("42", "http"),
        ("42", "api"),
        (None, None),
        ("-100123", None),
        ("@group", None),
        ("0", None),
    ],
)
async def test_typed_notices_only_reach_configured_owner(monkeypatch, caplog, owner, failure):
    from types import SimpleNamespace

    from graph.plugins.chat_surface import notify_chat_surfaces
    from plugins.telegram import register
    from tests.test_chat_surface import FakeHost, FakeRegistry

    ready = asyncio.Event()
    posts = []

    class Client:
        def __init__(self, **kwargs):
            self.polled = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            if not self.polled:
                self.polled = True
                return _Resp(
                    {
                        "result": [
                            {"update_id": 1, "message": {"text": "hello", "chat": {"id": -99}, "from": {"id": 7}}},
                            {"update_id": 2, "message": {"text": "hi", "chat": {"id": 8}, "from": {"id": 8}}},
                        ]
                    }
                )
            ready.set()
            await asyncio.Event().wait()

        async def post(self, url, json):
            posts.append(json)
            if json["text"] == "RESPONSE":
                return _Resp({"ok": True})
            if failure == "network":
                raise httpx.ConnectError("secret-token in request URL")
            return httpx.Response(
                403 if failure == "http" else 200,
                json={"ok": failure != "api"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = FakeRegistry(
        {"enabled": True, "bot_token": "secret-token", "owner_chat_id": owner, "admin_ids": [7, 8]},
        FakeHost(),
    )
    register(registry)
    task = registry.surface["start"]()
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert [post["chat_id"] for post in posts] == [-99, 8]
        posts.clear()
        notify_chat_surfaces("Fleet down", "https://console.example/app/", kind="review", importance="high")
        # Drain call_soon_threadsafe and the async hook (the fake HTTP send never suspends).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert posts == (
            [{"chat_id": "42", "text": "Fleet down\n\nhttps://console.example/app/"}] if owner == "42" else []
        )
        if failure:
            assert "[telegram] proactive notification" in caplog.text
            assert "secret-token" not in caplog.text
            assert not any(record.name == "protoagent.plugins.chat_surface" for record in caplog.records)
        if owner == "42" and failure is None:
            ready.clear()
            await registry.surface["reload"](
                SimpleNamespace(
                    plugin_config={
                        "telegram": {**registry.config, "owner_chat_id": "55"},
                    }
                )
            )
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(ready.wait(), 2)
            posts.clear()
            notify_chat_surfaces("Morning brief", "/app/", kind="notify", importance="low")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert posts == [{"chat_id": "55", "text": "Morning brief\n\n/app/"}]
    finally:
        await registry.surface["stop"]()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # Let a reloaded gateway finish cancellation too.
    posts.clear()
    notify_chat_surfaces("Stopped", "/app/", kind="notify", importance="normal")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert posts == []
