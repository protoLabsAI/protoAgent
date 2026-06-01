"""Tests for a2a_handler — task store, background runner, webhook delivery, routes."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from a2a_handler import (
    CANCELED,
    COMPLETED,
    FAILED,
    SUBMITTED,
    WORKING,
    WORLDSTATE_DELTA_MIME,
    _TERMINAL,
    A2ATaskStore,
    PushNotificationConfig,
    TaskRecord,
    _build_artifact_event,
    _build_status_event,
    _deliver_webhook,
    _now_iso,
    _run_task_background,
    _store,
    _task_to_response,
    _terminal_artifact_parts,
    _watch_task,
    register_a2a_routes,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_record(**kwargs) -> TaskRecord:
    now = _now_iso()
    defaults = dict(
        id="test-task-id",
        context_id="test-ctx",
        state=SUBMITTED,
        created_at=now,
        updated_at=now,
        message_text="hello",
    )
    defaults.update(kwargs)
    return TaskRecord(**defaults)


@pytest.fixture
def store() -> A2ATaskStore:
    return A2ATaskStore()


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear module-level _store + _pending_webhook_tasks between tests.

    Route-integration tests exercise register_a2a_routes() which always uses
    the module singletons, so tasks from one test would otherwise leak into
    the next. Clearing on entry and exit keeps tests hermetic.
    """
    from a2a_handler import _pending_webhook_tasks
    _store._tasks.clear()
    _pending_webhook_tasks.clear()
    yield
    _store._tasks.clear()
    _pending_webhook_tasks.clear()


# ── Task store ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_create_and_get(store):
    record = _make_record()
    await store.create(record)
    fetched = await store.get("test-task-id")
    assert fetched is record


@pytest.mark.asyncio
async def test_store_get_missing(store):
    assert await store.get("no-such-id") is None


@pytest.mark.asyncio
async def test_store_update_state(store):
    record = _make_record()
    await store.create(record)
    updated = await store.update_state("test-task-id", WORKING)
    assert updated.state == WORKING
    # Fetch again to confirm persistence
    assert (await store.get("test-task-id")).state == WORKING


@pytest.mark.asyncio
async def test_store_update_accumulated_text(store):
    record = _make_record()
    await store.create(record)
    await store.update_state("test-task-id", WORKING, accumulated_text="hello world")
    assert (await store.get("test-task-id")).accumulated_text == "hello world"


@pytest.mark.asyncio
async def test_store_update_event_rotated(store):
    """Subscribers waiting on the old event wake up; new event is fresh."""
    record = _make_record()
    await store.create(record)
    old_event = record._update_event
    await store.update_state("test-task-id", WORKING)
    # Old event should be set (woke up subscribers)
    assert old_event.is_set()
    # New event should be a different, unset object
    new_record = await store.get("test-task-id")
    assert new_record._update_event is not old_event
    assert not new_record._update_event.is_set()


@pytest.mark.asyncio
async def test_store_cancel_sets_event(store):
    record = _make_record(state=WORKING)
    record._bg_task = None
    await store.create(record)
    result = await store.cancel("test-task-id")
    assert result is True
    assert record._cancel_event.is_set()


@pytest.mark.asyncio
async def test_store_cancel_missing(store):
    assert await store.cancel("no-such-id") is False


# ── Eviction ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_removes_old_terminal_tasks(store):
    """Terminal tasks older than the TTL are evicted; recent ones survive."""
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    old = _make_record(id="old", state=COMPLETED)
    old.updated_at = old_ts
    recent = _make_record(id="recent", state=COMPLETED)
    recent.updated_at = recent_ts

    await store.create(old)
    await store.create(recent)

    removed = await store.cleanup_expired(ttl_seconds=3600)  # 1 hour
    assert removed == 1
    assert await store.get("old") is None
    assert await store.get("recent") is not None


@pytest.mark.asyncio
async def test_cleanup_spares_working_tasks_regardless_of_age(store):
    """A task that's been WORKING for hours must stay — only terminal tasks age out."""
    from datetime import datetime, timedelta, timezone

    stale_working = _make_record(id="stuck", state=WORKING)
    stale_working.updated_at = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat()
    await store.create(stale_working)

    removed = await store.cleanup_expired(ttl_seconds=60)
    assert removed == 0
    assert await store.get("stuck") is not None


@pytest.mark.asyncio
async def test_start_cleanup_is_idempotent(store):
    """Calling start_cleanup repeatedly must not spawn multiple loops — the
    route handlers call it on every request, so this property matters."""
    store.start_cleanup(interval_s=60, ttl_s=60)
    first = store._cleanup_task
    store.start_cleanup(interval_s=60, ttl_s=60)
    second = store._cleanup_task
    assert first is second
    first.cancel()
    try:
        await first
    except asyncio.CancelledError:
        pass


# ── Atomic cancel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_updated_record(store):
    record = _make_record(state=WORKING)
    await store.create(record)
    result = await store.cancel_if_not_terminal("test-task-id")
    assert result is not None
    assert result.state == CANCELED
    # Update event rotates so subscribers wake up (same contract as update_state)
    assert record._cancel_event.is_set()


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_none_when_already_terminal(store):
    """Prevents the route handler from clobbering a COMPLETED state that
    the runner wrote while the request was in flight."""
    record = _make_record(state=COMPLETED)
    await store.create(record)
    result = await store.cancel_if_not_terminal("test-task-id")
    assert result is None
    # Record must still be COMPLETED — not downgraded to CANCELED
    assert (await store.get("test-task-id")).state == COMPLETED


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_none_when_missing(store):
    assert await store.cancel_if_not_terminal("no-such-id") is None


# ── SSRF validation ───────────────────────────────────────────────────────────


def test_ssrf_rejects_non_http_scheme():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("file:///etc/passwd") is False
    assert _is_safe_webhook_url("gopher://example.com/x") is False
    assert _is_safe_webhook_url("javascript:alert(1)") is False


def test_ssrf_rejects_loopback():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://127.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://localhost/hook") is False
    assert _is_safe_webhook_url("http://[::1]/hook") is False


def test_ssrf_rejects_rfc1918():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://10.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://192.168.1.1/hook") is False
    assert _is_safe_webhook_url("http://172.16.0.1/hook") is False


def test_ssrf_rejects_link_local_and_metadata():
    """169.254.169.254 is the AWS/GCP instance-metadata endpoint — the
    canonical SSRF target. Must be blocked."""
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://169.254.169.254/latest/meta-data") is False
    assert _is_safe_webhook_url("http://169.254.1.1/hook") is False


def test_ssrf_rejects_unresolvable_hostname():
    from a2a_handler import _is_safe_webhook_url
    # A hostname under RFC2606's invalid TLD — guaranteed not to resolve.
    assert _is_safe_webhook_url("http://totally-not-a-real-host.invalid/") is False


def test_ssrf_rejects_malformed_url():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("") is False
    assert _is_safe_webhook_url("not-a-url") is False
    assert _is_safe_webhook_url("http://") is False


def test_ssrf_accepts_public_ip_literal():
    """A globally-routable IP literal passes — covers the common case of
    operators giving a Tailscale/cloud public IP without DNS."""
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("https://8.8.8.8/hook") is True


def test_parse_push_config_rejects_unsafe_url():
    """Integration between the parser and the SSRF check — malicious
    submit-time configurations must be dropped (return None), not converted
    into a PushNotificationConfig the runner would then deliver to."""
    from a2a_handler import _parse_push_config
    cfg = _parse_push_config({"pushNotificationConfig": {"url": "http://169.254.169.254/"}})
    assert cfg is None


def test_extract_push_token_top_level():
    """A2A spec PushNotificationConfig accepts two token shapes:
    top-level `token` (what @a2a-js/sdk serialises by default and
    Workstacean's SkillDispatcherPlugin uses) OR structured
    `authentication.credentials` (RFC-8821 AuthenticationInfo).
    Both must work — if only one shape is parsed, the agent falls
    back to polling silently because delivered webhooks get a 401."""
    from a2a_handler import _extract_push_token
    assert _extract_push_token({"token": "ws-token-xyz"}) == "ws-token-xyz"


def test_extract_push_token_structured_authentication():
    """The RFC-8821 AuthenticationInfo shape still works."""
    from a2a_handler import _extract_push_token
    assert _extract_push_token({
        "authentication": {"schemes": ["Bearer"], "credentials": "rfc-token"},
    }) == "rfc-token"


def test_extract_push_token_prefers_top_level_when_both_present():
    """If a caller sends both (unlikely but spec-permitted), top-level
    wins — it's the common case and structured is the fallback."""
    from a2a_handler import _extract_push_token
    assert _extract_push_token({
        "token": "preferred",
        "authentication": {"credentials": "fallback"},
    }) == "preferred"


def test_extract_push_token_none_when_absent():
    """Neither shape present → None. Delivery still fires, but without
    an Authorization header (for public or unauthenticated callbacks)."""
    from a2a_handler import _extract_push_token
    assert _extract_push_token({}) is None
    assert _extract_push_token({"token": ""}) is None
    assert _extract_push_token({"authentication": {}}) is None
    assert _extract_push_token({"token": None}) is None


# ── SSRF allowlist (trusted docker-network hosts) ────────────────────────────


def test_ssrf_allowlist_hosts_bypass_check(monkeypatch):
    """Workstacean registers callbacks at http://workstacean:3000/... —
    RFC1918 by design on the docker network. Operator-declared trusted
    hostnames bypass the check without DNS resolution even running
    (so even if the name doesn't resolve in the test env, the guard
    still accepts)."""
    monkeypatch.setenv("PUSH_NOTIFICATION_ALLOWED_HOSTS", "workstacean,automaker-server")
    import importlib, a2a_handler
    importlib.reload(a2a_handler)
    try:
        assert a2a_handler._is_safe_webhook_url(
            "http://workstacean:3000/api/a2a/callback/abc"
        ) is True
        assert a2a_handler._is_safe_webhook_url(
            "http://automaker-server:3008/hook"
        ) is True
        # Un-listed hostnames still get the full SSRF treatment
        assert a2a_handler._is_safe_webhook_url("http://evil-internal/x") is False
    finally:
        monkeypatch.delenv("PUSH_NOTIFICATION_ALLOWED_HOSTS", raising=False)
        importlib.reload(a2a_handler)


def test_ssrf_allowlist_cidr_bypasses_private_check(monkeypatch):
    """CIDR allowlist: trust a whole docker subnet at once. The resolved
    IP falls in the allowlist → accept even though ip.is_private."""
    monkeypatch.setenv("PUSH_NOTIFICATION_ALLOWED_CIDRS", "10.0.14.0/24")
    import importlib, a2a_handler
    importlib.reload(a2a_handler)
    try:
        # IP literal inside the allowlist — accepted despite RFC1918
        assert a2a_handler._is_safe_webhook_url("http://10.0.14.3/hook") is True
        # IP literal outside the allowlist — still rejected
        assert a2a_handler._is_safe_webhook_url("http://10.0.99.1/hook") is False
    finally:
        monkeypatch.delenv("PUSH_NOTIFICATION_ALLOWED_CIDRS", raising=False)
        importlib.reload(a2a_handler)


def test_ssrf_allowlist_empty_by_default(monkeypatch):
    """Default-deny: unset env vars mean no bypass, every RFC1918 still
    rejected. Locks the posture so a shipped image without the vars
    set behaves like the pre-allowlist guard."""
    monkeypatch.delenv("PUSH_NOTIFICATION_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("PUSH_NOTIFICATION_ALLOWED_CIDRS", raising=False)
    import importlib, a2a_handler
    importlib.reload(a2a_handler)
    assert a2a_handler._is_safe_webhook_url("http://10.0.14.3/hook") is False


def test_ssrf_allowlist_ignores_malformed_cidr(monkeypatch, caplog):
    """A typo in the CIDR allowlist shouldn't crash init — log it and
    ignore the bad entry, keeping valid ones."""
    monkeypatch.setenv("PUSH_NOTIFICATION_ALLOWED_CIDRS", "10.0.14.0/24,not-a-cidr,192.168.1.0/24")
    import importlib, a2a_handler
    importlib.reload(a2a_handler)
    try:
        assert a2a_handler._is_safe_webhook_url("http://10.0.14.3/") is True
        assert a2a_handler._is_safe_webhook_url("http://192.168.1.5/") is True
    finally:
        monkeypatch.delenv("PUSH_NOTIFICATION_ALLOWED_CIDRS", raising=False)
        importlib.reload(a2a_handler)


# ── Webhook task retention ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_retains_webhook_task_reference():
    """Regression: asyncio.create_task(...) without storing the handle risks
    GC mid-retry (Python 3.11+ docs). _push must add the task to the
    module-level set and only drop it on completion."""
    from a2a_handler import _pending_webhook_tasks, _push

    record = _make_record(state=COMPLETED, accumulated_text="done")
    record.push_config = PushNotificationConfig(url="https://example.com/hook")

    captured_tasks = []
    evt = asyncio.Event()

    async def _slow_deliver(r, cfg):
        # Stall long enough for the caller to observe task retention.
        await evt.wait()

    with patch("a2a_handler._deliver_webhook", _slow_deliver):
        await _push(record)
        await asyncio.sleep(0)  # let create_task schedule

        # Exactly one in-flight delivery, registered in the retention set.
        assert len(_pending_webhook_tasks) == 1
        task = next(iter(_pending_webhook_tasks))
        captured_tasks.append(task)

        # Release the stall; task completes; done_callback evicts from set.
        evt.set()
        await task
        await asyncio.sleep(0)
        assert task not in _pending_webhook_tasks


@pytest.mark.asyncio
async def test_deliver_webhook_includes_notification_token_header():
    """Webhook carries both Authorization: Bearer and the spec-canonical
    X-A2A-Notification-Token header so strict receivers can validate."""
    from a2a_handler import _deliver_webhook

    captured: dict = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return _Resp()

    rec = _make_record(state=COMPLETED, accumulated_text="done")
    cfg = PushNotificationConfig(url="https://example.com/hook", token="sek")
    with patch("a2a_handler.httpx.AsyncClient", _Client):
        await _deliver_webhook(rec, cfg)
    assert captured["headers"]["Authorization"] == "Bearer sek"
    assert captured["headers"]["X-A2A-Notification-Token"] == "sek"


@pytest.mark.asyncio
async def test_push_noop_without_config():
    from a2a_handler import _push

    calls = []

    async def _deliver(r, cfg):
        calls.append(r.state)

    rec = _make_record(state=WORKING)  # no push_config
    with patch("a2a_handler._deliver_webhook", _deliver):
        await _push(rec)
        await asyncio.sleep(0.02)
    assert calls == []


@pytest.mark.asyncio
async def test_push_throttles_nonterminal_then_flushes_terminal(monkeypatch):
    """Non-terminal transitions are throttled to one POST per window (latest
    state, via a trailing send); a terminal transition flushes immediately."""
    import a2a_handler as h

    monkeypatch.setattr(h, "_PUSH_MIN_INTERVAL_S", 0.05)
    h._push_last.clear()
    h._push_trailing.clear()

    calls: list[str] = []

    async def _deliver(r, cfg):
        calls.append(r.state)

    rec = _make_record(state=WORKING)
    rec.push_config = PushNotificationConfig(url="https://example.com/hook", token="t")
    h._store._tasks[rec.id] = rec  # trailing send re-reads the record from the store

    with patch("a2a_handler._deliver_webhook", _deliver):
        await h._push(rec)            # leading edge → immediate
        await asyncio.sleep(0.01)
        await h._push(rec)            # within window → suppressed, trailing scheduled
        await asyncio.sleep(0.01)
        assert calls == [WORKING]     # only the immediate one so far

        await asyncio.sleep(0.08)     # window closes → trailing fires with latest
        assert calls == [WORKING, WORKING]

        rec.state = COMPLETED         # terminal → immediate flush
        await h._push(rec)
        await asyncio.sleep(0.01)
        assert calls == [WORKING, WORKING, COMPLETED]

    h._push_last.clear()
    h._push_trailing.clear()


# ── Background task runner ────────────────────────────────────────────────────


async def _mock_stream(*events):
    """Helper: yields (event_type, payload) tuples."""
    for event in events:
        yield event
        await asyncio.sleep(0)  # yield control


@pytest.mark.asyncio
async def test_background_runner_success():
    store = A2ATaskStore()
    record = _make_record(id="bg-test")
    await store.create(record)

    push_calls = []

    async def _fake_push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "hello "), ("text", "world"), ("done", "hello world"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _fake_push):
        await _run_task_background("bg-test", stream_fn)

    final = await store.get("bg-test")
    assert final.state == COMPLETED
    assert final.accumulated_text == "hello world"
    assert WORKING in push_calls
    assert COMPLETED in push_calls


@pytest.mark.asyncio
async def test_background_runner_input_required_parks_task():
    """An input_required event parks the task (non-terminal) carrying the
    question, and pushes immediately — the HITL pause (ADR 0003)."""
    from a2a_handler import INPUT_REQUIRED, _STREAM_CLOSING, _TERMINAL

    assert INPUT_REQUIRED not in _TERMINAL  # parked, not finished
    assert INPUT_REQUIRED in _STREAM_CLOSING  # but closes the stream cycle

    store = A2ATaskStore()
    await store.create(_make_record(id="bg-hitl"))
    push_calls = []

    async def _fake_push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(
        ("text", "thinking… "),
        ("input_required", {"question": "Approve the merge?"}),
        ("done", "should not reach"),  # runner must stop at input_required
    )
    with patch("a2a_handler._store", store), patch("a2a_handler._push", _fake_push):
        await _run_task_background("bg-hitl", stream_fn)

    rec = await store.get("bg-hitl")
    assert rec.state == INPUT_REQUIRED
    assert rec.last_status_message == "Approve the merge?"  # question retained
    assert INPUT_REQUIRED in push_calls
    assert COMPLETED not in push_calls  # the trailing done was never processed


def test_status_event_final_on_input_required():
    from a2a_handler import INPUT_REQUIRED, _build_status_event

    rec = _make_record(id="q1", state=INPUT_REQUIRED)
    rec.last_status_message = "What is the threshold?"
    evt = _build_status_event(rec, final=True)
    assert evt["kind"] == "status-update"
    assert evt["status"]["state"] == "input-required"
    assert evt["final"] is True
    # The question rides in status.message (input-required isn't terminal, so
    # the status message is preserved rather than cleared).
    text = " ".join(p.get("text", "") for p in evt["status"]["message"]["parts"])
    assert "threshold" in text


@pytest.mark.asyncio
async def test_background_runner_error():
    store = A2ATaskStore()
    record = _make_record(id="bg-err")
    await store.create(record)

    push_calls = []

    async def _fake_push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "partial"), ("error", "boom"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _fake_push):
        await _run_task_background("bg-err", stream_fn)

    final = await store.get("bg-err")
    assert final.state == FAILED
    assert final.error_message == "boom"
    assert FAILED in push_calls


@pytest.mark.asyncio
async def test_background_runner_cancel():
    store = A2ATaskStore()
    record = _make_record(id="bg-cancel")
    await store.create(record)

    # Pre-set the cancel event so the runner exits immediately
    record._cancel_event.set()

    async def _noop_push(r):
        pass

    stream_fn = lambda: _mock_stream(("text", "should not process"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop_push):
        await _run_task_background("bg-cancel", stream_fn)

    final = await store.get("bg-cancel")
    assert final.state == CANCELED


# ── _push reads live push_config ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_reads_push_config_from_live_record():
    """Regression: _push must read record.push_config at call time, not close
    over a submit-time value. Otherwise POST /tasks/{id}/pushNotificationConfigs
    would silently never reach in-flight tasks."""
    from a2a_handler import _push

    record = _make_record(state=WORKING)
    # No push config yet — _push should no-op.
    delivered = []

    async def _fake_deliver(r, cfg):
        delivered.append(cfg.url)

    with patch("a2a_handler._deliver_webhook", _fake_deliver):
        await _push(record)
        await asyncio.sleep(0)  # drain create_task
        assert delivered == []

        # Caller registers a webhook after submit — _push must pick it up.
        record.push_config = PushNotificationConfig(url="http://late.example/hook")
        await _push(record)
        await asyncio.sleep(0)
        assert delivered == ["http://late.example/hook"]


# ── Worldstate-delta-v1 artifact emission ────────────────────────────────────


@pytest.mark.asyncio
async def test_store_add_delta_appends_to_record(store):
    """Deltas accumulate under the lock so concurrent tool calls don't
    clobber each other's entries."""
    record = _make_record(state=WORKING)
    await store.create(record)
    await store.add_delta("test-task-id", {"domain": "d", "path": "x", "op": "inc", "value": 1})
    await store.add_delta("test-task-id", {"domain": "d", "path": "y", "op": "set", "value": 42})
    fetched = await store.get("test-task-id")
    assert len(fetched.deltas) == 2
    assert fetched.deltas[0]["path"] == "x"
    assert fetched.deltas[1]["value"] == 42


@pytest.mark.asyncio
async def test_store_add_delta_on_missing_task_is_noop(store):
    """No raise, just silent drop — matches update_state's contract."""
    await store.add_delta("no-such-id", {"domain": "d", "path": "x", "op": "inc", "value": 1})


def test_terminal_artifact_parts_text_only() -> None:
    """A run with no observed mutations produces a single text part — no
    empty DataPart, which would confuse consumers looking for deltas."""
    record = _make_record(state=COMPLETED, accumulated_text="hello world")
    parts = _terminal_artifact_parts(record)
    assert parts == [{"kind": "text", "text": "hello world"}]


def test_terminal_artifact_parts_text_and_delta() -> None:
    """When deltas exist, the text part comes first and a DataPart carrying
    the canonical MIME type follows. Ordering matters — Workstacean's
    executor reads artifact.parts in order."""
    record = _make_record(state=COMPLETED, accumulated_text="Bug filed: ...")
    record.deltas.append(
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    )
    parts = _terminal_artifact_parts(record)
    assert len(parts) == 2
    assert parts[0] == {"kind": "text", "text": "Bug filed: ..."}
    assert parts[1]["kind"] == "data"
    assert parts[1]["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
    assert parts[1]["data"]["deltas"] == [
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    ]


def test_terminal_artifact_parts_empty_without_text_or_deltas() -> None:
    """Suppresses the artifact slot entirely — callers gate on an empty
    list to decide whether to emit an ``artifacts`` field at all."""
    record = _make_record(state=COMPLETED)
    assert _terminal_artifact_parts(record) == []


def test_task_to_response_includes_delta_artifact() -> None:
    """GET /tasks/{id} should surface the delta DataPart so pollers that
    never subscribed to webhooks still see the observed mutation."""
    record = _make_record(state=COMPLETED, accumulated_text="done")
    record.deltas.append(
        {"domain": "ci", "path": "data.blockedPRs", "op": "inc", "value": -1}
    )
    resp = _task_to_response(record)
    assert "artifacts" in resp
    parts = resp["artifacts"][0]["parts"]
    assert any(p.get("kind") == "data"
               and p["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
               for p in parts)


@pytest.mark.asyncio
async def test_webhook_payload_includes_delta_on_completed():
    """Push consumers must receive deltas in the same artifact they would
    have gotten by polling — otherwise webhook subscribers miss effects
    that poll subscribers see."""
    record = _make_record(state=COMPLETED, accumulated_text="ok")
    record.deltas.append(
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    )
    captured = {}

    async def _capture_post(url, json=None, headers=None):
        captured["json"] = json
        resp = MagicMock(status_code=204)
        return resp

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=MagicMock(post=_capture_post))
    client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("a2a_handler.httpx.AsyncClient", return_value=client_cm):
        await _deliver_webhook(
            record,
            PushNotificationConfig(url="https://example.com/hook"),
        )

    parts = captured["json"]["artifact"]["parts"]
    assert any(p.get("kind") == "data"
               and p["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
               for p in parts), f"delta missing from webhook payload: {parts}"


@pytest.mark.asyncio
async def test_background_runner_records_delta_event():
    """The runner must accept ``delta`` stream events and persist them on
    the record so the terminal artifact carries them."""
    store = A2ATaskStore()
    record = _make_record(id="bg-delta")
    await store.create(record)

    delta = {"domain": "protomaker_board", "path": "data.backlog_count",
             "op": "inc", "value": 1}
    stream_fn = lambda: _mock_stream(
        ("text", "working..."),
        ("tool_end", "✅ file_bug → Bug filed: ..."),
        ("delta", delta),
        ("done", "Bug filed: feature-abc"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        await _run_task_background("bg-delta", stream_fn)

    final = await store.get("bg-delta")
    assert final.state == COMPLETED
    assert final.deltas == [delta]


# ── Webhook delivery ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_delivery_success():
    record = _make_record(state=COMPLETED, accumulated_text="result text")
    cfg = PushNotificationConfig(url="https://example.com/hook", token="tok123")

    with patch("a2a_handler.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await _deliver_webhook(record, cfg)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer tok123"
        payload = call_kwargs.kwargs["json"]
        assert payload["kind"] == "status-update"
        assert payload["taskId"] == "test-task-id"
        assert payload["status"]["state"] == COMPLETED
        assert payload["final"] is True
        # Completed tasks include the artifact with camelCase lastChunk
        assert "artifact" in payload
        assert payload["artifact"]["lastChunk"] is True
        assert payload["artifact"]["artifactId"] == "test-task-id"


@pytest.mark.asyncio
async def test_webhook_success_is_logged_at_info_level(caplog):
    """Successful webhook delivery MUST log at INFO (not DEBUG). DEBUG
    is below the default WARNING log threshold, which silently hid
    delivery events in past builds — you couldn't tell from container
    logs whether delivery was even firing. Every successful POST must
    produce an INFO line carrying the task id, final state, and URL."""
    import logging
    record = _make_record(state=COMPLETED)
    cfg = PushNotificationConfig(url="https://example.com/hook")

    with patch("a2a_handler.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.INFO, logger="a2a_handler"):
            await _deliver_webhook(record, cfg)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("webhook delivered" in r.getMessage() for r in info_records), (
        "successful delivery must log at INFO, not DEBUG — otherwise the "
        "default WARNING log level silently hides whether delivery is working"
    )


@pytest.mark.asyncio
async def test_webhook_delivery_no_token():
    record = _make_record(state=FAILED, error_message="oops")
    cfg = PushNotificationConfig(url="https://example.com/hook")

    with patch("a2a_handler.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await _deliver_webhook(record, cfg)

        call_kwargs = mock_client.post.call_args
        assert "Authorization" not in call_kwargs.kwargs["headers"]


# ── Event builders ────────────────────────────────────────────────────────────


def test_build_status_event_has_a2a_spec_shape():
    """A2A spec: TaskStatusUpdateEvent has kind='status-update',
    camelCase taskId/contextId, and a final flag. Without these fields
    @a2a-js/sdk silently skips every event."""
    record = _make_record(state=WORKING)
    evt = _build_status_event(record)
    assert evt["kind"] == "status-update"
    assert evt["taskId"] == "test-task-id"
    assert evt["contextId"] == "test-ctx"
    assert evt["status"]["state"] == WORKING
    assert evt["final"] is False


def test_build_status_event_final_flag():
    record = _make_record(state=COMPLETED)
    evt = _build_status_event(record, final=True)
    assert evt["final"] is True


def test_build_artifact_event_has_a2a_spec_shape():
    """A2A spec: TaskArtifactUpdateEvent has kind='artifact-update',
    camelCase taskId/contextId/lastChunk, and the artifact object
    carries an artifactId for cross-event correlation."""
    record = _make_record(accumulated_text="some output")
    evt = _build_artifact_event(record, last_chunk=True)
    assert evt["kind"] == "artifact-update"
    assert evt["taskId"] == "test-task-id"
    assert evt["contextId"] == "test-ctx"
    assert evt["artifact"]["artifactId"] == "test-task-id"
    assert evt["artifact"]["parts"][0]["text"] == "some output"
    assert evt["lastChunk"] is True
    assert evt["append"] is True


def test_terminal_artifact_emits_cost_v1_when_usage_tracked():
    """When the producer reported any LLM token usage, the terminal
    artifact carries a cost-v1 DataPart with usage + durationMs.
    Workstacean's A2AExecutor (protoWorkstacean#372) extracts this onto
    result.data so the cost interceptor records per-skill samples."""
    from a2a_handler import _terminal_artifact_parts, COST_MIME
    record = _make_record(state=COMPLETED, accumulated_text="hi")
    record.usage = {"input_tokens": 1500, "output_tokens": 420, "total_tokens": 1920}
    parts = _terminal_artifact_parts(record)
    cost = next((p for p in parts if p.get("metadata", {}).get("mimeType") == COST_MIME), None)
    assert cost is not None, "cost-v1 DataPart missing"
    assert cost["data"]["usage"] == {"input_tokens": 1500, "output_tokens": 420, "total_tokens": 1920}
    assert "durationMs" in cost["data"]
    assert isinstance(cost["data"]["durationMs"], int)


def test_terminal_artifact_omits_cost_v1_when_no_usage():
    """No tokens tracked → no cost-v1 part. Avoids polluting the artifact
    with empty payloads on tasks that completed without an LLM call
    (cancelled before model invoke, fail-fast errors, etc.)."""
    from a2a_handler import _terminal_artifact_parts, COST_MIME
    record = _make_record(state=COMPLETED, accumulated_text="hi")
    # default usage is all zeros
    parts = _terminal_artifact_parts(record)
    cost = next((p for p in parts if p.get("metadata", {}).get("mimeType") == COST_MIME), None)
    assert cost is None


@pytest.mark.asyncio
async def test_store_add_usage_accumulates(store):
    """add_usage sums across calls — the producer fires it on every
    on_chat_model_end, so multiple LLM hops in one task contribute to
    the same cost-v1 artifact."""
    record = _make_record()
    await store.create(record)
    await store.add_usage("test-task-id", input_tokens=100, output_tokens=50,
                          cache_read_input_tokens=60, cost_usd=0.001)
    await store.add_usage("test-task-id", input_tokens=200, output_tokens=80,
                          cache_read_input_tokens=40, cache_creation_input_tokens=10, cost_usd=0.002)
    fetched = await store.get("test-task-id")
    assert fetched.usage == {
        "input_tokens": 300, "output_tokens": 130, "total_tokens": 430,
        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10,
        "cost_usd": 0.003,
    }


@pytest.mark.asyncio
async def test_store_add_usage_ignores_zero_payloads(store):
    """Some providers report zero on early stream chunks — don't bump."""
    record = _make_record()
    await store.create(record)
    await store.add_usage("test-task-id", input_tokens=0, output_tokens=0)
    fetched = await store.get("test-task-id")
    assert fetched.usage == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
    }


def test_task_to_response_has_kind_discriminator():
    """A2A spec: Task objects carry kind='task'. This is what
    message/send / tasks/get / initial stream frame all return."""
    record = _make_record(state=WORKING)
    resp = _task_to_response(record)
    assert resp["kind"] == "task"
    assert resp["id"] == "test-task-id"
    assert resp["contextId"] == "test-ctx"


# ── _watch_task: shared SSE consumer ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_watch_task_yields_snapshot_then_terminates():
    """A terminal task must emit one status frame and exit — no hanging."""
    store = A2ATaskStore()
    record = _make_record(id="done", state=COMPLETED, accumulated_text="final")
    await store.create(record)
    with patch("a2a_handler._store", store):
        kinds = []
        async for kind, r, payload in _watch_task("done", start_text_len=0):
            kinds.append((kind, payload))
    # Initial snapshot: status + text_delta (since accumulated > start_len)
    assert kinds == [("status", None), ("text_delta", "final")]


@pytest.mark.asyncio
async def test_watch_task_emits_delta_not_full_text():
    """The regression at the heart of #13 item 2: on reconnect, the
    watcher must emit only the new suffix — never the full accumulated_text.

    The watcher naturally coalesces rapid successive updates (desirable
    for SSE chatter), so the test yields aggressively between updates to
    guarantee the watcher visits each intermediate state and that each
    visit emits only the NEW suffix, not the full text."""
    store = A2ATaskStore()
    record = _make_record(id="mid", state=WORKING, accumulated_text="abcdefgh")
    await store.create(record)

    async def _reader():
        deltas = []
        with patch("a2a_handler._store", store):
            async for kind, r, payload in _watch_task("mid", start_text_len=5):
                if kind == "text_delta":
                    deltas.append(payload)
                if kind == "status" and r is not None and r.state in _TERMINAL:
                    return deltas
        return deltas

    task = asyncio.create_task(_reader())
    # Yield repeatedly so the reader runs through snapshot and parks on
    # the first update event.
    for _ in range(5):
        await asyncio.sleep(0)

    # First append — let the reader wake and consume before the next one.
    await store.update_state("mid", WORKING, accumulated_text="abcdefghIJKL")
    for _ in range(5):
        await asyncio.sleep(0)

    # Terminal.
    await store.update_state("mid", COMPLETED, accumulated_text="abcdefghIJKL")
    deltas = await asyncio.wait_for(task, timeout=2.0)
    # Each update yields only the NEW suffix — never the full text.
    assert deltas == ["fgh", "IJKL"]


@pytest.mark.asyncio
async def test_watch_task_multiple_subscribers_each_see_terminal():
    """Two consumers on the same task both receive a terminal status frame.
    Exercises the rotate-event pattern: each update sets the old event and
    installs a fresh one, so both waiters wake up in lock-step."""
    store = A2ATaskStore()
    record = _make_record(id="multi", state=WORKING)
    await store.create(record)

    terminal_states: list[str] = []

    async def _consumer():
        last_state = None
        async for kind, r, _payload in _watch_task("multi", start_text_len=0):
            if kind == "status" and r is not None:
                last_state = r.state
                if last_state in _TERMINAL:
                    break
        terminal_states.append(last_state)

    # Single outer patch — unittest.mock.patch is not safe to stack under
    # concurrent asyncio tasks (restore semantics race on exit).
    with patch("a2a_handler._store", store):
        a = asyncio.create_task(_consumer())
        b = asyncio.create_task(_consumer())
        # Give both consumers time to emit their snapshot and park on the
        # rotating update_event before the test triggers the transition.
        await asyncio.sleep(0.05)
        await store.update_state("multi", COMPLETED)
        await asyncio.wait_for(asyncio.gather(a, b), timeout=2.0)
    assert terminal_states == [COMPLETED, COMPLETED]


@pytest.mark.asyncio
async def test_background_runner_persists_tool_status_message():
    """tool_start / tool_end payloads land on record.last_status_message so
    :subscribe reconnects can see the most recent tool message — the
    producer's in-process event stream is no longer the only source."""
    store = A2ATaskStore()
    record = _make_record(id="tooltrack")
    await store.create(record)

    stream_fn = lambda: _mock_stream(
        ("text", "starting"),
        ("tool_start", "🔧 file_bug: draft"),
        ("tool_end", "✅ file_bug → Bug filed: ..."),
        ("done", "starting"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        await _run_task_background("tooltrack", stream_fn)

    final = await store.get("tooltrack")
    # Terminal transitions clear the status message — subscribers on a
    # completed task shouldn't see a stale tool ping.
    assert final.state == COMPLETED
    assert final.last_status_message is None


@pytest.mark.asyncio
async def test_stream_producer_survives_consumer_cancellation():
    """The biggest guarantee of the SSE refactor: SSE connection drop does
    NOT kill the LangGraph producer. Verified by simulating a consumer
    disconnect mid-run and asserting the bg task still completes."""
    store = A2ATaskStore()

    async def _slow_stream(_text, _ctx):
        yield ("text", "partial")
        await asyncio.sleep(0.05)
        yield ("text", " more")
        await asyncio.sleep(0.05)
        yield ("done", "partial more")

    async def _noop_push(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop_push):
        # Manually seed + spawn the bg runner (mimics _submit_task)
        task_id = "drop-test"
        now = _now_iso()
        record = TaskRecord(
            id=task_id, context_id="c", state=SUBMITTED,
            created_at=now, updated_at=now, message_text="t",
        )
        await store.create(record)
        bg = asyncio.create_task(
            _run_task_background(task_id, lambda: _slow_stream("t", "c"))
        )
        record._bg_task = bg

        # Simulate an SSE consumer that attaches, reads one frame, then
        # "disconnects" by closing the generator. Matches what FastAPI
        # does to an SSE StreamingResponse when the HTTP connection closes.
        async def _dropping_consumer():
            gen = _watch_task(task_id, 0)
            await gen.__anext__()         # read one frame then drop
            await gen.aclose()            # close generator cleanly
            return "dropped"

        result = await asyncio.wait_for(_dropping_consumer(), timeout=1.0)
        assert result == "dropped"

        # BG task should STILL complete — that's the whole point of decoupling.
        # Success is measured by the task landing in COMPLETED state, which
        # only happens if the producer ran all the way through "done".
        await asyncio.wait_for(bg, timeout=3.0)
        final = await store.get(task_id)
        assert final.state == COMPLETED
        assert final.accumulated_text == "partial more"


# ── Route integration (FastAPI ASGI test client) ──────────────────────────────


def _make_test_app():
    """Create a minimal FastAPI app with A2A routes wired in."""
    from fastapi import FastAPI

    app = FastAPI()
    card = {"name": "test", "capabilities": {}}

    async def _fake_stream(text, context_id, **kwargs):
        yield ("text", "hello ")
        yield ("text", "world")
        yield ("done", "hello world")

    async def _fake_chat(text, session_id):
        return [{"role": "assistant", "content": "response"}]

    register_a2a_routes(
        app=app,
        chat_stream_fn_factory=_fake_stream,
        chat_fn=_fake_chat,
        api_key="",
        agent_card=card,
    )
    return app, card


@pytest.mark.asyncio
async def test_agent_card_capabilities():
    app, card = _make_test_app()
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is True


@pytest.mark.asyncio
async def test_message_send_returns_submitted():
    """message/send must return submitted state immediately (not block)."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    # Spec-compliance: the result must be a full Task object with the
    # `kind` discriminator — @a2a-js/sdk routes by kind. Inline-dict
    # builds that omit it break every consumer.
    assert data["result"]["kind"] == "task"
    assert data["result"]["status"]["state"] == SUBMITTED
    assert "id" in data["result"]
    assert "contextId" in data["result"]


@pytest.mark.asyncio
async def test_message_stream_events_have_kind_discriminator():
    """Live SSE frames for `message/stream` must each carry a `kind`
    discriminator (task / status-update / artifact-update) per the A2A
    spec. Without `kind`, @a2a-js/sdk's ``for await`` loop silently
    skips every event and consumers never attach. Lock the frame
    shape so this regression can't recur.
    """
    app, _ = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "s1",
                "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            frames = []
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                import json as _json
                payload = _json.loads(line[6:])
                result = payload.get("result", {})
                frames.append(result)
                # Stop once we have the initial task + at least one update —
                # the fake stream in _make_test_app completes fast.
                if len(frames) >= 3:
                    break

    # Every frame must declare its kind — the spec uses this for SDK routing
    for f in frames:
        assert "kind" in f, f"frame missing `kind`: {f}"
        assert f["kind"] in ("task", "status-update", "artifact-update", "message"), (
            f"unknown kind: {f['kind']}"
        )

    # Frame 0 must be the full Task snapshot
    assert frames[0]["kind"] == "task"
    assert frames[0]["id"]
    assert frames[0]["contextId"]

    # Subsequent frames must use camelCase taskId / contextId (not snake_case)
    for f in frames[1:]:
        assert "taskId" in f, f"update event missing camelCase `taskId`: {f}"
        assert "contextId" in f, f"update event missing camelCase `contextId`: {f}"
        # No snake_case leakage
        assert "task_id" not in f
        assert "context_id" not in f
        assert "last_chunk" not in f
        if f["kind"] == "artifact-update":
            assert "lastChunk" in f  # camelCase per spec
            assert "artifactId" in f["artifact"]


@pytest.mark.asyncio
async def test_get_task_unknown_returns_404():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/tasks/no-such-id")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_task_after_submit():
    """After message/send, GET /tasks/{id} returns the task record."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        send_resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )
        task_id = send_resp.json()["result"]["id"]

        # Poll until completed (fake stream is instant in tests)
        for _ in range(20):
            poll = await client.get(f"/tasks/{task_id}")
            if poll.json()["status"]["state"] == COMPLETED:
                break
            await asyncio.sleep(0.05)

    assert poll.json()["status"]["state"] == COMPLETED
    assert poll.json()["artifacts"][0]["parts"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_cancel_task():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        send_resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )
        task_id = send_resp.json()["result"]["id"]
        cancel_resp = await client.post(f"/tasks/{task_id}:cancel")

    # Either canceled or already completed (fake stream may finish instantly)
    assert cancel_resp.status_code in (200, 409)


@pytest.mark.asyncio
async def test_rest_send_returns_202():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/message:send",
            json={
                "message": {"parts": [{"kind": "text", "text": "hello"}]},
            },
        )

    assert resp.status_code == 202
    assert resp.json()["status"]["state"] == SUBMITTED


@pytest.mark.asyncio
async def test_stream_first_event_is_submitted():
    """SSE stream must emit submitted as the very first frame."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/message:stream",
            json={
                "message": {"parts": [{"kind": "text", "text": "hello"}]},
            },
        ) as resp:
            assert resp.status_code == 200
            first_line = None
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    first_line = line
                    break

    assert first_line is not None
    first_event = json.loads(first_line[len("data:") :].strip())
    # REST stream returns plain result dict (no jsonrpc wrapper for rpc_id=None)
    result = first_event.get("result", first_event)
    assert result["status"]["state"] == SUBMITTED


@pytest.mark.asyncio
async def test_agent_card_route():
    app, card = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    assert resp.json()["capabilities"]["streaming"] is True
    assert resp.json()["capabilities"]["pushNotifications"] is True


# ── Spec-compliant JSON-RPC method names ──────────────────────────────────────
# The @a2a-js/sdk Client sends these exact method names on every call. Earlier
# only message/send + message/sendStream were routed, so streaming, polling,
# cancel, and push-config were all broken for SDK clients with -32601. Guard
# each one.


async def _rpc(client, method, params=None, rpc_id=1):
    resp = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": rpc_id, "method": method,
        "params": params or {},
    })
    return resp


@pytest.mark.asyncio
async def test_rpc_message_stream_returns_sse_not_method_not_found():
    """#23-era bug: SDK sent method 'message/stream' (spec name), handler
    only accepted 'message/sendStream', so every streaming call returned
    -32601 and the SDK wrapped it as an 'Invalid response Content-Type'
    error. Lock the spec method name."""
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/stream",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
    assert resp.status_code == 200
    # SSE stream, not a JSON-RPC error
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "data:" in body
    # First SSE frame carries the submitted task receipt
    assert '"state": "submitted"' in body or '"state":"submitted"' in body


@pytest.mark.asyncio
async def test_rpc_sendStream_alias_still_works():
    """Legacy protoPen-era method name. Keep it accepted so nothing that
    still sends the old name silently breaks."""
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/sendStream",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


@pytest.mark.asyncio
async def test_rpc_tasks_get_returns_task_record():
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        # Submit, then fetch via JSON-RPC tasks/get (NOT the REST path).
        send = await _rpc(client, "message/send", {
            "message": {"parts": [{"kind": "text", "text": "hi"}]},
        })
        task_id = send.json()["result"]["id"]
        resp = await _rpc(client, "tasks/get", {"id": task_id}, rpc_id=2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 2
    assert body["result"]["id"] == task_id
    assert body["result"]["status"]["state"] in {"submitted", "working", "completed"}


@pytest.mark.asyncio
async def test_rpc_tasks_get_unknown_returns_task_not_found_error():
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _rpc(client, "tasks/get", {"id": "no-such-task"})
    body = resp.json()
    assert "error" in body
    # A2A spec uses -32001 for "task not found"
    assert body["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_rpc_tasks_cancel_returns_canceled_record():
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        send = await _rpc(client, "message/send", {
            "message": {"parts": [{"kind": "text", "text": "hi"}]},
        })
        task_id = send.json()["result"]["id"]
        resp = await _rpc(client, "tasks/cancel", {"id": task_id}, rpc_id=2)
    body = resp.json()
    # Either we cancelled it in time (result w/ canceled state) or the
    # fake stream already finished (409-ish error). Both are spec-valid.
    if "result" in body:
        assert body["result"]["status"]["state"] == "canceled"
    else:
        assert body["error"]["code"] == -32002  # "task already terminal"


@pytest.mark.asyncio
async def test_rpc_push_notification_config_roundtrip():
    """set → get → list → delete → get-again round-trip via JSON-RPC.
    Exercises every tasks/pushNotificationConfig/* method the SDK emits."""
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        send = await _rpc(client, "message/send", {
            "message": {"parts": [{"kind": "text", "text": "hi"}]},
        })
        task_id = send.json()["result"]["id"]

        # set — 8.8.8.8 is public and passes the SSRF guard
        r = await _rpc(client, "tasks/pushNotificationConfig/set", {
            "taskId": task_id,
            "pushNotificationConfig": {"url": "https://8.8.8.8/hook"},
        }, rpc_id=2)
        assert r.status_code == 200 and "result" in r.json()
        cfg_id = r.json()["result"]["pushNotificationConfig"]["id"]

        # get returns the config
        r = await _rpc(client, "tasks/pushNotificationConfig/get",
                       {"taskId": task_id}, rpc_id=3)
        assert r.json()["result"]["pushNotificationConfig"]["id"] == cfg_id

        # list returns [cfg]
        r = await _rpc(client, "tasks/pushNotificationConfig/list",
                       {"taskId": task_id}, rpc_id=4)
        assert len(r.json()["result"]) == 1

        # delete clears; subsequent get returns null
        r = await _rpc(client, "tasks/pushNotificationConfig/delete",
                       {"taskId": task_id}, rpc_id=5)
        assert r.json()["result"] is None
        r = await _rpc(client, "tasks/pushNotificationConfig/get",
                       {"taskId": task_id}, rpc_id=6)
        assert r.json()["result"] is None


@pytest.mark.asyncio
async def test_rpc_push_notification_config_set_rejects_unsafe_url():
    """SSRF guard applies to the JSON-RPC entry point, not just the
    REST alias — otherwise a spec-compliant SDK client bypasses it."""
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        send = await _rpc(client, "message/send", {
            "message": {"parts": [{"kind": "text", "text": "hi"}]},
        })
        task_id = send.json()["result"]["id"]
        r = await _rpc(client, "tasks/pushNotificationConfig/set", {
            "taskId": task_id,
            "pushNotificationConfig": {"url": "http://169.254.169.254/latest"},
        }, rpc_id=2)
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_rpc_unknown_method_returns_method_not_found():
    """Unknown methods should still surface as a clean -32601; locks the
    dispatch fallback so we don't accidentally 500 on a typo."""
    app, _ = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await _rpc(client, "some/bogus/method")
    body = r.json()
    assert body["error"]["code"] == -32601
    assert "some/bogus/method" in body["error"]["message"]


# ── Bearer token authentication tests ─────────────────────────────────────────


def _make_auth_test_app(token: str | None = "secret-token"):
    """Create a test app with A2A_AUTH_TOKEN configured (or unset)."""
    from fastapi import FastAPI

    app = FastAPI()
    card = {"name": "test", "capabilities": {}}

    async def _fake_stream(text, context_id, **kwargs):
        yield ("text", "hello ")
        yield ("done", "hello")

    async def _fake_chat(text, session_id):
        return [{"role": "assistant", "content": "response"}]

    env_patch = {"A2A_AUTH_TOKEN": token} if token is not None else {}
    with patch.dict("os.environ", env_patch, clear=True if token is None else False):
        register_a2a_routes(
            app=app,
            chat_stream_fn_factory=_fake_stream,
            chat_fn=_fake_chat,
            api_key="",
            agent_card=card,
        )
    return app, card


@pytest.mark.asyncio
async def test_missing_bearer_token():
    """POST /a2a without Authorization header returns 401 when auth is enabled."""
    app, _ = _make_auth_test_app(token="secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_bearer_token():
    """POST /a2a with wrong bearer token returns 401."""
    app, _ = _make_auth_test_app(token="secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        }, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_token():
    """POST /a2a with correct bearer token returns 200 and processes request."""
    app, _ = _make_auth_test_app(token="secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        }, headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    assert body["result"]["status"]["state"] == "submitted"


@pytest.mark.asyncio
async def test_auth_disabled_no_token():
    """When A2A_AUTH_TOKEN is unset, requests pass without Authorization header."""
    with patch.dict("os.environ", {}, clear=True):
        from fastapi import FastAPI
        app = FastAPI()
        card = {"name": "test", "capabilities": {}}

        async def _fake_stream(text, context_id, **kwargs):
            yield ("text", "hello ")
            yield ("done", "hello")

        async def _fake_chat(text, session_id):
            return [{"role": "assistant", "content": "response"}]

        # Ensure A2A_AUTH_TOKEN is not set
        import os
        os.environ.pop("A2A_AUTH_TOKEN", None)

        register_a2a_routes(
            app=app,
            chat_stream_fn_factory=_fake_stream,
            chat_fn=_fake_chat,
            api_key="",
            agent_card=card,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_warning(caplog):
    """WARNING is logged at startup when A2A_AUTH_TOKEN is not configured."""
    import logging
    import os
    with patch.dict("os.environ", {}, clear=False):
        os.environ.pop("A2A_AUTH_TOKEN", None)
        from fastapi import FastAPI
        app = FastAPI()
        card = {"name": "test", "capabilities": {}}

        async def _fake_stream(text, context_id, **kwargs):
            yield ("done", "")

        async def _fake_chat(text, session_id):
            return []

        with caplog.at_level(logging.WARNING, logger="a2a_handler"):
            register_a2a_routes(
                app=app,
                chat_stream_fn_factory=_fake_stream,
                chat_fn=_fake_chat,
                api_key="",
                agent_card=card,
            )

    assert any("A2A auth token not configured" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_bearer_token_advertised_in_agent_card():
    """securitySchemes.bearer is present in agent card when A2A_AUTH_TOKEN is set."""
    _, card = _make_auth_test_app(token="secret-token")
    assert "securitySchemes" in card
    assert card["securitySchemes"]["bearer"]["type"] == "http"
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"


@pytest.mark.asyncio
async def test_bearer_token_not_in_agent_card_when_unset():
    """securitySchemes is absent from agent card when A2A_AUTH_TOKEN is not set."""
    import os
    os.environ.pop("A2A_AUTH_TOKEN", None)
    with patch.dict("os.environ", {}, clear=False):
        os.environ.pop("A2A_AUTH_TOKEN", None)
        _, card = _make_auth_test_app(token=None)
    assert "securitySchemes" not in card


@pytest.mark.asyncio
async def test_whitespace_only_token_treated_as_unset():
    """A2A_AUTH_TOKEN with only whitespace is treated as unset (open mode)."""
    import os
    with patch.dict("os.environ", {"A2A_AUTH_TOKEN": "   "}, clear=False):
        from fastapi import FastAPI
        app = FastAPI()
        card = {"name": "test", "capabilities": {}}

        async def _fake_stream(text, context_id, **kwargs):
            yield ("done", "")

        async def _fake_chat(text, session_id):
            return []

        register_a2a_routes(
            app=app,
            chat_stream_fn_factory=_fake_stream,
            chat_fn=_fake_chat,
            api_key="",
            agent_card=card,
        )

    # With whitespace-only token, no auth required — request succeeds without header
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
    assert resp.status_code == 200
    assert "securitySchemes" not in card


# ── Origin verification ────────────────────────────────────────────────────────


def _make_origin_test_app(allowed_origins: str | None):
    """Build a test FastAPI app with A2A_ALLOWED_ORIGINS configured."""
    import os
    from fastapi import FastAPI
    env = {}
    if allowed_origins is not None:
        env["A2A_ALLOWED_ORIGINS"] = allowed_origins
    else:
        os.environ.pop("A2A_ALLOWED_ORIGINS", None)

    with patch.dict("os.environ", env, clear=False):
        if allowed_origins is None:
            os.environ.pop("A2A_ALLOWED_ORIGINS", None)
        app = FastAPI()
        card = {"name": "test", "capabilities": {}}

        async def _fake_stream(text, context_id, **kwargs):
            yield ("text", "hello")
            yield ("done", "hello")

        async def _fake_chat(text, session_id):
            return []

        register_a2a_routes(
            app=app,
            chat_stream_fn_factory=_fake_stream,
            chat_fn=_fake_chat,
            api_key="",
            agent_card=card,
        )
    return app


@pytest.mark.asyncio
async def test_allowed_origin_passes_sse():
    """SSE stream with matching Origin header is accepted."""
    app = _make_origin_test_app("https://example.com,https://other.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
            headers={"Origin": "https://example.com"},
        ) as resp:
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rejected_origin_returns_403():
    """SSE stream with non-matching Origin header receives 403."""
    app = _make_origin_test_app("https://example.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
            headers={"Origin": "https://evil.com"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unset_origins_warning(caplog):
    """WARNING is logged at startup when A2A_ALLOWED_ORIGINS is not set."""
    import logging
    import os
    from fastapi import FastAPI as _FastAPI
    os.environ.pop("A2A_ALLOWED_ORIGINS", None)
    with patch.dict("os.environ", {}, clear=False):
        os.environ.pop("A2A_ALLOWED_ORIGINS", None)
        app = _FastAPI()
        card = {"name": "test", "capabilities": {}}

        async def _fake_stream(text, context_id, **kwargs):
            yield ("done", "")

        async def _fake_chat(text, session_id):
            return []

        with caplog.at_level(logging.WARNING, logger="a2a_handler"):
            register_a2a_routes(
                app=app,
                chat_stream_fn_factory=_fake_stream,
                chat_fn=_fake_chat,
                api_key="",
                agent_card=card,
            )

    assert any("A2A_ALLOWED_ORIGINS" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_wildcard_disables_verification():
    """A2A_ALLOWED_ORIGINS='*' disables origin verification — any origin passes."""
    app = _make_origin_test_app("*")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
            headers={"Origin": "https://anything.example.com"},
        ) as resp:
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_origin_header_rejected_when_allowlist_set():
    """Missing Origin header is treated as empty string and rejected when allowlist is set."""
    app = _make_origin_test_app("https://example.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_origin_case_insensitive():
    """Origin comparison is case-insensitive (HTTPS://Example.COM matches https://example.com)."""
    app = _make_origin_test_app("https://example.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
            headers={"Origin": "HTTPS://Example.COM"},
        ) as resp:
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_origin_whitespace_trimmed_in_allowlist():
    """Whitespace around comma-separated origins is trimmed."""
    app = _make_origin_test_app("  https://example.com  ,  https://other.com  ")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
            headers={"Origin": "https://other.com"},
        ) as resp:
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rest_message_stream_origin_rejected():
    """REST POST /message:stream rejects non-matching Origin with 403."""
    app = _make_origin_test_app("https://example.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post(
            "/message:stream",
            json={"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            headers={"Origin": "https://evil.com"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_subscribe_endpoint_origin_rejected():
    """GET /tasks/{id}:subscribe rejects non-matching Origin with 403."""
    # First create a task
    app = _make_origin_test_app("https://example.com")

    # Create a task via unprotected message/send (no origin check on non-streaming)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        })
        assert resp.status_code == 200
        task_id = resp.json()["result"]["id"]

        # Now try to subscribe with a bad origin
        sub_resp = await client.get(
            f"/tasks/{task_id}:subscribe",
            headers={"Origin": "https://evil.com"},
        )
    assert sub_resp.status_code == 403
