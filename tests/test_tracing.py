"""Tests for the Langfuse tracing module.

The hot path here is ``trace_session`` — an async context manager that
makes a Langfuse observation the active parent for its body. These tests
verify the wiring survives a re-arrangement without regression:

- When Langfuse is disabled, every helper is a silent no-op (never raises,
  never holds state).
- When enabled, ``trace_session`` calls ``start_as_current_observation``
  AND enters the returned context manager — the previous API created the
  span but never entered its scope, so children didn't nest.
- ``current_trace_id()`` reads the contextvar set on entry and clears on
  exit; nested sessions restore the outer trace id.
- ``trace_tool_call`` stamps the current trace_id into its metadata so
  audit-log cross-ref works even if Langfuse later reshapes the span tree.

The tests don't require the real langfuse package — a minimal fake client
with the three methods we touch covers the contract.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def _reload_tracing():
    """Fresh module import of the real tracing.py so each test starts
    from init=disabled, even if a sibling test file inserted a stub
    into sys.modules first (test_exception_logging.py does this)."""
    import importlib.util
    from pathlib import Path

    if "tracing" in sys.modules:
        del sys.modules["observability.tracing"]
    real_path = Path(__file__).parents[1] / "observability" / "tracing.py"
    spec = importlib.util.spec_from_file_location("observability.tracing", real_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability.tracing"] = module
    spec.loader.exec_module(module)
    return module


def _enable_with_fake_client(tracing):
    """Inject a fake Langfuse client and flip _enabled. Returns the fake."""
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "trace-abc"
    # start_as_current_observation returns a context manager
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    # start_observation returns an observation with .end()
    child = MagicMock()
    fake.start_observation.return_value = child
    tracing._langfuse = fake
    tracing._enabled = True
    return fake, span, child


# ── Disabled (no Langfuse) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_trace_session_is_noop_context_manager():
    tracing = _reload_tracing()
    assert tracing.is_enabled() is False

    async with tracing.trace_session("s-1", name="x") as span:
        assert span is None
        assert tracing.current_trace_id() == ""
        # session_id is set even when Langfuse is disabled
        assert tracing.current_session_id() == "s-1"

    # Calls outside a session return default ""
    assert tracing.current_trace_id() == ""
    assert tracing.current_session_id() == ""


def test_disabled_trace_tool_call_returns_none():
    tracing = _reload_tracing()
    assert tracing.trace_tool_call("t", {}, "ok", 10, True) is None


def test_disabled_score_current_trace_is_silent():
    tracing = _reload_tracing()
    tracing.score_current_trace("verdict", 1.0)  # must not raise


# ── Enabled (fake Langfuse) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_session_enters_context_and_exposes_trace_id():
    """Regression: the previous API called start_as_current_observation
    without `with`, so the span was created but its scope was never active.
    Children never nested. Lock that trace_session enters the CM."""
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)

    captured_trace_id = None
    captured_session_id = None

    async with tracing.trace_session("s-abc", name="a2a-stream"):
        captured_trace_id = tracing.current_trace_id()
        captured_session_id = tracing.current_session_id()

    # start_as_current_observation was called with the right name + metadata
    fake.start_as_current_observation.assert_called_once()
    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "a2a-stream"
    assert kwargs["metadata"]["session_id"] == "s-abc"
    assert "protoagent" in kwargs["metadata"]["tags"]

    # AND the returned CM was actually entered (the bug fix)
    cm = fake.start_as_current_observation.return_value
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()

    # current_trace_id reflected the span inside the scope, clears outside
    assert captured_trace_id == "trace-abc"
    assert tracing.current_trace_id() == ""
    # session_id is set by trace_session and cleared on exit
    assert captured_session_id == "s-abc"
    assert tracing.current_session_id() == ""


@pytest.mark.asyncio
async def test_disabled_trace_session_swallows_cross_context_reset_error():
    """Regression: with Langfuse disabled (the local default), an SSE client
    disconnecting mid-stream tears down the async generator in a different
    context, so ``_session_id_ctx.reset(token)`` raises ``ValueError: ... was
    created in a different Context``. trace_session's finally must swallow it
    instead of letting it escape as an unretrieved task exception."""
    tracing = _reload_tracing()
    assert tracing.is_enabled() is False

    real = tracing._session_id_ctx

    class _ResetBoom:
        def set(self, value):
            return real.set(value)

        def get(self):
            return real.get()

        def reset(self, _token):
            raise ValueError("was created in a different Context")

    tracing._session_id_ctx = _ResetBoom()
    try:
        # Must not raise despite reset() blowing up in the finally block.
        async with tracing.trace_session("s-boom", name="x") as span:
            assert span is None
    finally:
        tracing._session_id_ctx = real


@pytest.mark.asyncio
async def test_trace_session_exception_is_swallowed_so_agent_keeps_running():
    """If Langfuse itself raises, the agent must not crash. trace_session
    yields None and the caller proceeds unscoped."""
    tracing = _reload_tracing()
    fake = MagicMock()
    fake.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    tracing._langfuse = fake
    tracing._enabled = True

    async with tracing.trace_session("s-err") as span:
        assert span is None


@pytest.mark.asyncio
async def test_trace_tool_call_stamps_current_trace_id_into_metadata():
    """Audit cross-ref contract: the tool observation carries the
    current trace_id in its metadata so an audit-log line (which also
    records trace_id) can be matched to the exact Langfuse trace."""
    tracing = _reload_tracing()
    fake, _span, child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", name="parent"):
        tracing.trace_tool_call(
            tool_name="board_monitor",
            args={"action": "sitrep"},
            result="ok",
            duration_ms=42,
            success=True,
            session_id="s-1",
        )

    fake.start_observation.assert_called_once()
    kwargs = fake.start_observation.call_args.kwargs
    assert kwargs["name"] == "tool:board_monitor"
    assert kwargs["metadata"]["trace_id"] == "trace-abc"
    assert kwargs["metadata"]["duration_ms"] == 42
    assert kwargs["level"] == "DEFAULT"
    child.end.assert_called_once()


def test_trace_tool_call_on_failure_marks_error_level():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    tracing.trace_tool_call(
        tool_name="file_bug",
        args={},
        result="boom",
        duration_ms=10,
        success=False,
    )
    kwargs = fake.start_observation.call_args.kwargs
    assert kwargs["level"] == "ERROR"


def test_score_current_trace_delegates_to_client():
    tracing = _reload_tracing()
    fake, _s, _c = _enable_with_fake_client(tracing)
    tracing.score_current_trace("verdict", 1.0, comment="PASS")
    fake.score_current_trace.assert_called_once_with(
        name="verdict",
        value=1.0,
        comment="PASS",
    )


# ── Fleet tracing: caller trace join (a2a.trace → trace_context) ─────────────

_TID = "a" * 32  # valid W3C trace id (32 hex)
_SID = "b" * 16  # valid W3C span id (16 hex)


@pytest.mark.asyncio
async def test_trace_session_joins_caller_trace_context():
    """When metadata carries caller_trace_id/caller_span_id (the a2a.trace ids
    an upstream agent sent), the session span JOINS that trace via Langfuse's
    trace_context — and current_trace_id() reports the JOINED id so audit
    records + downstream propagation carry the fleet trace."""
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)
    span.trace_id = _TID  # the SDK reports the joined trace id on the span

    async with tracing.trace_session(
        "s-1", name="a2a-stream", metadata={"caller_trace_id": _TID, "caller_span_id": _SID}
    ):
        assert tracing.current_trace_id() == _TID

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID, "parent_span_id": _SID}
    # The metadata stamping is KEPT (operators cross-reference by it too)
    assert kwargs["metadata"]["caller_trace_id"] == _TID
    assert kwargs["metadata"]["caller_span_id"] == _SID


@pytest.mark.asyncio
async def test_trace_session_join_without_span_id_still_joins_trace():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", metadata={"caller_trace_id": _TID}):
        pass

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID}


@pytest.mark.asyncio
async def test_trace_session_malformed_caller_ids_fall_back_to_fresh_trace():
    """Malformed caller ids must never crash a turn or feed the SDK a bogus
    W3C context — the session degrades to a fresh trace."""
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session(
        "s-1", metadata={"caller_trace_id": "not-a-trace-id", "caller_span_id": "zz"}
    ) as span:
        assert span is not None  # the session still traces — freshly
        assert tracing.current_trace_id() == "trace-abc"

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] is None


@pytest.mark.asyncio
async def test_trace_session_join_with_malformed_span_id_keeps_trace_id_only():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", metadata={"caller_trace_id": _TID, "caller_span_id": "junk"}):
        pass

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID}


# ── current_trace_context (outbound propagation helper) ──────────────────────


def test_disabled_current_trace_context_is_none():
    tracing = _reload_tracing()
    assert tracing.current_trace_context() is None


def test_current_trace_context_shape_from_sdk():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = _TID
    fake.get_current_observation_id.return_value = _SID
    assert tracing.current_trace_context() == {"trace_id": _TID, "span_id": _SID}


def test_current_trace_context_without_current_span_has_trace_id_only():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = _TID
    fake.get_current_observation_id.return_value = None
    assert tracing.current_trace_context() == {"trace_id": _TID}


def test_current_trace_context_none_when_no_active_trace():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = None
    fake.get_current_observation_id.return_value = None
    assert tracing.current_trace_context() is None


def test_current_trace_context_falls_back_to_contextvar_when_sdk_errors():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.side_effect = RuntimeError("otel misery")
    fake.get_current_observation_id.side_effect = RuntimeError("otel misery")
    token = tracing._trace_id_ctx.set(_TID)
    try:
        assert tracing.current_trace_context() == {"trace_id": _TID}
    finally:
        tracing._trace_id_ctx.reset(token)


# ── trace_span (boundary spans, e.g. subagent:<type>) ────────────────────────


def test_disabled_trace_span_is_noop():
    tracing = _reload_tracing()
    with tracing.trace_span("subagent:worker") as span:
        assert span is None


def test_trace_span_opens_and_closes_child_observation():
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)

    with tracing.trace_span("subagent:worker", metadata={"description": "d"}, as_type="agent") as s:
        assert s is span

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "subagent:worker"
    assert kwargs["as_type"] == "agent"
    assert kwargs["metadata"] == {"description": "d"}
    cm = fake.start_as_current_observation.return_value
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()


def test_trace_span_body_exception_propagates_but_span_closes():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    with pytest.raises(RuntimeError):
        with tracing.trace_span("subagent:worker"):
            raise RuntimeError("subagent exploded")

    cm = fake.start_as_current_observation.return_value
    cm.__exit__.assert_called_once()


def test_trace_span_sdk_error_yields_none_and_body_runs():
    tracing = _reload_tracing()
    fake = MagicMock()
    fake.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    tracing._langfuse = fake
    tracing._enabled = True

    ran = False
    with tracing.trace_span("subagent:worker") as span:
        assert span is None
        ran = True
    assert ran


# ── shutdown flush wiring ─────────────────────────────────────────────────────


def test_flush_delegates_to_client_and_swallows_errors():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    tracing.flush()
    fake.flush.assert_called_once()
    fake.flush.side_effect = RuntimeError("exporter down")
    tracing.flush()  # must not raise


def test_server_shutdown_hook_flushes_tracing():
    """Wiring lock: the server shutdown hook must flush buffered observations
    so spans survive process exit (server/__init__.py is only importable inside
    a full boot, so we lock the source contract)."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "server" / "__init__.py").read_text()
    hook = src.split('@fastapi_app.on_event("shutdown")', 1)[1]
    hook = hook.split("register_chat_routes", 1)[0]  # the hook body ends before route registration
    assert "tracing.flush" in hook, "shutdown hook no longer flushes Langfuse tracing"


def test_no_legacy_shims_exist():
    """Greenfield guarantee — start_trace / end_trace / trace_llm_call were
    removed. Their return would silently break the nesting contract by
    teaching callers to bypass trace_session."""
    tracing = _reload_tracing()
    assert not hasattr(tracing, "start_trace")
    assert not hasattr(tracing, "end_trace")
    assert not hasattr(tracing, "trace_llm_call")


def test_otel_cross_context_detach_error_is_silenced():
    """When an SSE consumer (e.g. an A2A executor) closes
    the stream early, GeneratorExit propagates through
    trace_session's __aexit__. The Langfuse span's underlying OTel
    token was attached in a child task's contextvar snapshot, so the
    detach during cleanup logs an error before raising. Our finally
    block already swallows the raised ValueError — this test locks in
    that the OTel logger doesn't spam docker logs about it either.
    """
    import io
    import logging

    _reload_tracing()  # ensures the filter is installed via module import

    handler_buf = io.StringIO()
    handler = logging.StreamHandler(handler_buf)
    handler.setLevel(logging.ERROR)
    otel_log = logging.getLogger("opentelemetry.context")
    otel_log.addHandler(handler)
    otel_log.setLevel(logging.ERROR)

    try:
        # Simulate the exact noise OTel emits on cross-context detach.
        # OTel calls `_logger.error("Failed to detach context", exc_info=True)` —
        # the actual ValueError text is in exc_info, not the message. Filter
        # has to match on the message string itself.
        try:
            raise ValueError(
                "<Token var=<ContextVar name='current_context'> at 0x...> was created in a different Context"
            )
        except ValueError:
            otel_log.error("Failed to detach context", exc_info=True)
        otel_log.error("Some other unrelated OTel error that should NOT be silenced")
    finally:
        otel_log.removeHandler(handler)

    output = handler_buf.getvalue()
    assert "Failed to detach context" not in output, "filter failed to silence the cross-context detach error"
    assert "unrelated OTel error" in output, "filter is too broad — it silenced an unrelated error too"
