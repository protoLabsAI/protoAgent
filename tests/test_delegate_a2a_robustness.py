"""A2A delegate robustness — configurable poll timeout + error transparency.

The old dispatch hard-capped polling at 30s (long delegated tasks were cut off
mid-flight) and surfaced opaque errors. These cover the configurable
``poll_timeout_s`` and the legible cause mapping (unreachable / timed-out /
version-incompatible).
"""

from __future__ import annotations

import asyncio
import json as _json

import httpx
import pytest

from plugins.delegates.adapters import A2aAdapter, Delegate, DelegateError, _a2a_error_detail

A = A2aAdapter()


def _parse(**raw):
    return A.parse({"name": "peer", "type": "a2a", "url": "http://127.0.0.1:9/a2a", **raw})


# ── parse: poll_timeout_s ──────────────────────────────────────────────────────


def test_parse_poll_timeout_default():
    assert _parse().poll_timeout_s == 300.0


def test_parse_poll_timeout_override():
    assert _parse(poll_timeout_s=120).poll_timeout_s == 120.0


def test_parse_poll_timeout_invalid_falls_back():
    assert _parse(poll_timeout_s="nope").poll_timeout_s == 300.0


def test_a2a_schema_exposes_poll_timeout():
    keys = [f.key for f in A.config_schema()]
    assert "poll_timeout_s" in keys


# ── error-detail mapping ───────────────────────────────────────────────────────


def test_error_detail_version_skew():
    d = Delegate(name="peer", type="a2a")
    msg = _a2a_error_detail(d, {"code": -32009, "message": "anything"})
    assert "VERSION_NOT_SUPPORTED" in msg
    assert "peer" in msg


def test_error_detail_generic_keeps_message():
    d = Delegate(name="peer", type="a2a")
    msg = _a2a_error_detail(d, {"code": -1, "message": "boom"})
    assert "boom" in msg


# ── dispatch: transport + protocol error transparency ──────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Returns ``send_resp`` for SendMessage and ``get_resp`` for GetTask forever
    (no queue to exhaust), or raises ``raise_exc`` on every post."""

    def __init__(self, *, send_resp=None, get_resp=None, raise_exc=None, **_kw):
        self.send_resp = send_resp
        self.get_resp = get_resp if get_resp is not None else send_resp
        self.raise_exc = raise_exc
        self.posts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.send_resp if (json or {}).get("method") == "SendMessage" else self.get_resp


@pytest.fixture
def patched(monkeypatch):
    """Allow the url (skip the egress policy) and skip real sleeps."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    return monkeypatch


def _install_client(monkeypatch, **kw):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **client_kw: _FakeClient(**kw, **client_kw))


def test_dispatch_unreachable_maps_to_clear_error(patched):
    _install_client(patched, raise_exc=httpx.ConnectError("refused"))
    d = _parse()
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(d, "hi"))
    assert "unreachable" in str(ei.value)


def test_dispatch_version_error_maps_to_clear_error(patched):
    _install_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "error": {"code": -32009, "message": "x"}}))
    d = _parse()
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(d, "hi"))
    assert "VERSION_NOT_SUPPORTED" in str(ei.value)


def test_dispatch_deadline_exceeded_reports_still_running(patched):
    # A task that never reaches a terminal state; with a tiny poll timeout the dispatch
    # must give up locally with a "still running" message (not hang, not the old 30s cap).
    patched.setattr("tools.a2a_parse._extract_text", lambda *_a, **_k: "")
    patched.setattr("tools.a2a_parse._is_terminal", lambda *_a, **_k: False)
    running = _Resp({"jsonrpc": "2.0", "result": {"task": {"id": "t1", "status": {"state": "RUNNING"}}}})
    _install_client(patched, send_resp=running)
    d = _parse(poll_timeout_s=0.01)
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(d, "hi"))
    assert "still running" in str(ei.value)


def test_dispatch_returns_immediate_text(patched):
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    _install_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))
    d = _parse()
    assert asyncio.run(A.dispatch(d, "ping")) == "pong"
