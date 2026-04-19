"""AuditMiddleware — logs tool calls to audit.py, Langfuse, and Prometheus.

Wraps every tool execution with timing, success/failure tracking,
and observability integration. Reuses existing audit/tracing/metrics modules.
"""

import time
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from langgraph.prebuilt.chat_agent_executor import AgentState

from graph.middleware.redaction import redact


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
        from audit import audit_logger
        import tracing
        import metrics

        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        session_id = tracing.current_session_id()

        t0 = time.monotonic()
        try:
            result = handler(request)
            duration_ms = int((time.monotonic() - t0) * 1000)

            content = ""
            if isinstance(result, ToolMessage):
                content = str(result.content)[:200]
            success = not content.startswith("Error")

            safe_args = redact(args)
            safe_content = redact(content)

            audit_logger.log(
                session_id=session_id, tool=tool_name, args=safe_args,
                result_summary=safe_content, duration_ms=duration_ms, success=success,
            )
            tracing.trace_tool_call(
                tool_name=tool_name, args=safe_args, result=safe_content,
                duration_ms=duration_ms, success=success, session_id=session_id,
            )
            metrics.record_tool_call(tool_name, success, duration_ms / 1000)

            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            safe_args = redact(args)
            safe_exc = redact(str(exc)[:200])
            audit_logger.log(
                session_id=session_id, tool=tool_name, args=safe_args,
                result_summary=safe_exc, duration_ms=duration_ms, success=False,
            )
            tracing.trace_tool_call(
                tool_name=tool_name, args=safe_args, result=safe_exc,
                duration_ms=duration_ms, success=False, session_id=session_id,
            )
            metrics.record_tool_call(tool_name, False, duration_ms / 1000)
            raise

    async def _ahandle_tool_call(self, request, handler):
        """Async wrapper — same logic, async execution."""
        from audit import audit_logger
        import tracing
        import metrics

        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        session_id = tracing.current_session_id()

        t0 = time.monotonic()
        try:
            result = await handler(request)
            duration_ms = int((time.monotonic() - t0) * 1000)

            content = ""
            if isinstance(result, ToolMessage):
                content = str(result.content)[:200]
            success = not content.startswith("Error")

            safe_args = redact(args)
            safe_content = redact(content)

            audit_logger.log(
                session_id=session_id, tool=tool_name, args=safe_args,
                result_summary=safe_content, duration_ms=duration_ms, success=success,
            )
            tracing.trace_tool_call(
                tool_name=tool_name, args=safe_args, result=safe_content,
                duration_ms=duration_ms, success=success, session_id=session_id,
            )
            metrics.record_tool_call(tool_name, success, duration_ms / 1000)

            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            safe_args = redact(args)
            safe_exc = redact(str(exc)[:200])
            audit_logger.log(
                session_id=session_id, tool=tool_name, args=safe_args,
                result_summary=safe_exc, duration_ms=duration_ms, success=False,
            )
            tracing.trace_tool_call(
                tool_name=tool_name, args=safe_args, result=safe_exc,
                duration_ms=duration_ms, success=False, session_id=session_id,
            )
            metrics.record_tool_call(tool_name, False, duration_ms / 1000)
            raise
