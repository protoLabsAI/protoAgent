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


# Resolver inputs persisted per session so a turn that carries NO request metadata of its
# own — an idle steer start or a parked resume (ADR 0131 P1b.2) — resolves the SAME
# checkpointer thread as the original turn, instead of re-deriving it from empty metadata
# under a custom resolver (#571). The template default ignores metadata, so this is inert
# unless a fork registered a resolver. Bounded + in-memory (a restart just falls back to the
# default thread), like the other per-session caches this runtime keeps.
_RESOLVER_METADATA: dict[str, dict] = {}
_RESOLVER_METADATA_MAX = 1024


def remember_resolver_metadata(session_id: str, request_metadata: dict | None) -> None:
    """Persist this turn's resolver inputs so the newest request's scoping wins.

    A metadata-less turn clears earlier inputs: a custom resolver can map empty metadata
    to a different thread. An empty session id is ignored.
    """
    if not session_id:
        return
    if request_metadata is None or request_metadata == {}:
        _RESOLVER_METADATA.pop(session_id, None)
        return
    if not isinstance(request_metadata, dict):
        return
    if session_id not in _RESOLVER_METADATA and len(_RESOLVER_METADATA) >= _RESOLVER_METADATA_MAX:
        # Evict the oldest-inserted entry; dict preserves insertion order.
        _RESOLVER_METADATA.pop(next(iter(_RESOLVER_METADATA)), None)
    _RESOLVER_METADATA[session_id] = dict(request_metadata)


def resolver_metadata(session_id: str) -> dict:
    """A copy of the resolver inputs persisted for ``session_id`` (``{}`` when none)."""
    return dict(_RESOLVER_METADATA.get(session_id) or {})


def forget_resolver_metadata(session_id: str) -> None:
    """Drop the persisted resolver inputs for ``session_id`` (session deletion / test reset)."""
    _RESOLVER_METADATA.pop(session_id, None)
