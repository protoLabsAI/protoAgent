"""AuditMiddleware — logs tool calls to audit.py, Langfuse, and Prometheus.

Wraps every tool execution with timing, success/failure tracking,
and observability integration. Reuses existing audit/tracing/metrics modules.
"""

import logging
import time

from langchain.agents.middleware import AgentMiddleware


from graph.middleware.redaction import redact

log = logging.getLogger(__name__)

# Warn once, not once per tool call: the failure mode this guards against is a
# retry storm, and a per-call traceback would be the loudest thing in the log.
_OBS_UNAVAILABLE_LOGGED = False


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
    """Log all tool calls to audit, Langfuse, and Prometheus."""

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
            result = handler(request)
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
            success = not content.startswith("Error")

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
            result = await handler(request)
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
            success = not content.startswith("Error")

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
