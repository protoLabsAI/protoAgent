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
