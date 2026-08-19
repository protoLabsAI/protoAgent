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

    def build_request(self, method, url, headers=None, content=None, params=None, timeout=None):
        self.built = {
            "method": method,
            "url": url,
            "headers": headers,
            "content": content,
            "params": params,
            "timeout": timeout,  # the #2590 read-timeout lane this request was placed in
        }
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
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {"alice": {"port": 7001, "pid": 42}})
    monkeypatch.setattr(proxy.supervisor, "_alive", lambda pid: True)
    monkeypatch.setattr(proxy.supervisor, "remote_for_slug", lambda slug: None)
    assert proxy._target_for_slug("alice") == ("http://127.0.0.1:7001", {})


def test_peer_slug_is_none_when_dead(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {"alice": {"port": 7001, "pid": 42}})
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
    monkeypatch.setattr(
        proxy.supervisor,
        "remote_for_slug",
        lambda slug: {"id": "ava-1a2b", "name": "ava", "url": "http://100.101.189.45:7871", "token": "sek"},
    )
    assert proxy._target_for_slug("ava-1a2b") == ("http://100.101.189.45:7871", {"authorization": "Bearer sek"})


def test_remote_without_token_adds_no_header(monkeypatch):
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(
        proxy.supervisor, "remote_for_slug", lambda slug: {"id": "r1", "name": "r", "url": "http://h:1", "token": ""}
    )
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
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {"alice": {"port": 7001, "pid": 42}})
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


async def test_forward_to_member_public_drops_stored_remote_bearer(monkeypatch):
    """#1890: a request the hub admitted off the MEMBER's public list arrived anonymous —
    forwarding it must NOT lend the remote's stored bearer to an unauthenticated caller."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        proxy, "_target_for_slug", lambda slug: ("http://remote:7870", {"authorization": "Bearer sekrit"})
    )
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    req = FakeRequest()
    req.state = SimpleNamespace(member_public=True)
    assert await proxy.forward_to("matt", req, "plugins/content/view") == "OK"
    assert seen["extra"] == {}


async def test_forward_to_authed_request_keeps_stored_remote_bearer(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        proxy, "_target_for_slug", lambda slug: ("http://remote:7870", {"authorization": "Bearer sekrit"})
    )
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    req = FakeRequest()
    req.state = SimpleNamespace()  # no member_public stamp — normal authed traffic
    assert await proxy.forward_to("matt", req, "api/chat") == "OK"
    assert seen["extra"] == {"authorization": "Bearer sekrit"}


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


# --- fleet service token swap (ADR 0089) ----------------------------------


async def test_forward_to_local_operator_swaps_in_fleet_token(monkeypatch):
    """The #2047/ADR-0089 fix: for a LOCAL peer, an operator caller's Authorization (e.g. a
    device token the member's own registry can't verify) is REPLACED with the fleet service
    token the member does accept."""
    from types import SimpleNamespace

    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-secret")
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    req = FakeRequest(headers={"Authorization": "Bearer device-token"})
    req.state = SimpleNamespace(trust_tier="operator")
    assert await proxy.forward_to("alice", req, "api/plugins/spacetraders/x") == "OK"
    assert seen["extra"] == {"authorization": "Bearer fleet-secret"}


async def test_forward_to_local_non_operator_gets_no_fleet_token(monkeypatch):
    """Never elevate a lesser credential: a non-operator tier is forwarded as-is."""
    from types import SimpleNamespace

    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    req = FakeRequest()
    req.state = SimpleNamespace(trust_tier="federation")
    await proxy.forward_to("alice", req, "a2a")
    assert seen["extra"] == {}


async def test_forward_to_local_member_public_gets_no_fleet_token(monkeypatch):
    """member_public wins over the swap: an anonymously-admitted request is forwarded
    anonymous, never lent the fleet token even at operator tier."""
    from types import SimpleNamespace

    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))
    seen = {}

    async def fake_fwd(base, request, path, extra=None):
        seen.update(extra=extra)
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    req = FakeRequest()
    req.state = SimpleNamespace(member_public=True, trust_tier="operator")
    await proxy.forward_to("matt", req, "plugins/content/view")
    assert seen["extra"] == {}


async def test_forward_base_swapped_auth_replaces_callers(monkeypatch):
    """The swapped Authorization REPLACES the caller's (case-insensitively) — the upstream
    never carries two, and unrelated headers survive."""
    up = FakeUpstream()
    client = FakeClient(upstream=up)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)
    req = FakeRequest(headers={"Authorization": "Bearer caller", "X-Keep": "1"})
    await proxy._forward_to_base("http://h:1", req, "api/x", {"authorization": "Bearer fleet"})
    sent = client.built["headers"]
    auth_keys = [k for k in sent if k.lower() == "authorization"]
    assert len(auth_keys) == 1 and sent[auth_keys[0]] == "Bearer fleet"
    assert sent.get("X-Keep") == "1"


# --- #2590: a stalled member must not park the connection -------------------


def test_stream_paths_stay_unbounded():
    """A2A + the SSE event feed idle between frames by design. Bounding them would cut
    live member chat mid-turn — and the console streams A2A over `fetch`, which sends NO
    `Accept: text/event-stream`, so the path list (not the header) is what saves it."""
    req = FakeRequest(headers={})
    assert proxy._timeout_for(req, "a2a").read is None
    assert proxy._timeout_for(req, "api/events").read is None
    assert proxy._timeout_for(req, "/a2a/").read is None  # normalized


def test_accept_header_also_selects_the_stream_lane():
    """A plugin's own well-behaved SSE endpoint isn't on the path list, so the header is
    honored as a second signal."""
    req = FakeRequest(headers={"accept": "text/event-stream"})
    assert proxy._timeout_for(req, "plugins/thing/feed").read is None


def test_turn_path_gets_the_long_lane_not_the_view_lane():
    """The desktop fallback POSTs a whole agent turn to /api/chat — minutes, not seconds."""
    t = proxy._timeout_for(FakeRequest(method="POST"), "api/chat")
    assert t.read == proxy._TURN_TIMEOUT.read and t.read >= 300


def test_plugin_view_reads_are_bounded():
    """The traffic that actually wedged the console: board view fetches + its ~3s progress
    poll. These must be bounded or six of them exhaust the browser's per-origin cap."""
    t = proxy._timeout_for(FakeRequest(), "plugins/project_board/api/features")
    assert t.read == proxy._READ_TIMEOUT.read and 0 < t.read <= 60


async def test_forward_passes_the_lane_to_the_request(monkeypatch):
    client = FakeClient(upstream=FakeUpstream())
    monkeypatch.setattr(proxy, "_get_client", lambda: client)
    await proxy._forward_to_base("http://127.0.0.1:7001", FakeRequest(), "plugins/x/api/y")
    assert client.built["timeout"] is proxy._READ_TIMEOUT  # not the unbounded default


async def test_read_timeout_returns_504_instead_of_hanging(monkeypatch):
    client = FakeClient(raise_exc=httpx.ReadTimeout("stalled"))
    monkeypatch.setattr(proxy, "_get_client", lambda: client)
    resp = await proxy._forward_to_base("http://127.0.0.1:7001", FakeRequest(), "plugins/x/api/y")
    assert resp.status_code == 504


async def test_a_stalled_member_returns_rather_than_parking_the_socket(monkeypatch):
    """The acceptance criterion, against a REAL socket: a stub that accepts the connection
    and never writes a byte. Before the fix this awaited forever."""
    import asyncio

    stop = asyncio.Event()

    async def _never_responds(reader, writer):
        await reader.read(65536)  # consume the request, then go silent — never write
        await stop.wait()  # held open until the test releases it, NOT a fixed sleep
        writer.close()  # without this, wait_closed() below blocks on the live connection

    server = await asyncio.start_server(_never_responds, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(proxy, "_READ_TIMEOUT", httpx.Timeout(0.5, connect=5.0))
    try:
        resp = await asyncio.wait_for(
            proxy._forward_to_base(f"http://127.0.0.1:{port}", FakeRequest(), "plugins/x/api/y"),
            timeout=10,  # the test itself must not hang if the fix regresses
        )
        assert resp.status_code == 504  # answered, connection released
    finally:
        stop.set()  # release the handler BEFORE closing, or wait_closed() blocks on it
        server.close()
        await server.wait_closed()


# ── Swap & Resume S4: SSE keepalive + turn-touch ───────────────────────────────


@pytest.mark.asyncio
async def test_sse_pipe_emits_keepalive_on_upstream_silence(monkeypatch):
    """An abandoned proxied stream used to park until the member's next write —
    indefinitely for a silent tool call. The SSE pipe now writes a comment
    keepalive after idle, so a dead client raises on the write and the pipe
    (and the member-side connection) unwinds within one keepalive period."""
    import asyncio

    monkeypatch.setattr(proxy, "_SSE_KEEPALIVE_S", 0.05)

    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_raw(self):
            yield b"data: one\n\n"
            await asyncio.sleep(0.2)  # silence >> keepalive interval
            yield b"data: two\n\n"

        async def aclose(self):
            pass

    class FakeClient:
        def build_request(self, *a, **k):
            return "req"

        async def send(self, req, stream=True):
            return FakeUpstream()

    monkeypatch.setattr(proxy, "_get_client", lambda: FakeClient())
    resp = await proxy._forward_to_base("http://127.0.0.1:7001", FakeRequest(), "a2a")
    chunks = [c async for c in resp.body_iterator]
    joined = b"".join(chunks)
    assert b"data: one" in joined and b"data: two" in joined
    assert b": keepalive" in joined  # emitted during the silent gap
    # ordering: keepalive lands between the two data frames
    assert joined.index(b"data: one") < joined.index(b": keepalive") < joined.index(b"data: two")


@pytest.mark.asyncio
async def test_non_sse_pipe_never_injects_keepalive(monkeypatch):
    monkeypatch.setattr(proxy, "_SSE_KEEPALIVE_S", 0.05)

    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aiter_raw(self):
            import asyncio

            yield b'{"a":'
            await asyncio.sleep(0.15)
            yield b"1}"

        async def aclose(self):
            pass

    class FakeClient:
        def build_request(self, *a, **k):
            return "req"

        async def send(self, req, stream=True):
            return FakeUpstream()

    monkeypatch.setattr(proxy, "_get_client", lambda: FakeClient())
    resp = await proxy._forward_to_base("http://127.0.0.1:7001", FakeRequest(), "api/x")
    joined = b"".join([c async for c in resp.body_iterator])
    assert joined == b'{"a":1}'  # byte-exact: comments would corrupt a JSON body


@pytest.mark.asyncio
async def test_member_turn_start_touches_recency(monkeypatch):
    """S4: a POST /a2a through the proxy refreshes the member's LRU recency, so
    the warm-cap grace window tracks agents that are WORKING, not just clicked."""
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://127.0.0.1:7001", {}))
    touched = []
    monkeypatch.setattr(proxy.supervisor, "touch", lambda slug: touched.append(slug))

    async def fake_fwd(base, request, path, extra=None):
        return "OK"

    monkeypatch.setattr(proxy, "_forward_to_base", fake_fwd)
    await proxy.forward_to("ava", FakeRequest(method="POST"), "a2a")
    assert touched == ["ava"]
    await proxy.forward_to("ava", FakeRequest(method="GET"), "api/tools")
    assert touched == ["ava"]  # non-turn traffic doesn't churn LRU order
