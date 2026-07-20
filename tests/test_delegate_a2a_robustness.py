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


# ── #1778: the synchronous SendMessage read follows poll_timeout, not a flat 60s ──


def _capture_client_timeout(patched, **fake_kw):
    """Install a fake AsyncClient that records the httpx timeout it was built with."""
    captured: dict = {}

    def _client(**client_kw):
        captured["timeout"] = client_kw.get("timeout")
        return _FakeClient(**fake_kw, **client_kw)

    patched.setattr(httpx, "AsyncClient", _client)
    return captured


def test_sync_read_timeout_tracks_poll_timeout(patched):
    """A synchronous A2A peer holds the SendMessage connection open for the whole turn,
    so the read budget must be poll_timeout_s — NOT the old flat 60s that hard-failed
    every member turn >60s (#1778). Connect stays short so unreachable peers still fail fast."""
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "ok" if result else "")
    cap = _capture_client_timeout(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "ok"}}))
    d = _parse(poll_timeout_s=180)

    assert asyncio.run(A.dispatch(d, "hi")) == "ok"
    t = cap["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.read == 180.0  # the turn budget, not 60
    assert t.connect == 10.0  # unreachable peers still fail fast


def test_explicit_timeout_overrides_read_budget(patched):
    """An explicit per-call timeout still wins over poll_timeout_s for the read budget."""
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "ok" if result else "")
    cap = _capture_client_timeout(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "ok"}}))
    d = _parse(poll_timeout_s=180)

    assert asyncio.run(A.dispatch(d, "hi", timeout=25)) == "ok"
    assert cap["timeout"].read == 25.0


# ── fleet tracing: outbound a2a.trace propagation ──────────────────────────────


class _BodyCaptureClient(_FakeClient):
    """A _FakeClient that also records every posted JSON-RPC body."""

    bodies: list  # class attr replaced per-install

    async def post(self, url, json=None, headers=None):
        type(self).bodies.append(json)
        return await super().post(url, json=json, headers=headers)


def _install_capture_client(monkeypatch, **kw):
    class _C(_BodyCaptureClient):
        bodies = []

    monkeypatch.setattr(httpx, "AsyncClient", lambda **client_kw: _C(**kw, **client_kw))
    return _C.bodies


_TID = "a" * 32
_SID = "b" * 16

# Patch the SAME module object dispatch resolves via `from observability import
# tracing` (the package attribute — stable even if a sibling test swapped the
# sys.modules entry).
from observability import tracing as _tracing  # noqa: E402


def test_dispatch_attaches_a2a_trace_when_tracing_active(patched):
    """When a traced turn dispatches to a peer, the SendMessage carries our
    Langfuse trace context as ``a2a.trace`` metadata — camelCase traceId/spanId,
    the exact shape a2a_impl/executor._extract_caller_trace reads — at BOTH
    request level (preferred) and message level (fallback)."""
    patched.setattr(_tracing, "current_trace_context", lambda: {"trace_id": _TID, "span_id": _SID})
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    bodies = _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    wire = {"traceId": _TID, "spanId": _SID}
    assert send["params"]["metadata"] == {"a2a.trace": wire}
    assert send["params"]["message"]["metadata"] == {"a2a.trace": wire}


def test_dispatch_attaches_trace_id_only_when_no_current_span(patched):
    patched.setattr(_tracing, "current_trace_context", lambda: {"trace_id": _TID})
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    bodies = _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    assert send["params"]["metadata"] == {"a2a.trace": {"traceId": _TID}}


def test_dispatch_sends_no_trace_metadata_when_tracing_inactive(patched):
    """Tracing off ⇒ the request is unchanged — no metadata keys at all."""
    patched.setattr(_tracing, "current_trace_context", lambda: None)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    bodies = _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    assert "metadata" not in send["params"]
    assert "metadata" not in send["params"]["message"]


def test_dispatch_survives_tracing_helper_blowup(patched):
    """A tracing failure must never break a dispatch."""

    def _boom():
        raise RuntimeError("tracing exploded")

    patched.setattr(_tracing, "current_trace_context", _boom)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"


# ── ADR 0089 D4: fleet service token for a loopback (in-instance) delegate ──────


@pytest.fixture(autouse=True)
def _isolate_fleet_token(tmp_path, monkeypatch):
    """Keep the fleet-token resolution (now reached by every loopback dispatch) off the real
    instance root, and clear its process cache so each test starts clean."""
    import graph.fleet.service_token as _st

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.delenv(_st.ENV_VAR, raising=False)
    monkeypatch.setattr(_st, "_cached", [None])
    yield


def _install_header_capture(monkeypatch, **kw):
    """A fake AsyncClient that records the headers of the last POST (the SendMessage)."""

    class _C(_FakeClient):
        seen: dict = {}

        async def post(self, url, json=None, headers=None):
            _C.seen["headers"] = headers
            return await super().post(url, json=json, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **client_kw: _C(**kw, **client_kw))
    return _C.seen


def test_dispatch_loopback_delegate_attaches_fleet_token(patched, monkeypatch):
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-abc")
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    seen = _install_header_capture(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))
    d = _parse()  # url is http://127.0.0.1:9/a2a — loopback, no auth_token
    assert asyncio.run(A.dispatch(d, "ping")) == "pong"
    assert seen["headers"]["Authorization"] == "Bearer fleet-abc"


def test_dispatch_remote_delegate_gets_no_fleet_token(patched, monkeypatch):
    """The fleet token never leaves the box: an off-box (non-loopback) tokenless delegate
    dispatches unauthenticated, exactly as before."""
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-abc")
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    seen = _install_header_capture(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))
    d = _parse(url="http://100.1.2.3:7870/a2a")
    assert asyncio.run(A.dispatch(d, "ping")) == "pong"
    assert "Authorization" not in seen["headers"]


def test_dispatch_explicit_token_wins_over_fleet(patched, monkeypatch):
    """A delegate with its own configured token keeps it — the fleet fallback is elif-gated."""
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-abc")
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    seen = _install_header_capture(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))
    d = _parse(auth={"scheme": "bearer", "token": "sekret"})  # loopback, but explicit token
    assert asyncio.run(A.dispatch(d, "ping")) == "pong"
    assert seen["headers"]["Authorization"] == "Bearer sekret"
