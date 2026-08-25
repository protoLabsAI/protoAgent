"""Checkpointer ``thread_id`` resolution — shared by turn drivers and tool bodies."""

from __future__ import annotations

import logging

from runtime.state import STATE

log = logging.getLogger("protoagent.graph")


def resolve_thread_id(request_metadata: dict | None, session_id: str) -> str:
    """Resolve the checkpointer ``thread_id`` for a session (#571).

    Template default keys A2A sessions by conversation id (``a2a:<session_id>``),
    prefixed to isolate them from non-streaming chat in the shared checkpointer. A fork
    can register a resolver ``(request_metadata, session_id) -> str`` via a plugin
    (``register_thread_id_resolver``) to scope memory from request metadata — e.g. per
    project — with no core edits. Falls back to the default when no resolver is
    registered or a custom resolver errors or returns a falsy value.
    """
    resolver = getattr(STATE, "thread_id_resolver", None)
    if resolver is not None:
        try:
            thread_id = resolver(request_metadata or {}, session_id)
            if thread_id:
                return str(thread_id)
            log.warning("[thread_id] resolver returned falsy; using default")
        except Exception:
            log.exception("[thread_id] custom resolver failed; using default")
    return f"a2a:{session_id}"
