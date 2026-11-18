"""Which conversations carry private (human-operator) turns — the write-side memory gate.

A privately-steered turn's input and reply stay in the conversation's checkpoint, where a
later ordinary turn, an idle gist, a compaction archive or the retirement harvest would
otherwise derive peer-readable session memory from them. Only the turn's INPUT carries an
audience stamp (its replies and tool results do not), so the protection follows the
conversation: once a private turn has run on a session, the session card, the idle gist
and the retirement harvest skip it — the same shape as the incognito skip, but durable,
because a later ordinary turn cannot clear it. The compaction archive is the exception:
it is a transcript, so it keeps the ordinary turns and drops whole private turns
(:func:`public_part`).

The mark is a content-free file under the session-memory dir, named by a digest of the
session id (any id is safe as a filename, and the listing walkers only read ``*.json``
at the top level). A failed write falls back to an in-process mark and logs loudly.
Durable task ownership also protects pre-marker conversations. Their unstamped
transcripts cannot be safely partitioned, so archives are withheld too. A new marker
records this condition when older private tasks exist; a later stamped turn cannot
clear it. Conversations without private turns keep their existing writer behavior.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

from graph.fence_scope import in_private_segment

log = logging.getLogger(__name__)

_DIR = ".private-sessions"
_fallback: set[str] = set()
_mark_lock = threading.Lock()


def _marker(session_id: str) -> str:
    from graph.middleware.memory import memory_path

    return os.path.join(memory_path(), _DIR, hashlib.sha256(session_id.encode("utf-8")).hexdigest())


def _owned_private_turn(session_id: str) -> bool:
    """Read durable ownership even for turns predating message stamps and markers.

    Writers include synchronous middleware and timer threads, so use a short-lived
    SQLite connection to the runtime engine's database, not its async connection pool.
    Never cache a negative answer: another turn can change ownership at any time.
    """
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from a2a.server.tasks.database_task_store import TaskModel
    from a2a_impl.stores import HUMAN_OPERATOR_OWNER
    from runtime.state import STATE

    engine = getattr(STATE, "a2a_task_engine", None)
    if engine is None:
        return bool(getattr(STATE.graph_config, "private_steering_enabled", False))
    try:
        if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
            return True
        uri = Path(engine.url.database).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            return conn.execute(
                f"SELECT 1 FROM {TaskModel.__tablename__} WHERE context_id=? AND owner=? LIMIT 1",
                (session_id, HUMAN_OPERATOR_OWNER),
            ).fetchone() is not None
    except Exception:
        log.warning("[memory] private task ownership unavailable — withholding shared memory", exc_info=True)
        return True


def mark(session_id: str) -> None:
    """Record that ``session_id`` has a private turn. Idempotent; never raises."""
    if not session_id:
        return
    try:
        from infra.paths import atomic_write

        with _mark_lock:
            path = _marker(session_id)
            if not os.path.exists(path):
                # A new stamped turn must not make older, unstamped turns safe to archive.
                legacy = _owned_private_turn(session_id)
                atomic_write(path, "unlabelled" if legacy else "")
    except Exception:
        _fallback.add(session_id)
        log.exception("[memory] could not persist the private-session mark; holding it in-process")


def is_marked(session_id: str) -> bool:
    if not session_id:
        return False
    if session_id in _fallback:
        return True
    try:
        return os.path.exists(_marker(session_id))
    except Exception:
        return True  # an unreadable mark must not open the shared writers


def public_part(messages: list, *session_ids: str | None) -> list:
    """The part of a conversation's ``messages`` a shared archive may hold.

    A conversation with no private turn — unmarked, nothing stamped, no private turn
    running — keeps every message, exactly as before. Otherwise whole private TURNS are dropped, fail closed: a
    private-stamped message opens a private span that runs until the next ordinary operator
    message, so the turn's unstamped replies and tool results go with its input. A compaction
    summary opens one too — it may have folded a private turn, and the tail kept after it
    may begin mid-turn. (Where the private replies are themselves stamped, the partition is
    fully labelled and the stamps alone decide, as they always did.)"""
    messages = list(messages or [])
    from graph.conversation_audience import is_private_message, public_messages

    if any(_unlabelled_private_turns(s) for s in session_ids if s):
        return []  # no trustworthy boundary for a pre-marker private turn
    known = in_private_segment() or any(is_marked(s) for s in session_ids if s)
    if not known and not any(is_private_message(m) for m in messages):
        return public_messages(messages)
    from langchain_core.messages import HumanMessage

    if any(is_private_message(m) and not isinstance(m, HumanMessage) for m in messages):
        # A fully-labelled partition stamps its private replies too: the stamps alone decide.
        return public_messages(messages)

    from graph.compaction_engine import OPERATOR, human_kind, is_summary_message

    kept, private = [], False
    for message in messages:
        if is_private_message(message) or is_summary_message(message):
            private = True
        elif private and isinstance(message, HumanMessage) and human_kind(message) == OPERATOR:
            private = False
        if not private:
            kept.append(message)
    return kept


def private_kwargs() -> dict:
    """``additional_kwargs`` stamping a message a private turn puts into the checkpoint
    (empty outside a private segment, so an ordinary message is built exactly as before)."""
    from graph.conversation_audience import AUDIENCE_KWARG, PRIVATE_AUDIENCE

    return {AUDIENCE_KWARG: PRIVATE_AUDIENCE} if in_private_segment() else {}


def withheld(session_id: str) -> bool:
    """Whether shared derived memory must NOT be written for ``session_id`` right now: a
    private turn is running (it is marked on the way), or one ran earlier."""
    if in_private_segment():
        mark(session_id)
        return True
    return is_marked(session_id) or _owned_private_turn(session_id)


def _unlabelled_private_turns(session_id: str) -> bool:
    if session_id in _fallback:
        return True
    try:
        with open(_marker(session_id), encoding="utf-8") as stream:
            return bool(stream.read())
    except FileNotFoundError:
        return _owned_private_turn(session_id)
    except Exception:
        return True
