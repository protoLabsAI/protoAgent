"""A2A delegate robustness — configurable poll timeout + error transparency.

The old dispatch hard-capped polling at 30s (long delegated tasks were cut off
mid-flight) and surfaced opaque errors. These cover the configurable
``poll_timeout_s`` and the legible cause mapping (unreachable / timed-out /
version-incompatible).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time as _time

import httpx
import pytest

from plugins.delegates.adapters import (
    A2aAdapter,
    Delegate,
    DelegateError,
    _a2a_error_detail,
    _a2a_progress_fingerprint,
    _warn_if_suspiciously_short,
)
from tools.a2a_parse import (
    ANSWER_BARE_MESSAGE,
    ANSWER_COMPLETED,
    ANSWER_FAILED,
    ANSWER_INPUT_REQUIRED,
    ANSWER_PENDING,
    classify_answer,
    state_name,
)

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


def test_error_detail_keeps_code_and_data():
    """`-32603 "Internal error"` is the generic JSON-RPC code and says nothing on its
    own; the cause rides in `data`. Dropping it left the operator with two useless
    words."""
    d = Delegate(name="peer", type="a2a")
    msg = _a2a_error_detail(
        d, {"code": -32603, "message": "Internal error", "data": {"detail": "KeyError: session_id"}}
    )
    assert "peer" in msg
    assert "Internal error" in msg
    assert "-32603" in msg
    assert "KeyError: session_id" in msg


def test_error_detail_handles_missing_pieces():
    """A peer may send no `data`, no `message`, or a non-dict error entirely — the
    formatter has to stay legible rather than emit a dangling separator or raise."""
    d = Delegate(name="peer", type="a2a")
    assert _a2a_error_detail(d, {"code": -1, "message": "boom", "data": None}).endswith("boom (JSON-RPC -1)")
    assert "(no message)" in _a2a_error_detail(d, {"code": -32603})
    assert "just a string" in _a2a_error_detail(d, "just a string")
    # A peer echoing a huge body can't flood the delegating agent's context.
    long = _a2a_error_detail(d, {"code": -1, "message": "x", "data": "y" * 10_000})
    assert len(long) < 2500


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


class _QueueClient(_FakeClient):
    """A fake client whose GetTask responses advance through a finite script."""

    def __init__(self, *, get_resps=None, **kw):
        super().__init__(**kw)
        self.get_resps = list(get_resps or [])

    async def post(self, url, json=None, headers=None):
        method = (json or {}).get("method")
        if method == "GetTask" and self.get_resps:
            self.posts += 1
            return self.get_resps.pop(0)
        return await super().post(url, json=json, headers=headers)


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


def _task_resp(*, state="TASK_STATE_WORKING", task_id="t1", context_id=None, text=None):
    task = {"id": task_id, "status": {"state": state}}
    if context_id:
        task["contextId"] = context_id
    if text:
        task["artifacts"] = [{"parts": [{"text": text}]}]
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _clock(monkeypatch, step=0.3):
    """A fake ``time.monotonic`` that ADVANCES by ``step`` on every read.

    It must never stop advancing. The adapter reads the clock an
    implementation-defined number of times per dispatch — ``_rpc_tracked`` alone reads
    it twice per RPC — so a script that freezes on a final value can leave the poll
    deadline permanently ahead of the clock. Combined with the ``patched`` fixture's
    no-op ``asyncio.sleep``, that is not a slow test but an infinite spin (it timed out
    a 15-minute CI job). An always-increasing clock guarantees every deadline is
    eventually crossed, so a miscounted read fails the assertion instead of hanging.

    Returns the list of values handed out, so a test can assert on elapsed time."""
    reads: list[float] = []

    def _monotonic() -> float:
        reads.append(step * len(reads))
        return reads[-1]

    monkeypatch.setattr(_time, "monotonic", _monotonic)
    return reads


def test_progress_fingerprint_distinguishes_material_task_advancement():
    working = _task_resp(state="TASK_STATE_WORKING").json()["result"]
    same_working = _task_resp(state="TASK_STATE_WORKING").json()["result"]
    advanced = _task_resp(state="TASK_STATE_WORKING", text="built wheels").json()["result"]

    assert _a2a_progress_fingerprint(working) == _a2a_progress_fingerprint(same_working)
    assert _a2a_progress_fingerprint(working) != _a2a_progress_fingerprint(advanced)


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


def test_identical_working_polls_timeout_without_a_second_send(patched):
    patched.setattr("tools.a2a_parse._extract_text", lambda *_a, **_k: "")
    _clock(patched, step=0.3)
    running = _task_resp(state="TASK_STATE_WORKING")
    bodies = _install_capture_client(patched, send_resp=running, get_resp=running)

    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=1), "hi"))

    assert "without observable progress" in str(ei.value)
    methods = [b.get("method") for b in bodies]
    # The invariant that matters: a heartbeat that never changes is not progress, so the
    # bound still trips — and the timeout opens NO second task (room_rounds._dropped).
    assert methods.count("SendMessage") == 1
    assert methods.count("GetTask") >= 1


def test_material_progress_resets_poll_timeout_until_completion(patched):
    reads = _clock(patched, step=1.0)
    bodies = _install_capture_client(
        patched,
        send_resp=_task_resp(state="TASK_STATE_WORKING"),
        get_resps=[
            _task_resp(state="TASK_STATE_WORKING", text="built wheels"),
            _task_resp(state="TASK_STATE_COMPLETED", text="done"),
        ],
    )

    assert asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi")) == "done"
    methods = [b.get("method") for b in bodies]
    assert methods.count("SendMessage") == 1
    assert methods.count("GetTask") == 2
    # The point of the change: an advancing task runs past the nominal poll_timeout_s
    # total and still returns its answer, because each material observation reset the
    # inactivity deadline rather than the whole turn being capped at 10s.
    assert reads[-1] - reads[0] >= 10


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


class _BodyCaptureClient(_QueueClient):
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
    expected = {"origin": "a2a", "trigger": "delegate_to", "a2a.trace": wire}
    assert send["params"]["metadata"] == expected
    assert send["params"]["message"]["metadata"] == expected


def test_dispatch_attaches_trace_id_only_when_no_current_span(patched):
    patched.setattr(_tracing, "current_trace_context", lambda: {"trace_id": _TID})
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    bodies = _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    expected = {"origin": "a2a", "trigger": "delegate_to", "a2a.trace": {"traceId": _TID}}
    assert send["params"]["metadata"] == expected
    assert send["params"]["message"]["metadata"] == expected


def test_dispatch_stamps_a2a_provenance_when_tracing_inactive(patched):
    """Tracing off still identifies a protoAgent peer delegation at both levels."""
    patched.setattr(_tracing, "current_trace_context", lambda: None)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    bodies = _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    expected = {"origin": "a2a", "trigger": "delegate_to"}
    assert send["params"]["metadata"] == expected
    assert send["params"]["message"]["metadata"] == expected


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


# ── outbound boundary span (cross-agent latency on the CALLER's side) ──────────


def _record_spans(monkeypatch) -> list[tuple[str, dict, str]]:
    """Replace trace_span with a recorder, preserving its context-manager contract."""
    import contextlib

    seen: list[tuple[str, dict, str]] = []

    @contextlib.contextmanager
    def _span(name, metadata=None, as_type="span"):
        seen.append((name, metadata or {}, as_type))
        yield None

    monkeypatch.setattr(_tracing, "trace_span", _span)
    return seen


def test_dispatch_opens_an_outbound_span_named_for_the_delegate(patched):
    """Without this span a delegation is invisible caller-side except as the
    enclosing ``tool:delegate_to``, so "the peer was slow" and "we were slow
    calling it" are indistinguishable — the first question of any fleet trace."""
    spans = _record_spans(patched)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    assert len(spans) == 1, f"expected exactly one outbound span, got {spans}"
    name, meta, as_type = spans[0]
    assert name == "a2a:peer"
    assert as_type == "agent"
    assert meta["url"] == "http://127.0.0.1:9/a2a"
    assert meta["delegate"] == "peer"


def test_outbound_span_opens_before_the_trace_context_is_read(patched):
    """Ordering is load-bearing: the peer must nest under the DISPATCH span, not
    the enclosing turn, or a delegation chain renders as a flat list of sibling
    agents instead of a call tree. Pins that the context is read inside the span.
    """
    order: list[str] = []

    import contextlib

    @contextlib.contextmanager
    def _span(name, metadata=None, as_type="span"):
        order.append("span-open")
        yield None
        order.append("span-close")

    patched.setattr(_tracing, "trace_span", _span)

    def _ctx():
        order.append("read-trace-context")
        return {"trace_id": _TID, "span_id": _SID}

    patched.setattr(_tracing, "current_trace_context", _ctx)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "pong" if result else "")
    _install_capture_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "pong"}}))

    assert asyncio.run(A.dispatch(_parse(), "ping")) == "pong"

    assert order == ["span-open", "read-trace-context", "span-close"], order


def test_outbound_span_closes_even_when_the_dispatch_fails(patched):
    """A failed hop is the one you most want on the trace — the span must still
    close (and the DelegateError must propagate unchanged)."""
    closed: list[str] = []

    import contextlib

    @contextlib.contextmanager
    def _span(name, metadata=None, as_type="span"):
        try:
            yield None
        finally:
            closed.append(name)

    patched.setattr(_tracing, "trace_span", _span)
    _install_client(patched, raise_exc=httpx.ConnectError("refused"))

    with pytest.raises(DelegateError):
        asyncio.run(A.dispatch(_parse(), "ping"))

    assert closed == ["a2a:peer"]


# ── the GetTask wire shape + input-required convergence ────────────────────────


def test_gettask_poll_uses_the_a2a_10_id_param(patched):
    """A2A 1.0 GetTaskRequest is {tenant, id, history_length} — the v0.3 legacy
    {"name": …} shape never worked against a 1.0 peer, so the poll loop could
    not converge for an async-style delegate (latent behind protoAgent peers
    answering SendMessage inline). Pin the wire shape."""
    working = {"jsonrpc": "2.0", "result": {"task": {"id": "t-9", "status": {"state": "TASK_STATE_WORKING"}}}}
    done = {
        "jsonrpc": "2.0",
        "result": {
            "task": {
                "id": "t-9",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "peer answer"}]}],
            }
        },
    }
    bodies = _install_capture_client(patched, send_resp=_Resp(working), get_resp=_Resp(done))

    assert asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "do the thing")) == "peer answer"

    gettask = next(b for b in bodies if b.get("method") == "GetTask")
    assert gettask["params"] == {"id": "t-9"}, f"1.0 GetTask must send id, got {gettask['params']}"


def _parked_task(question="Which repo should I use?"):
    return {
        "jsonrpc": "2.0",
        "result": {
            "task": {
                "id": "t-9",
                "contextId": "ctx-7",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"parts": [{"text": question}]},
                },
            }
        },
    }


def test_input_required_returns_the_question_with_a_resume_handle(patched):
    """The HITL delegation chain (operator decision, 2026-08-20): a parked peer's
    QUESTION comes back to the calling agent as a tool RESULT carrying the parked
    task id and resume instructions — never buried in an error, never polled to
    the deadline. Escalation is emergent: a caller that can't answer asks ITS
    caller the same way."""
    working = {"jsonrpc": "2.0", "result": {"task": {"id": "t-9", "status": {"state": "TASK_STATE_WORKING"}}}}
    bodies = _install_capture_client(patched, send_resp=_Resp(working), get_resp=_Resp(_parked_task()))

    out = asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "do the thing"))
    assert "needs input" in out
    assert "Which repo should I use?" in out
    assert "resume_task_id='t-9'" in out
    # fails fast: exactly one GetTask observed the park — no poll-to-deadline
    assert [b.get("method") for b in bodies].count("GetTask") == 1


def test_resume_sends_the_answer_into_the_parked_task(patched):
    """resume_task_id routes the caller's answer back into the parked task:
    GetTask first (honest state + contextId), then SendMessage carrying the
    parked taskId + contextId — the wire shape the handler's resume tests pin."""
    done = {
        "jsonrpc": "2.0",
        "result": {
            "task": {
                "id": "t-9",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "resumed and finished"}]}],
            }
        },
    }
    bodies = _install_capture_client(patched, send_resp=_Resp(done), get_resp=_Resp(_parked_task()))

    out = asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "use the protoAgent repo", resume_task_id="t-9"))
    assert out == "resumed and finished"
    methods = [b.get("method") for b in bodies]
    assert methods == ["GetTask", "SendMessage"]  # state check, then the answer
    send = bodies[1]["params"]["message"]
    assert send["taskId"] == "t-9"
    assert send["contextId"] == "ctx-7"
    assert send["parts"] == [{"text": "use the protoAgent repo"}]


def test_resume_of_a_finished_task_reports_instead_of_resending(patched):
    finished = {
        "jsonrpc": "2.0",
        "result": {
            "task": {
                "id": "t-9",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "already done answer"}]}],
            }
        },
    }
    bodies = _install_capture_client(patched, send_resp=_Resp(finished), get_resp=_Resp(finished))

    out = asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "the answer", resume_task_id="t-9"))
    assert "already finished" in out and "already done answer" in out
    assert [b.get("method") for b in bodies] == ["GetTask"]  # no answer sent into a done task


def test_resume_of_a_running_task_refuses_legibly(patched):
    running = {"jsonrpc": "2.0", "result": {"task": {"id": "t-9", "status": {"state": "TASK_STATE_WORKING"}}}}
    _install_capture_client(patched, send_resp=_Resp(running), get_resp=_Resp(running))

    with pytest.raises(DelegateError, match="not parked for input"):
        asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "the answer", resume_task_id="t-9"))


def test_resume_toward_a_non_a2a_delegate_is_refused(patched):
    """A resume id on an openai/acp delegate would silently run the ANSWER as a
    brand-new task (managed-git coder: a brand-new PR). The registry refuses."""
    from types import SimpleNamespace

    from plugins.delegates.registry import DelegateRegistry

    reg = DelegateRegistry.__new__(DelegateRegistry)
    reg._items = {"gpt": SimpleNamespace(name="gpt", type="openai", manage_git=False)}

    with pytest.raises(DelegateError, match="resume_task_id only applies to a2a"):
        asyncio.run(reg.dispatch("gpt", "the answer", resume_task_id="t-9"))


def test_inline_park_is_not_mistaken_for_an_answer(patched):
    """A synchronous peer (protoAgent itself) answers SendMessage INLINE with the
    task already parked — its question in status.message. The text early-return
    must not hand that question back as if it were the delegate's ANSWER (it
    would lose the task id and the whole resume protocol). Caught by the live
    smoke on 2026-08-20; the poll-path park test alone missed it."""
    inline_parked = {
        "jsonrpc": "2.0",
        "result": {
            "task": {
                "id": "t-9",
                "contextId": "ctx-7",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"parts": [{"text": "Which repo should I use?"}]},
                },
            }
        },
    }
    bodies = _install_capture_client(patched, send_resp=_Resp(inline_parked), get_resp=_Resp(inline_parked))

    out = asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "do the thing"))
    assert "needs input" in out
    assert "Which repo should I use?" in out
    assert "resume_task_id='t-9'" in out
    # the park was visible inline — no GetTask round-trips at all
    assert [b.get("method") for b in bodies] == ["SendMessage"]


# ── #3085: suspiciously-short-reply diagnostic ─────────────────────────────────
#
# The live failure: a background delegation returned a 416-char reply that ended
# mid-sentence — narration, not the answer. The dispatch path logs a WARNING when a
# short reply comes back after a non-trivial wait, so the next occurrence leaves a
# breadcrumb. It only logs; it never alters the returned text.

_DELEGATE_LOGGER = "protoagent.plugins.delegates"


def test_warn_helper_fires_for_a_short_reply_after_a_wait(caplog):
    """A <500-char reply after the dispatch has run past the elapsed floor smells of a
    truncated / partial-narration answer — the helper says so, once, at WARNING."""
    with caplog.at_level(logging.WARNING, logger=_DELEGATE_LOGGER):
        _warn_if_suspiciously_short("protoEngineer", "x" * 416, elapsed_s=30.0)
    assert any("#3085" in m and "protoEngineer" in m and "416 chars" in m for m in caplog.messages)


def test_warn_helper_is_quiet_for_a_full_length_reply(caplog):
    """A full answer is exactly what we want — no warning even after a long dispatch."""
    with caplog.at_level(logging.WARNING, logger=_DELEGATE_LOGGER):
        _warn_if_suspiciously_short("protoEngineer", "x" * 600, elapsed_s=30.0)
    assert caplog.messages == []


def test_warn_helper_is_quiet_for_a_fast_terse_reply(caplog):
    """A short reply that came back instantly is a normal terse answer, not a truncation
    — the elapsed floor keeps it quiet."""
    with caplog.at_level(logging.WARNING, logger=_DELEGATE_LOGGER):
        _warn_if_suspiciously_short("protoEngineer", "ok", elapsed_s=0.2)
    assert caplog.messages == []


def test_dispatch_warns_on_a_short_reply_but_returns_it_unchanged(patched, caplog, monkeypatch):
    """End to end: the dispatch path flags a suspiciously short reply (elapsed floor
    neutralised so the fast fake client trips it) yet still returns the text verbatim —
    the diagnostic is post-hoc, never a behavior change (#3085)."""
    monkeypatch.setattr("plugins.delegates.adapters._SHORT_REPLY_MIN_ELAPSED_S", 0.0)
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "narration only" if result else "")
    _install_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": "narration only"}}))
    with caplog.at_level(logging.WARNING, logger=_DELEGATE_LOGGER):
        out = asyncio.run(A.dispatch(_parse(), "do the whole thing"))
    assert out == "narration only"  # returned untouched
    assert any("#3085" in m for m in caplog.messages)


def test_dispatch_does_not_warn_on_a_full_length_reply(patched, caplog, monkeypatch):
    """The same path stays silent when the peer returns a full answer, even with the
    elapsed floor neutralised — the trigger is length, not merely slowness."""
    monkeypatch.setattr("plugins.delegates.adapters._SHORT_REPLY_MIN_ELAPSED_S", 0.0)
    long = "x" * 600
    patched.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: long if result else "")
    _install_client(patched, send_resp=_Resp({"jsonrpc": "2.0", "result": {"text": long}}))
    with caplog.at_level(logging.WARNING, logger=_DELEGATE_LOGGER):
        out = asyncio.run(A.dispatch(_parse(), "do the whole thing"))
    assert out == long
    assert not any("#3085" in m for m in caplog.messages)


# ── #3362: answer eligibility — a result's text is an answer only from a COMPLETED task ─
#
# ``_extract_text`` reads whatever text a result carries; whether that text is the
# delegate's ANSWER is a SEPARATE question, decided by ``classify_answer`` off the task
# state. A WORKING task's status message and a FAILED task's error message are text the
# adapter must never hand back as the reply. ``_is_terminal`` stays a poll-STOP predicate
# only — never an answer-eligibility predicate.


def _completed_resp(text="the answer", *, task_id="t1", context_id=None):
    task = {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"text": text}]}]}
    if context_id:
        task["contextId"] = context_id
    return _Resp({"jsonrpc": "2.0", "result": {"task": task}})


def _status_resp(state, *, task_id="t1", msg_text=None):
    status: dict = {"state": state}
    if msg_text is not None:
        status["message"] = {"parts": [{"text": msg_text}]}
    return _Resp({"jsonrpc": "2.0", "result": {"task": {"id": task_id, "status": status}}})


# the pure classifier ----------------------------------------------------------


def test_classifier_completed_task_is_answerable():
    v = classify_answer(
        {"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"text": "a"}]}]}}
    )
    assert v.kind == ANSWER_COMPLETED and v.completed and v.answerable


def test_classifier_legacy_completed_spelling_is_answerable():
    """v0.3 lowercase ``completed`` must classify the same as 1.0 ``TASK_STATE_COMPLETED``."""
    assert classify_answer({"id": "t", "status": {"state": "completed"}}).completed


def test_classifier_working_task_with_status_text_is_pending_not_answerable():
    """The core bug: a WORKING task's status message is progress narration, NOT the answer."""
    v = classify_answer(
        {
            "task": {
                "id": "t1",
                "status": {"state": "TASK_STATE_WORKING", "message": {"parts": [{"text": "working on it"}]}},
            }
        }
    )
    assert v.kind == ANSWER_PENDING and not v.answerable and not v.completed


@pytest.mark.parametrize(
    "state,word",
    [
        ("TASK_STATE_FAILED", "failed"),
        ("TASK_STATE_CANCELED", "canceled"),
        ("TASK_STATE_CANCELLED", "canceled"),
        ("TASK_STATE_REJECTED", "rejected"),
        ("canceled", "canceled"),  # v0.3 spelling
    ],
)
def test_classifier_failure_states_are_diagnostics_not_answers(state, word):
    v = classify_answer({"id": "t", "status": {"state": state}})
    assert v.kind == ANSWER_FAILED and v.failed and not v.answerable, state
    assert state_name(state) == word


def test_classifier_input_required_is_a_park():
    v = classify_answer(
        {"task": {"status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"text": "which?"}]}}}}
    )
    assert v.kind == ANSWER_INPUT_REQUIRED and v.input_required and not v.answerable


def test_classifier_bare_message_stays_compatible():
    """A genuine bare Message (no task envelope) is answerable — pre-task compatibility.
    A status-less reply that merely carries artifacts degrades to the same path."""
    assert classify_answer({"parts": [{"text": "hi"}], "messageId": "m1", "role": "ROLE_AGENT"}).kind == ANSWER_BARE_MESSAGE
    assert classify_answer({"artifacts": [{"parts": [{"text": "hi"}]}]}).answerable


def test_classifier_task_envelope_without_state_is_not_a_bare_message():
    """r6: a ``{"task": …}`` envelope with text but no usable state must NOT be treated as
    a bare Message just because it carries text — it is pending, never an answer."""
    v = classify_answer({"task": {"id": "t1", "artifacts": [{"parts": [{"text": "partial"}]}]}})
    assert v.kind == ANSWER_PENDING and not v.answerable


def test_state_name_normalizes_for_diagnostics():
    assert state_name("TASK_STATE_COMPLETED") == "completed"
    assert state_name("TASK_STATE_FAILED") == "failed"
    assert state_name("TASK_STATE_CANCELLED") == "canceled"
    assert state_name("input-required") == "input_required"
    assert state_name(None) == "unknown"


# the dispatch paths -----------------------------------------------------------


def test_inline_working_status_text_is_not_returned_a_later_completed_is(patched):
    """r1: a WORKING task whose status message carries narration is never returned as the
    answer — the poll continues and the subsequent COMPLETED result is what comes back."""
    working = _status_resp("TASK_STATE_WORKING", msg_text="starting the migration…")
    bodies = _install_capture_client(patched, send_resp=working, get_resps=[_completed_resp("the real answer")])

    out = asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi"))

    assert out == "the real answer"
    methods = [b.get("method") for b in bodies]
    assert methods.count("SendMessage") == 1 and methods.count("GetTask") >= 1


def test_completed_artifact_text_is_returned(patched):
    """r2: a terminal COMPLETED task's artifact text returns successfully."""
    _install_client(patched, send_resp=_completed_resp("done and dusted"))
    assert asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi")) == "done and dusted"


def test_completed_status_message_only_text_is_returned(patched):
    """r2: a COMPLETED task carrying its text only on the status message (no artifact) is
    still a valid answer."""
    _install_client(patched, send_resp=_status_resp("TASK_STATE_COMPLETED", msg_text="answer via status"))
    assert asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi")) == "answer via status"


def test_failed_task_raises_a_state_bearing_diagnostic_never_returns_its_text(patched):
    """r3: a FAILED task's status message is the peer's error, not the reply. Raise a
    normalized state-bearing DelegateError carrying a bounded slice of the diagnostic."""
    _install_client(patched, send_resp=_status_resp("TASK_STATE_FAILED", msg_text="OOM killed the worker"))
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi"))
    msg = str(ei.value)
    assert "failed" in msg and "state=TASK_STATE_FAILED" in msg and "OOM killed the worker" in msg


@pytest.mark.parametrize(
    "state,word",
    [("TASK_STATE_CANCELED", "canceled"), ("TASK_STATE_CANCELLED", "canceled"), ("TASK_STATE_REJECTED", "rejected")],
)
def test_canceled_and_rejected_tasks_also_raise(patched, state, word):
    """r3: CANCELED / CANCELLED / REJECTED are diagnostics too, never answers."""
    _install_client(patched, send_resp=_status_resp(state, msg_text="stopped"))
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi"))
    assert word in str(ei.value)


def test_completed_task_with_no_text_raises_rather_than_returning_empty(patched):
    """r4: a COMPLETED task we can read no answer text out of raises a state-bearing error
    rather than handing back an empty string."""
    _install_client(patched, send_resp=_status_resp("TASK_STATE_COMPLETED"))
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=10), "hi"))
    assert "returned no text" in str(ei.value) and "TASK_STATE_COMPLETED" in str(ei.value)


def test_bare_message_reply_still_returns_its_text(patched):
    """r6: a peer that answers with a status-less reply (no task envelope) stays
    compatible — its text is the answer, unchanged from before this fix."""
    bare = _Resp({"jsonrpc": "2.0", "result": {"artifacts": [{"parts": [{"kind": "text", "text": "bare reply"}]}]}})
    _install_client(patched, send_resp=bare)
    assert asyncio.run(A.dispatch(_parse(), "hi")) == "bare reply"


def test_task_envelope_without_state_is_not_answered_as_a_bare_message(patched):
    """r6: a ``{"task": …}`` envelope with artifacts but no usable state is a pending /
    malformed task, not a bare Message — its text is never returned; the dispatch reports
    "still running" once the poll deadline passes instead."""
    _clock(patched, step=0.3)
    stateless = _Resp(
        {"jsonrpc": "2.0", "result": {"task": {"id": "t1", "artifacts": [{"parts": [{"text": "not an answer"}]}]}}}
    )
    _install_capture_client(patched, send_resp=stateless, get_resp=stateless)
    with pytest.raises(DelegateError) as ei:
        asyncio.run(A.dispatch(_parse(poll_timeout_s=1), "hi"))
    assert "not an answer" not in str(ei.value)
    assert "still running" in str(ei.value)
