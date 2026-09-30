"""The one shape check for a caller-supplied chat session id.

A session id is chosen by whoever opens the conversation — the console (``chat-<ms>-<rand>``),
the Zed shim (``chat-zed-...``), ``/api/chat`` callers (``api-...`` when minted), A2A peers
(their ``contextId``: a UUID, or ids carrying ``:``), ``/v1`` (``openai-compat-...``) — and it
travels a long way from there: checkpointer thread keys, the task store's ``context_id``
column, and per-session files under instance stores (session-memory summaries, goal state,
trajectory logs). The stores each map the id onto a filename themselves; this module is the
single place the HTTP and A2A entry points agree on which ids are acceptable at all, so every
surface answers the same question the same way.

The rule is deliberately about SHAPE, not an allow-list alphabet: every id a first-party client
mints (and every id an existing on-disk session carries) passes unchanged, and only ids no
client produces are refused —

* empty, or ``.`` / ``..`` on their own;
* longer than :data:`MAX_SESSION_ID_LEN` (the chat tombstone table's key width);
* containing a path separator (``/`` or ``\\``), a control character (NUL included), ``%``
  (reserved: the session-memory filename mapper encodes ``:`` as ``%3A``, so a literal ``%``
  could alias another session's encoded name), or one of the characters Windows forbids in
  filenames (``< > " | ? *``).

This is a shape rule, not a complete portable-filename validator: Windows reserved device names
(``CON``, ``NUL``, ...) and trailing dots/spaces are not refused here. Each store that turns an
id into a filename still owns its own mapping and its own check that the resolved path stays
under its base (e.g. ``graph.middleware.memory.contained_in``).

``/v1`` keeps its own normalization (``operator_api.chat_routes._v1_session_id``) because the
OpenAI ``user`` field is free text by contract; its output always satisfies this check.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

#: The chat tombstone table's key (``chat_session_tombstones.context_id``) is ``String(255)``.
MAX_SESSION_ID_LEN = 255

_FORBIDDEN_CHARS = frozenset('/\\%<>"|?*')


def session_id_problem(session_id: str) -> str | None:
    """Why *session_id* is not an acceptable session id, or ``None`` when it is.

    Pure and total: never raises, for any ``str``."""
    if not isinstance(session_id, str) or not session_id:
        return "session id is empty"
    if session_id in (".", ".."):
        return "session id may not be '.' or '..'"
    if len(session_id) > MAX_SESSION_ID_LEN:
        return f"session id is longer than {MAX_SESSION_ID_LEN} characters"
    for ch in session_id:
        if ch in _FORBIDDEN_CHARS:
            return f"session id may not contain {ch!r}"
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return "session id may not contain control characters"
    return None


def is_valid_session_id(session_id: str) -> bool:
    return session_id_problem(session_id) is None


def require_session_id(session_id: str) -> str:
    """Return *session_id* unchanged when acceptable; raise ``ValueError`` (with the reason)
    otherwise. Suits a pydantic ``AfterValidator`` — FastAPI turns the error into a 422."""
    problem = session_id_problem(session_id)
    if problem is not None:
        raise ValueError(problem)
    return session_id


#: A FastAPI parameter / pydantic field type carrying the check: an unacceptable id is a 422
#: before any handler runs. Every ``{session_id}`` path parameter on the chat and goal routes
#: uses it.
SessionId = Annotated[str, AfterValidator(require_session_id)]
