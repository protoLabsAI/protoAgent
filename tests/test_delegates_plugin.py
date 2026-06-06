"""Tests for the unified delegate registry plugin (ADR 0025, PR1).

Covers adapter parse/validation per type, secret resolution, the registry
(parse + drop bad + dispatch routing), the delegate_to tool, and a2a/openai/acp
dispatch with fakes.
"""

from __future__ import annotations

import pytest

import plugins.delegates as P
from plugins.delegates.adapters import (
    ADAPTERS,
    DelegateError,
    _secret,
    delegate_types,
)
from plugins.delegates.registry import DelegateRegistry


# ── adapter parse / validation ────────────────────────────────────────────────


def test_a2a_parse_ok_and_missing_url():
    d = ADAPTERS["a2a"].parse({"name": "helm", "type": "a2a", "url": "https://h/a2a",
                               "auth": {"scheme": "bearer", "token": "sek"}})
    assert d.name == "helm" and d.url == "https://h/a2a"
    assert d.auth_scheme == "bearer" and d.auth_token == "sek"
    with pytest.raises(DelegateError):
        ADAPTERS["a2a"].parse({"name": "x", "type": "a2a"})  # no url


def test_openai_parse_ok_and_requires_url_model():
    d = ADAPTERS["openai"].parse({"name": "opus", "type": "openai",
                                  "url": "https://g/v1", "model": "protolabs/reasoning",
                                  "api_key": "k", "max_tokens": "50", "temperature": "0.1"})
    assert d.model == "protolabs/reasoning" and d.api_key == "k"
    assert d.max_tokens == 50 and d.temperature == pytest.approx(0.1)
    with pytest.raises(DelegateError):
        ADAPTERS["openai"].parse({"name": "x", "type": "openai", "url": "https://g/v1"})  # no model


def test_acp_parse_ok_and_requires_command_workdir():
    d = ADAPTERS["acp"].parse({"name": "proto", "type": "acp", "command": "proto",
                               "args": ["--acp"], "workdir": "/tmp", "permissions": "READONLY",
                               "confirm": "true"})
    assert d.command == "proto" and d.args == ["--acp"] and d.workdir == "/tmp"
    assert d.permissions == "readonly" and d.confirm is True
    with pytest.raises(DelegateError):
        ADAPTERS["acp"].parse({"name": "x", "type": "acp", "command": "proto"})  # no workdir


def test_secret_value_wins_then_env(monkeypatch):
    assert _secret({"token": "explicit"}, "token", "credentialsEnv") == "explicit"
    monkeypatch.setenv("MY_TOK", "fromenv")
    assert _secret({"credentialsEnv": "MY_TOK"}, "token", "credentialsEnv") == "fromenv"
    assert _secret({}, "token", "credentialsEnv") == ""


def test_delegate_types_schema_shape():
    types = {t["type"]: t for t in delegate_types()}
    assert set(types) == {"a2a", "openai", "acp"}
    # each type advertises a field schema with required keys
    for t in types.values():
        assert t["label"] and isinstance(t["fields"], list) and t["fields"]
        for f in t["fields"]:
            assert {"key", "label", "kind"} <= set(f)


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_parses_and_drops_bad():
    reg = DelegateRegistry([
        {"name": "helm", "type": "a2a", "url": "https://h/a2a"},
        {"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"},
        {"name": "bad", "type": "nope"},                 # unknown type
        {"name": "helm", "type": "a2a", "url": "https://dup/a2a"},  # duplicate
        {"name": "incomplete", "type": "acp", "command": "proto"},  # no workdir
        "not-a-dict",
    ])
    assert reg.names() == ["helm", "opus"]
    assert reg.get("helm").url == "https://h/a2a"        # first dup wins
    assert "helm" in reg.listing() and "a2a" in reg.listing()


async def test_registry_dispatch_unknown_raises():
    reg = DelegateRegistry([])
    with pytest.raises(DelegateError):
        await reg.dispatch("nope", "hi")


# ── delegate_to tool ──────────────────────────────────────────────────────────


def _register(delegates, monkeypatch):
    monkeypatch.setattr(P, "_load_delegates_config", lambda: delegates)

    class _Reg:
        def __init__(self):
            self.config = {}
            self.tools = []

        def register_tool(self, t):
            self.tools.append(t)

    r = _Reg()
    P.register(r)
    return r


def test_register_no_delegates_registers_nothing(monkeypatch):
    r = _register([], monkeypatch)
    assert r.tools == []


def test_register_exposes_delegate_to_with_listing(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    assert [t.name for t in r.tools] == ["delegate_to"]
    assert "opus" in r.tools[0].description


async def test_delegate_to_unknown_and_empty(monkeypatch):
    r = _register([{"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}], monkeypatch)
    tool = r.tools[0]
    assert "unknown delegate" in await tool.ainvoke({"target": "nope", "query": "hi"})
    assert "empty" in (await tool.ainvoke({"target": "opus", "query": "  "})).lower()


# ── dispatch with fakes ───────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload, **kw):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return _FakeResp(self._p)


async def test_openai_dispatch(monkeypatch):
    import httpx
    payload = {"choices": [{"message": {"content": "the answer"}}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(payload))
    d = ADAPTERS["openai"].parse({"name": "o", "type": "openai", "url": "https://g/v1", "model": "m"})
    assert await ADAPTERS["openai"].dispatch(d, "q") == "the answer"


async def test_a2a_dispatch_inline_reply(monkeypatch):
    import httpx
    # message/send returns an artifact with text → _extract_text picks it up.
    payload = {"result": {"artifacts": [{"parts": [{"kind": "text", "text": "hi from peer"}]}]}}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(payload))
    import security
    monkeypatch.setattr(security, "check_url", lambda url: None)
    d = ADAPTERS["a2a"].parse({"name": "p", "type": "a2a", "url": "https://p/a2a"})
    assert await ADAPTERS["a2a"].dispatch(d, "q") == "hi from peer"


async def test_acp_dispatch_reuses_client(monkeypatch):
    import plugins.coding_agent as CA

    class _StubClient:
        _permission = None

        async def prompt(self, query, timeout=600.0):
            return "coding done"

    monkeypatch.setattr(CA, "_client_for", lambda spec: _StubClient())
    d = ADAPTERS["acp"].parse({"name": "proto", "type": "acp", "command": "proto", "workdir": "/tmp"})
    assert await ADAPTERS["acp"].dispatch(d, "fix the bug") == "coding done"
