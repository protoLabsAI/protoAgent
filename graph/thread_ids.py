"""Checkpointer ``thread_id`` resolution — shared by turn drivers and tool bodies."""

from __future__ import annotations

import logging

from runtime.state import STATE

log = logging.getLogger("protoagent.server")


def resolve_thread_id(request_metadata: dict | None, session_id: str) -> str:
    """Resolve the checkpointer thread for a session, honoring a plugin resolver."""
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
