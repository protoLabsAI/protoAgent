"""Session-id resolution for tool bodies.

A deliberately tiny module: tools that need the calling chat session import it
from here rather than from ``tools.lg_tools`` (the whole core toolset, which
drags the scheduler in). Keep it free of heavy imports — ``observability.tracing``
is imported lazily so a test's ``monkeypatch.setattr(tracing,
"current_session_id", ...)`` is honoured at call time.
"""

from __future__ import annotations

from typing import Any


def _session_id_from(state: Any) -> str:
    """Resolve the originating session id from inside a TOOL BODY.

    The graph state (``graph/state.py``) reliably carries ``session_id`` at
    tool-execution time — every turn's graph input stamps it. The
    ``tracing.current_session_id()`` contextvar is visible to MIDDLEWARE but NOT
    to a tool body under LangGraph (the tool runs in a different execution
    context), so it silently reads empty there — which is why ``wait`` dropped
    its same-session resume to the Activity thread (ADR 0053) and why ``set_goal``
    would refuse with "No active session". Prefer the injected state; keep the
    contextvar only as a fallback for tools invoked outside a graph turn."""
    from observability import tracing

    sid = ""
    if isinstance(state, dict):
        sid = (state.get("session_id") or "").strip()
    return sid or (tracing.current_session_id() or "")


def origin_session_for(context_id: str) -> str:
    """Resolve work to its conversation in at most three durable-store hops.

    Origin is display/routing provenance, never authorization. Missing stores,
    records, read failures and unresolved chains yield an empty origin.
    """
    try:
        from runtime.state import STATE

        for _ in range(3):
            if context_id.startswith("job:"):
                origin = STATE.inbox_store.origin_for_context(context_id) if STATE.inbox_store is not None else ""
                if not origin and STATE.scheduler is not None:
                    origin = STATE.scheduler.origin_for_context(context_id)
                context_id = origin or ""
            elif context_id.startswith("background:"):
                manager = STATE.background_mgr
                job = manager.store.get(context_id.removeprefix("background:")) if manager is not None else None
                context_id = (job.origin_session or "") if job is not None else ""
            else:
                return context_id
        return "" if context_id.startswith(("job:", "background:")) else context_id
    except Exception:
        return ""


def _origin_session_from(state: Any) -> str:
    """Resolve the conversation behind the current tool invocation's state."""
    return origin_session_for(_session_id_from(state))
