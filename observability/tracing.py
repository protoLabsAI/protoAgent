"""Langfuse tracing for protoAgent.

Primary entry point is ``trace_session`` — an async context manager that
opens a Langfuse observation and makes it the current parent for any
observations created inside its scope (including tool calls, LLM calls
from the LiteLLM gateway, and nested subagent sessions).

Hierarchy
─────────
A typical A2A-submitted task produces a trace like::

    a2a.task (session root)
    ├── agent-turn (LangGraph run)
    │   ├── tool:<my_tool_a>
    │   ├── tool:<my_tool_b>
    │   └── litellm-acompletion            ← from the gateway callback
    └── subagent:worker                    ← if a subagent is dispatched
        ├── tool:<my_tool_c>
        └── litellm-acompletion

Every span shares the same Langfuse trace_id, so clicking one surfaces the
whole run. The trace_id is also stamped onto every audit-log entry via
``current_trace_id()`` so operators can cross-reference a JSONL line to
the Langfuse UI without grep-archaeology.

The OTel "Failed to detach context" filter below handles a real bug that
appears any time an SSE consumer closes the stream early (e.g. an A2A
client breaks out of the `for await` loop after capturing the initial
task event). Keep it.

Graceful degrade
────────────────
When Langfuse isn't configured (or its client errors), every helper in
this module is a no-op. The agent continues; tracing just doesn't land.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
from typing import Any, AsyncIterator, Iterator

_langfuse = None
_enabled = False

# W3C/OTel id shapes (what Langfuse v3+/v4 uses on the wire). A caller's ids
# must match these to be JOINable — anything else falls back to a fresh trace
# rather than crashing the turn or poisoning the SDK with a bogus context.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


# Silence the harmless "Failed to detach context" error emitted by
# OpenTelemetry when Langfuse's span context manager exits in a
# different async context than where it was attached. OTel logs this
# as `_logger.error("Failed to detach context", exc_info=True)` —
# the actual "was created in a different Context" string lives only
# in `exc_info`, NOT in the formatted message. Match on the message
# text instead. The detach-context call site only fires this line
# for the cross-context case in practice, so the filter is precise
# enough to leave other OTel errors alone.
logging.getLogger("opentelemetry.context").addFilter(
    lambda record: "Failed to detach context" not in record.getMessage()
)

# Holds the current Langfuse trace_id for the active async context. Audit
# logging + error handlers read this to cross-reference records back to
# the trace that produced them.
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_protoagent_trace_id",
    default="",
)

# Holds the A2A/chat session_id so middleware (AuditMiddleware) and
# audit logging can stamp it without needing access to graph state.
_session_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_protoagent_session_id",
    default="",
)


def init() -> None:
    """Connect to Langfuse if LANGFUSE_{PUBLIC,SECRET}_KEY are set. Idempotent."""
    global _langfuse, _enabled

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_URL", "http://host.docker.internal:3001")

    if not public_key or not secret_key:
        print("[tracing] Langfuse not configured. Tracing disabled.")
        return

    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        _enabled = True
        print(f"[tracing] Langfuse initialized -> {host}")
    except ImportError:
        print("[tracing] langfuse not installed. Tracing disabled.")
    except Exception as e:
        print(f"[tracing] Langfuse init failed: {e}. Tracing disabled.")


def is_enabled() -> bool:
    return _enabled


def current_trace_id() -> str:
    """Return the trace_id of the currently-active session (or empty)."""
    return _trace_id_ctx.get()


def current_session_id() -> str:
    """Return the session_id of the currently-active session (or empty)."""
    return _session_id_ctx.get()


def current_trace_context() -> dict | None:
    """The active trace context as ``{"trace_id": ..., "span_id": ...}``, or None.

    Used to PROPAGATE the trace across process boundaries — outbound A2A
    dispatches attach it as ``a2a.trace`` metadata (the receiver's
    ``trace_session`` joins it via ``trace_context``), and the gateway
    trace-join middleware stamps it onto LLM request metadata so LiteLLM's
    Langfuse callback lands generations in the SAME trace.

    ``span_id`` is the current observation (present only when the SDK exposes
    one); ``trace_id`` alone is still a valid, joinable context. Returns None
    when tracing is disabled or no trace is active — callers skip propagation.
    """
    if not _enabled or _langfuse is None:
        return None
    trace_id = ""
    try:
        trace_id = _langfuse.get_current_trace_id() or ""
    except Exception:  # noqa: BLE001 — propagation is best-effort
        pass
    trace_id = trace_id or _trace_id_ctx.get()
    if not trace_id:
        return None
    ctx: dict = {"trace_id": trace_id}
    try:
        span_id = _langfuse.get_current_observation_id()
        if span_id:
            ctx["span_id"] = span_id
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _caller_trace_context(metadata: dict | None) -> dict | None:
    """Build a Langfuse ``trace_context`` from ``caller_trace_id`` /
    ``caller_span_id`` metadata (the ids an A2A caller sent as ``a2a.trace``).

    Malformed ids → None (fresh trace) — never let a bad caller id crash a
    turn or feed the SDK an invalid W3C context."""
    if not metadata:
        return None
    trace_id = str(metadata.get("caller_trace_id") or "").strip().lower()
    if not _TRACE_ID_RE.match(trace_id):
        return None
    ctx: dict = {"trace_id": trace_id}
    span_id = str(metadata.get("caller_span_id") or "").strip().lower()
    if _SPAN_ID_RE.match(span_id):
        ctx["parent_span_id"] = span_id
    return ctx


@contextlib.asynccontextmanager
async def trace_session(
    session_id: str,
    name: str = "agent-session",
    metadata: dict | None = None,
) -> AsyncIterator[Any]:
    """Open a session-level Langfuse observation that child observations nest under.

    Any ``_langfuse.start_observation(...)`` call (including those made by
    ``trace_tool_call`` below) becomes a child of this span for the duration
    of the ``async with`` block.

    The block always runs — if Langfuse isn't configured or raises on setup,
    the manager yields None and proceeds. Never let tracing failures cascade
    into the agent's execution path.

    Usage::

        async with tracing.trace_session(session_id, name="a2a.task",
                                         metadata={"task_id": tid}):
            ... # LangGraph run, tool calls, subagent dispatches
            ... # all land as children of this span

    ``session_id`` is threaded into both the metadata and the Langfuse
    contextvar so audit records created inside the scope can be cross-
    referenced to the trace.

    Fleet tracing: when ``metadata`` carries ``caller_trace_id`` (and
    optionally ``caller_span_id``) — the ids an upstream agent sent as
    ``a2a.trace`` — the session span JOINS that trace via Langfuse's
    ``trace_context`` instead of opening a fresh one, so a hub→member
    delegation renders as ONE distributed trace. The ids are still stamped
    into the span metadata; malformed ids degrade to a fresh trace.
    """
    # Always set session_id so AuditMiddleware can read it even when
    # Langfuse is disabled.
    sid_token = _session_id_ctx.set(session_id)

    if not _enabled or _langfuse is None:
        try:
            yield None
        finally:
            # reset can raise if the generator is torn down in a different
            # context than the one that set the token (e.g. an SSE client
            # disconnects mid-stream and the async generator is closed by a
            # different task). The contextvar resets itself on context exit,
            # so swallowing here is safe.
            try:
                _session_id_ctx.reset(sid_token)
            except ValueError:
                pass
        return

    ctx = None
    token = None
    try:
        trace_context = _caller_trace_context(metadata)
        ctx = _langfuse.start_as_current_observation(
            trace_context=trace_context,
            name=name,
            metadata={
                **(metadata or {}),
                "session_id": session_id,
                "tags": [os.environ.get("AGENT_NAME", "protoagent")],
            },
        )
        span = ctx.__enter__()
        # Joined session: the span reports the CALLER's trace id — that's what
        # audit records and downstream propagation must carry.
        trace_id = (
            getattr(span, "trace_id", "")
            or (trace_context or {}).get("trace_id", "")
            or getattr(span, "id", "")
        )
        token = _trace_id_ctx.set(trace_id)
        yield span
    except Exception as e:
        print(f"[tracing] trace_session({name}) error: {e}")
        yield None
    finally:
        try:
            _session_id_ctx.reset(sid_token)
        except Exception:
            pass
        if token is not None:
            try:
                _trace_id_ctx.reset(token)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


@contextlib.contextmanager
def trace_span(
    name: str,
    metadata: dict | None = None,
    as_type: str = "span",
) -> Iterator[Any]:
    """Open a child observation in the CURRENT trace for the duration of the block.

    Used for boundary spans — e.g. ``subagent:<type>`` around a subagent run so
    the subagent's tool/LLM observations nest under one node instead of
    scattering across the session span. Nests under whatever observation is
    current (the ``trace_session`` root, or an outer ``trace_span``).

    Contract mirrors ``trace_session``: the block ALWAYS runs; when tracing is
    disabled or the SDK errors on setup, it yields None and proceeds. A body
    exception propagates unchanged (the span still closes) — tracing never
    alters control flow.
    """
    if not _enabled or _langfuse is None:
        yield None
        return

    ctx = None
    span = None
    try:
        ctx = _langfuse.start_as_current_observation(
            name=name,
            as_type=as_type,
            metadata=metadata or {},
        )
        span = ctx.__enter__()
    except Exception as e:  # noqa: BLE001 — never fail the wrapped work for tracing
        print(f"[tracing] trace_span({name}) error: {e}")
        ctx = None
    try:
        yield span
    finally:
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def trace_tool_call(
    tool_name: str,
    args: dict,
    result: str,
    duration_ms: int,
    success: bool,
    session_id: str = "",
) -> Any:
    """Log a completed tool execution as a child observation.

    When called inside a ``trace_session`` scope, this nests under the
    session span automatically — Langfuse's internal current-observation
    stack threads the parent without explicit wiring.
    """
    if not _enabled or _langfuse is None:
        return None

    # Truncate oversize args to keep Langfuse payloads lean. The full args
    # are already in the audit log for forensic reconstruction.
    safe_args = {}
    for k, v in (args or {}).items():
        sv = str(v)
        safe_args[k] = sv[:500] if len(sv) > 500 else v

    try:
        span = _langfuse.start_observation(
            name=f"tool:{tool_name}",
            as_type="tool",
            input=safe_args,
            output=(result or "")[:1000],
            metadata={
                "duration_ms": duration_ms,
                "success": success,
                "session_id": session_id,
                "trace_id": _trace_id_ctx.get(),
            },
            level="ERROR" if not success else "DEFAULT",
        )
        span.end()
        return span
    except Exception:
        return None


def score_current_trace(name: str, value: float, comment: str = "") -> None:
    """Attach a numeric score to the currently-active trace.

    Examples:
        - score_current_trace("tool_success_rate", 1.0 if all_succeeded else 0.0)
        - score_current_trace("latency_p95_ms", duration_ms)
        - score_current_trace("verdict", 1.0 if verdict == "PASS" else 0.0,
                              comment="triage outcome")
    """
    if not _enabled or _langfuse is None:
        return
    try:
        _langfuse.score_current_trace(name=name, value=value, comment=comment)
    except Exception:
        pass


def flush() -> None:
    """Flush any buffered observations. Call before process exit."""
    if _enabled and _langfuse is not None:
        try:
            _langfuse.flush()
        except Exception:
            pass
