"""Checkpointer ``thread_id`` resolution (#571) — one implementation, neutral module.

Lived in ``server.chat`` as ``_resolve_thread_id``. It moved here because the thread id
is now needed from a *tool body* as well as a turn driver: the delegates plugin records
its delegations on the session's thread, and ``plugins/`` cannot import ``server`` without
either breaking the layering contract or duplicating this logic — and a second copy of
"which thread is this session" is exactly the drift that would put a delegation's record
on a different thread from the turn that spawned it.

``server.chat._resolve_thread_id`` is now a thin alias, so every existing caller and the
test suite keep working.
"""

from __future__ import annotations

import logging

from runtime.state import STATE

log = logging.getLogger("protoagent.server")


def resolve_thread_id(request_metadata: dict | None, session_id: str) -> str:
    """Resolve the checkpointer ``thread_id`` for a session (#571).

    Template default keys A2A sessions by conversation id (``a2a:<session_id>``),
    prefixed to isolate them from the non-streaming chat in the shared checkpointer. A
    fork can register a resolver ``(request_metadata, session_id) -> str`` via a plugin
    (``register_thread_id_resolver``) to scope memory off request metadata — e.g.
    per-project working memory — with ZERO edits to any core file. Falls back to the
    default when no resolver is registered or a custom one errors / returns falsy.
    """
    resolver = getattr(STATE, "thread_id_resolver", None)
    if resolver is not None:
        try:
            tid = resolver(request_metadata or {}, session_id)
            if tid:
                return str(tid)
            log.warning("[thread_id] resolver returned falsy; using default")
        except Exception:
            log.exception("[thread_id] custom resolver failed; using default")
    return f"a2a:{session_id}"
