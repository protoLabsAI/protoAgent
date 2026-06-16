"""Slack communication plugin — Socket Mode ChatAdapter (ADR 0029)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from plugins.slack import SlackAdapter


def test_manifest_is_a_comms_plugin():
    m = yaml.safe_load(Path("plugins/slack/protoagent.plugin.yaml").read_text())
    assert m["id"] == "slack" and m["config_section"] == "slack"
    assert {"bot_token", "app_token"} <= set(m["secrets"])
    assert {"enabled", "bot_token", "app_token", "admin_ids"} <= {s["key"] for s in m["settings"]}


def test_configured_needs_both_tokens():
    a = SlackAdapter()
    assert a.configured({"bot_token": "xoxb", "app_token": "xapp"}) is True
    assert a.configured({"bot_token": "xoxb"}) is False
    assert a.configured({"app_token": "xapp"}) is False


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.calls = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(url)
        return _Resp(self._data)


@pytest.mark.asyncio
async def test_validate_ok(monkeypatch):
    fake = _FakeClient({"ok": True, "user": "agentbot"})
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    ok, who, err = await SlackAdapter().validate({"bot_token": "xoxb", "app_token": "xapp"})
    assert ok and who == "agentbot" and err is None


@pytest.mark.asyncio
async def test_validate_missing_tokens():
    ok, who, err = await SlackAdapter().validate({"bot_token": "xoxb"})
    assert not ok and "app-level token" in err


@pytest.mark.asyncio
async def test_validate_auth_error(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient({"ok": False, "error": "invalid_auth"}))
    ok, _who, err = await SlackAdapter().validate({"bot_token": "xoxb", "app_token": "xapp"})
    assert not ok and err == "invalid_auth"
