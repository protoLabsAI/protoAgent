"""Fleet reverse proxy (ADR 0042 slug routing) — graph/fleet/proxy.py.

The proxy is the hot path for the unified console: every console window on
``/app/agent/<slug>/`` rewrites its calls to ``/agents/<slug>/*``, which this
module forwards to the agent's workspace port. Covers slug→port resolution (host
vs peer, the alive check, the 1s TTL cache), the 409/502 error paths, and the
hop-by-hop header stripping on both the forwarded request and the response.
"""

from __future__ import annotations

import time

import httpx
import pytest

from graph.fleet import proxy


@pytest.fixture(autouse=True)
def _clear_cache():
    proxy._slug_cache.clear()
    yield
    proxy._slug_cache.clear()


class FakeRequest:
    def __init__(self, method="GET", headers=None, query=None, body=b""):
        self.method = method
        self.headers = headers or {}
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return self._body


class FakeUpstream:
    def __init__(self, status_code=200, headers=None, chunks=(b"data: x\n\n",)):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


class FakeClient:
    def __init__(self, upstream=None, raise_exc=None):
        self._upstream = upstream
        self._raise = raise_exc
        self.built = None

    def build_request(self, method, url, headers=None, content=None, params=None):
        self.built = {"method": method, "url": url, "headers": headers,
                      "content": content, "params": params}
        return object()  # opaque request handle

    async def send(self, req, stream=True):
        if self._raise:
            raise self._raise
        return self._upstream


# --- _target_for_slug -------------------------------------------------------

def test_host_slug_uses_active_port(monkeypatch):
    from runtime import state as state_mod
    monkeypatch.setattr(state_mod.STATE, "active_port", 7870, raising=False)
    assert proxy._target_for_slug("host") == ("http://127.0.0.1:7870", {})


def test_peer_slug_resolves_when_alive(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state",
                        lambda: {"alice": {"port": 7001, "pid": 42}})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})


def test_peer_slug_is_none_when_dead(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state",
                        lambda: {"alice": {"port": 7001, "pid": 42}})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: False)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("alice") is None


def test_unknown_slug_is_none(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("ghost") is None


def test_remote_slug_resolves_to_url_with_bearer(monkeypatch):
    """A REMOTE member resolves to its registered URL; its stored bearer rides as an
    Authorization override (the browser's header carries the HUB's token, not the remote's)."""
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: False)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug",
                        lambda slug: {"id": "ava-1a2b", "name": "ava",
                                      "url": "http://100.101.189.45:7871", "token": "sek"})
    assert proxy._target_for_slug("ava-1a2b") == (
        "http://100.101.189.45:7871", {"authorization": "Bearer sek"})


def test_remote_without_token_adds_no_header(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug",
                        lambda slug: {"id": "r1", "name": "r", "url": "http://h:1", "token": ""})
    assert proxy._target_for_slug("r1") == ("http://h:1", {})


def test_resolution_is_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def load():
        calls["n"] += 1
        return {"alice": {"port": 7001, "pid": 42}}

    monkeypatch.setattr(proxy.supervisor, "_load_state", load)
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})
    assert calls["n"] == 1  # second hit served from the 1s TTL cache


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state",
                        lambda: {"alice": {"port": 7001, "pid": 42}})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})
    proxy._slug_cache["alice"] = (("http://127.0.0.1:9999", {}), time.monotonic() - 2.0)  # stale
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})  # re-resolved


# --- forward_to -----------------------------------------------------------

async def test_forward_to_returns_409_when_not_running(monkeypatch):
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: None)
    resp = await proxy.forward_to("ghost", FakeRequest(), "api/x")
    assert resp.status_code == 409
    assert b"is not running" in resp.body


async def test_forward_to_delegates_to_target_when_running(monkeypatch):
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(base=base, path=path, extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    out = await proxy.forward_to("alice", FakeRequest(), "api/chat")
    assert out == "OK"
    assert seen == {"base": "http://127.0.0.1:7001", "path": "api/chat", "extra": {}}


# --- _forward_to_base -----------------------------------------------------

async def test_forward_strips_hop_headers_and_pipes_body(monkeypatch):
    up = FakeUpstream(
        status_code=200,
        headers={"content-type": "text/event-stream", "connection": "keep-alive"},
        chunks=(b"data: a\n\n", b"data: b\n\n"),
    )
    client = FakeClient(upstream=up)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)

    req = FakeRequest(
        method="POST",
        headers={"host": "hub", "connection": "keep-alive", "authorization": "Bearer t"},
        query={"stream": "1"},
        body=b"{}",
    )
    resp = await proxy._forward_to_base("http://127.0.0.1:7001", req, "api/chat")

    # request: hop-by-hop headers dropped, app headers + target preserved
    assert "host" not in client.built["headers"]
    assert "connection" not in client.built["headers"]
    assert client.built["headers"]["authorization"] == "Bearer t"
    assert client.built["url"] == "http://127.0.0.1:7001/api/chat"
    assert client.built["params"] == {"stream": "1"}
    assert client.built["method"] == "POST"

    # response: hop-by-hop dropped, status + content-type preserved, body piped, upstream closed
    assert resp.status_code == 200
    assert "connection" not in resp.headers
    assert resp.headers["content-type"] == "text/event-stream"
    chunks = [c async for c in resp.body_iterator]
    assert b"".join(chunks) == b"data: a\n\ndata: b\n\n"
    assert up.closed


async def test_forward_returns_502_on_connect_error(monkeypatch):
    client = FakeClient(raise_exc=httpx.ConnectError("refused"))
    monkeypatch.setattr(proxy, "_get_client", lambda: client)
    resp = await proxy._forward_to_base("http://127.0.0.1:7001", FakeRequest(), "api/x")
    assert resp.status_code == 502
    assert b"not reachable" in resp.body


# --- _get_client ----------------------------------------------------------

def test_get_client_is_pooled_and_recreated_when_closed():
    proxy._client = None
    c1 = proxy._get_client()
    assert proxy._get_client() is c1  # pooled
    import asyncio
    asyncio.run(c1.aclose())
    c2 = proxy._get_client()
    assert c2 is not c1  # recreated after close
    asyncio.run(c2.aclose())
