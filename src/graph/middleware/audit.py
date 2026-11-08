"""AuditMiddleware — logs tool calls to audit.py, Langfuse, Prometheus, and SQLite.

Wraps every tool execution with timing, success/failure tracking,
and observability integration. Reuses existing audit/tracing/metrics modules.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import BoundedSemaphore, Lock

from langchain.agents.middleware import AgentMiddleware


from graph.middleware.redaction import redact

log = logging.getLogger(__name__)

# Warn once, not once per tool call: the failure mode this guards against is a
# retry storm, and a per-call traceback would be the loudest thing in the log.
_OBS_UNAVAILABLE_LOGGED = False


# Bounded, off-thread writes: a busy SQLite store must not stall a tool or grow
# an unbounded backlog. Only metadata (never args/results) enters this queue.
_TELEMETRY_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-telemetry")
_TELEMETRY_SLOTS = BoundedSemaphore(1024)
_TELEMETRY_LOG_LOCK = Lock()
_TELEMETRY_FAILURE_LOGGED = False


def _telemetry_failed():
    global _TELEMETRY_FAILURE_LOGGED
    with _TELEMETRY_LOG_LOCK:
        if not _TELEMETRY_FAILURE_LOGGED:
            _TELEMETRY_FAILURE_LOGGED = True
            # No exception text: a store exception could contain sensitive data.
            log.warning("[audit] tool telemetry unavailable; dropping records")


def _write_tool_telemetry(store, row):
    try:
        store.record_tool_call(row)
    except Exception:
        _telemetry_failed()
    finally:
        _TELEMETRY_SLOTS.release()


def _result_failed(result, text: str) -> bool:
    """Did this tool result report a failure?

    The standard signal is the result's ``status``: a tool that refuses raises
    ``ToolException`` and its ``handle_tool_error`` turns that into a ToolMessage with
    ``status="error"`` (the chat card also closes as an X on it). The text is
    unchanged by that, so it can't be read off the words. Tools that still return a
    plain ``"Error..."`` string are counted too, as before; that prefix is the older
    convention many core tools keep.
    """
    return getattr(result, "status", None) == "error" or text.startswith("Error")


def _record_tool_telemetry(request, result, error_class, started):
    """Capture caller context and enqueue one content-free row, best-effort."""
    try:
        from graph import ledger
        from observability import tracing
        from runtime.state import STATE

        store = STATE.telemetry_store
        if store is None:
            return
        tool = request.tool_call.get("name", "unknown")
        # The runtime's tool-owner map contains display names. The loaded catalog
        # lists kept tools by stable plugin id, including collision filtering.
        plugin = next(
            (entry["id"] for entry in STATE.plugin_meta if entry.get("loaded") and tool in entry.get("tools", [])),
            "core",
        )
        edge_id = ledger.current_edge_id()
        subagent = next(
            (edge["to_name"] for edge in ledger.live_edges()
             if edge["id"] == edge_id and edge["to_kind"] == "subagent"),
            None,
        ) if edge_id else None
        content = getattr(result, "content", None)
        text = "" if content is None else str(content)
        ok = not error_class and not _result_failed(result, text)
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": tracing.current_session_id() or ledger.current_session_id(),
            "trace_id": tracing.current_trace_id(),
            "tool": tool,
            "plugin": plugin,
            "ok": int(ok),
            "error_class": error_class or ("ToolError" if not ok else None),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "args_chars": len(json.dumps(request.tool_call.get("args", {}), ensure_ascii=False, default=str)),
            "result_chars": len(text),
            "subagent": subagent,
        }
        if not _TELEMETRY_SLOTS.acquire(blocking=False):
            _telemetry_failed()
            return
        try:
            _TELEMETRY_WRITER.submit(_write_tool_telemetry, store, row)
        except Exception:
            _TELEMETRY_SLOTS.release()
            raise
    except Exception:
        _telemetry_failed()


class _NoopAudit:
    """Stand-in for ``observability.audit.audit_logger`` — drops the row."""

    @staticmethod
    def log(**_kwargs) -> None:
        pass


class _NoopTracing:
    """Stand-in for ``observability.tracing``."""

    @staticmethod
    def current_session_id() -> str:
        return ""

    @staticmethod
    def trace_tool_call(**_kwargs) -> None:
        pass


class _NoopMetrics:
    """Stand-in for ``observability.metrics``."""

    @staticmethod
    def record_tool_call(*_args, **_kwargs) -> None:
        pass


def _observability():
    """Resolve ``(audit_logger, tracing, metrics)``, degrading to no-ops.

    Observability is NOT load-bearing: when these can't be resolved the tool call
    still has to run. They were imported unguarded in both tool-call paths, so a
    momentarily unresolvable ``observability.audit`` killed the tool call and
    failed the whole turn — for a logging concern (#3366). That happens in the
    wild: the host is often run out of a live checkout, and a lazy import landing
    mid-edit or mid-branch-switch resolves against a tree that doesn't have the
    file yet (the #2298 hazard — eagerly imported modules are immune, lazily
    imported ones are not).

    This matches the guard the other two audit call sites already use
    (``plugins/execute_code/engine.py``, ``graph/plugins/installer.py``); this
    middleware was the one place missing it, and the hottest of the three.

    Nothing is cached. The failure is transient by nature, so the next call
    re-imports and self-heals; on the happy path this is a ``sys.modules`` hit.
    """
    global _OBS_UNAVAILABLE_LOGGED
    try:
        from observability import metrics, tracing
        from observability.audit import audit_logger

        return audit_logger, tracing, metrics
    except Exception:  # noqa: BLE001 — observability must never break a tool call
        if not _OBS_UNAVAILABLE_LOGGED:
            _OBS_UNAVAILABLE_LOGGED = True
            log.exception("[audit] observability unavailable — tool calls run unaudited until it resolves")
        return _NoopAudit, _NoopTracing, _NoopMetrics


class AuditMiddleware(AgentMiddleware):
    """Log all tool calls to audit, Langfuse, Prometheus, and local telemetry."""

    def __init__(self):
        super().__init__()

    def wrap_tool_call(self, request, handler):
        return self._handle_tool_call(request, handler)

    async def awrap_tool_call(self, request, handler):
        return await self._ahandle_tool_call(request, handler)

    def _handle_tool_call(self, request, handler):
        """Sync wrapper — times and logs tool execution."""
        audit_logger, tracing, metrics = _observability()

        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        session_id = tracing.current_session_id()

        t0 = time.monotonic()
        try:
            result = None
            error_class = None
            try:
                result = handler(request)
            except BaseException as exc:
                error_class = type(exc).__name__
                raise
            finally:
                _record_tool_telemetry(request, result, error_class, t0)
            duration_ms = int((time.monotonic() - t0) * 1000)

            # Transcript redaction: scrub credentials from result.content before it
            # reaches the model, checkpoints, and memory — the full string, not just
            # the 200-char audit summary. Gated by security.redact_tool_output (default
            # True). Only applied to string content; non-string (multipart list, etc.)
            # is left as-is. Audit redaction below always runs regardless of this flag.
            _tc = getattr(result, "content", None)
            if isinstance(_tc, str):
                try:
                    from graph import sdk as _sdk

                    _should_redact = bool(getattr(_sdk.config(), "security_redact_tool_output", True))
                except Exception:  # noqa: BLE001 — never let config lookup break tool execution
                    _should_redact = True
                if _should_redact:
                    result.content = redact(_tc)

            # Duck-type on `.content`: the handler returns a ToolMessage in
            # production, but isinstance is brittle across langchain versions
            # (and ToolMessage subclasses) — capture any message-like result.
            content = ""
            result_content = getattr(result, "content", None)
            if result_content is not None:
                content = str(result_content)[:200]
            success = not _result_failed(result, content)

            safe_args = redact(args)
            safe_content = redact(content)

            audit_logger.log(
                session_id=session_id,
                tool=tool_name,
                args=safe_args,
                result_summary=safe_content,
                duration_ms=duration_ms,
                success=success,
            )
            tracing.trace_tool_call(
                tool_name=tool_name,
                args=safe_args,
                result=safe_content,
                duration_ms=duration_ms,
                success=success,
                session_id=session_id,
            )
            metrics.record_tool_call(tool_name, success, duration_ms / 1000)

            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            safe_args = redact(args)
            safe_exc = redact(str(exc)[:200])
            audit_logger.log(
                session_id=session_id,
                tool=tool_name,
                args=safe_args,
                result_summary=safe_exc,
                duration_ms=duration_ms,
                success=False,
            )
            tracing.trace_tool_call(
                tool_name=tool_name,
                args=safe_args,
                result=safe_exc,
                duration_ms=duration_ms,
                success=False,
                session_id=session_id,
            )
            metrics.record_tool_call(tool_name, False, duration_ms / 1000)
            raise

    async def _ahandle_tool_call(self, request, handler):
        """Async wrapper — same logic, async execution."""
        audit_logger, tracing, metrics = _observability()

        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        session_id = tracing.current_session_id()

        t0 = time.monotonic()
        try:
            result = None
            error_class = None
            try:
                result = await handler(request)
            except BaseException as exc:
                error_class = type(exc).__name__
                raise
            finally:
                _record_tool_telemetry(request, result, error_class, t0)
            duration_ms = int((time.monotonic() - t0) * 1000)

            # Transcript redaction — same logic as the sync path above.
            _tc = getattr(result, "content", None)
            if isinstance(_tc, str):
                try:
                    from graph import sdk as _sdk

                    _should_redact = bool(getattr(_sdk.config(), "security_redact_tool_output", True))
                except Exception:  # noqa: BLE001 — never let config lookup break tool execution
                    _should_redact = True
                if _should_redact:
                    result.content = redact(_tc)

            # Duck-type on `.content`: the handler returns a ToolMessage in
            # production, but isinstance is brittle across langchain versions
            # (and ToolMessage subclasses) — capture any message-like result.
            content = ""
            result_content = getattr(result, "content", None)
            if result_content is not None:
                content = str(result_content)[:200]
            success = not _result_failed(result, content)

            safe_args = redact(args)
            safe_content = redact(content)

            audit_logger.log(
                session_id=session_id,
                tool=tool_name,
                args=safe_args,
                result_summary=safe_content,
                duration_ms=duration_ms,
                success=success,
            )
            tracing.trace_tool_call(
                tool_name=tool_name,
                args=safe_args,
                result=safe_content,
                duration_ms=duration_ms,
                success=success,
                session_id=session_id,
            )
            metrics.record_tool_call(tool_name, success, duration_ms / 1000)

            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            safe_args = redact(args)
            safe_exc = redact(str(exc)[:200])
            audit_logger.log(
                session_id=session_id,
                tool=tool_name,
                args=safe_args,
                result_summary=safe_exc,
                duration_ms=duration_ms,
                success=False,
            )
            tracing.trace_tool_call(
                tool_name=tool_name,
                args=safe_args,
                result=safe_exc,
                duration_ms=duration_ms,
                success=False,
                session_id=session_id,
            )
            metrics.record_tool_call(tool_name, False, duration_ms / 1000)
            raise
