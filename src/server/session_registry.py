"""Server-held chat session LIST state (ADR 0130 S6, D1/D2).

The server — not any device — owns each chat session's LIST state: when it was
created, its title, and whether its tab is open or closed. S2 made the per-session
LIVE state (idle/running/paused) server-authoritative; S6 does the same for the tab
list itself, so every device shows the same chats (Josh, 2026-11-13: desktop and
phone showed different tabs).

A durable table beside the chat tombstones in ``a2a-tasks.db`` holds one row per
registered console chat — ``(context_id, created_at, title, closed_at)``. Closing a
tab sets ``closed_at`` (it hides everywhere but the chat stays retrievable — its
turns are untouched); deleting removes the row (the delete route separately tombstones
and retires the turns). The list is bounded by real console usage, so it is read whole.

Every list transition publishes ONE change-log event the S1 log records and a resuming
client replays (D2: "sessions created, renamed and deleted"):

- ``session.created``  — a new session registered (first send, or the one-time migrate).
- ``session.renamed``  — its title changed.
- ``session.closed``   — its tab closed (``closed_at`` set).
- ``session.reopened`` — a closed tab reopened (``closed_at`` cleared, e.g. a new send).
- ``session.deleted``  — the session deleted (row gone).

Events carry ``{session_id, title, kind}`` — identities, not payloads (D2): a client
reacts by refetching ``GET /api/chat/sessions``, which is the truth.
``operator_api.chat_routes`` reaches these over the grandfathered ``server.chat``
re-export edge, never by importing this module directly (the import-layering invariant,
same as ``server.session_state``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from a2a_impl.stores import CHAT_SESSION_LIST_TABLE
from runtime.state import STATE

log = logging.getLogger("protoagent.server")

_TITLE_MAX = 512

# The list-event topics (ADR 0130 S6). Distinct from S2's ``session.changed`` (live
# status): the change log records both kinds, and a client reacts to either by refetching.
CREATED = "session.created"
RENAMED = "session.renamed"
CLOSED = "session.closed"
REOPENED = "session.reopened"
DELETED = "session.deleted"

_table_holder: list[Any | None] = [None]


def _bus():
    """The server's bus, at call time (mirrors ``session_state._bus`` — nothing from
    ``server`` is imported at module load, and a test's monkeypatched bus is picked up)."""
    from server import _event_bus

    return _event_bus


def _is_chat_session(session_id: str) -> bool:
    from server import session_state

    return session_state.is_chat_session(session_id)


def _engine():
    return getattr(STATE, "a2a_task_engine", None)


def _table():
    table = _table_holder[0]
    if table is None:
        from sqlalchemy import Column, DateTime, MetaData, String, Table

        metadata = MetaData()
        table = Table(
            CHAT_SESSION_LIST_TABLE,
            metadata,
            Column("context_id", String(255), primary_key=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("title", String(_TITLE_MAX), nullable=True),
            Column("closed_at", DateTime(timezone=True), nullable=True),
        )
        _table_holder[0] = table
    return table


async def _ensure(conn) -> Any:
    table = _table()
    await conn.run_sync(table.metadata.create_all, tables=[table])
    return table


def _clean_title(title: str | None) -> str | None:
    if title is None:
        return None
    text = str(title).strip()
    return text[:_TITLE_MAX] if text else None


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    dt = _utc(value)
    return dt.isoformat() if dt else None


def _publish(topic: str, session_id: str, *, title: str | None = None) -> bool:
    """Publish ONE list event. Never raises — callers sit inside routes and turn paths."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return False
    data: dict[str, Any] = {"session_id": sid, "kind": topic.split(".")[-1]}
    if title is not None:
        data["title"] = title
    try:
        _bus().publish(topic, data)
    except Exception:  # noqa: BLE001 — a bus failure must never break the route
        log.exception("[session-registry] %s publish failed for %s", topic, sid)
        return False
    return True


# ── writes ───────────────────────────────────────────────────────────────────


async def register_session(session_id: str, *, title: str | None = None, created_at: Any = None) -> None:
    """Register a chat session in the list (a first send, or the one-time client migrate).

    Idempotent create-or-reopen: a new row publishes ``session.created``; an existing CLOSED
    row reopens (``session.reopened``) — sending to a closed chat brings its tab back
    everywhere; an existing OPEN row only updates a changed title (``session.renamed``)."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return
    engine = _engine()
    if engine is None:
        return
    from sqlalchemy import insert, select, update

    title = _clean_title(title)
    now = datetime.now(timezone.utc)
    created = _utc(created_at) or now
    event: tuple[str, str | None] | None = None
    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            row = (
                await conn.execute(select(table.c.title, table.c.closed_at).where(table.c.context_id == sid))
            ).first()
            if row is None:
                await conn.execute(
                    insert(table).values(context_id=sid, created_at=created, title=title, closed_at=None)
                )
                event = (CREATED, title)
            else:
                was_closed = row.closed_at is not None
                new_title = title if title is not None else row.title
                title_changed = title is not None and title != row.title
                if was_closed or title_changed:
                    await conn.execute(
                        update(table).where(table.c.context_id == sid).values(title=new_title, closed_at=None)
                    )
                if was_closed:
                    event = (REOPENED, new_title)
                elif title_changed:
                    event = (RENAMED, new_title)
    except Exception:  # noqa: BLE001 — a store failure must never break the route
        log.warning("[session-registry] register failed for %s", sid, exc_info=True)
        return
    if event:
        _publish(event[0], sid, title=event[1])


async def rename_session(session_id: str, title: str) -> None:
    """Set a session's title. An unregistered session is registered with it (a renamed empty
    tab is intent) — ``session.created``; an existing one changed — ``session.renamed``."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return
    engine = _engine()
    if engine is None:
        return
    from sqlalchemy import insert, select, update

    clean = _clean_title(title)
    now = datetime.now(timezone.utc)
    event: tuple[str, str | None] | None = None
    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            row = (await conn.execute(select(table.c.title).where(table.c.context_id == sid))).first()
            if row is None:
                await conn.execute(insert(table).values(context_id=sid, created_at=now, title=clean, closed_at=None))
                event = (CREATED, clean)
            elif clean != row.title:
                await conn.execute(update(table).where(table.c.context_id == sid).values(title=clean))
                event = (RENAMED, clean)
    except Exception:  # noqa: BLE001
        log.warning("[session-registry] rename failed for %s", sid, exc_info=True)
        return
    if event:
        _publish(event[0], sid, title=event[1])


async def close_session(session_id: str) -> None:
    """Close a session's tab everywhere — ``closed_at`` set, the chat kept (its turns stay).
    An unregistered session is recorded closed so the state is durable. ``session.closed``."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return
    engine = _engine()
    if engine is None:
        return
    from sqlalchemy import insert, select, update

    now = datetime.now(timezone.utc)
    published = False
    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            row = (
                await conn.execute(select(table.c.title, table.c.closed_at).where(table.c.context_id == sid))
            ).first()
            if row is None:
                await conn.execute(insert(table).values(context_id=sid, created_at=now, title=None, closed_at=now))
                published = True
            elif row.closed_at is None:
                await conn.execute(update(table).where(table.c.context_id == sid).values(closed_at=now))
                published = True
            title = row.title if row else None
    except Exception:  # noqa: BLE001
        log.warning("[session-registry] close failed for %s", sid, exc_info=True)
        return
    if published:
        _publish(CLOSED, sid, title=title)


async def reopen_session(session_id: str) -> None:
    """Reopen a closed tab everywhere (``closed_at`` cleared). ``session.reopened``."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return
    engine = _engine()
    if engine is None:
        return
    from sqlalchemy import select, update

    published = False
    title = None
    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            row = (
                await conn.execute(select(table.c.title, table.c.closed_at).where(table.c.context_id == sid))
            ).first()
            if row is not None and row.closed_at is not None:
                await conn.execute(update(table).where(table.c.context_id == sid).values(closed_at=None))
                published = True
                title = row.title
    except Exception:  # noqa: BLE001
        log.warning("[session-registry] reopen failed for %s", sid, exc_info=True)
        return
    if published:
        _publish(REOPENED, sid, title=title)


async def delete_session(session_id: str) -> None:
    """Drop a session's list row (the delete route separately tombstones + retires its turns)
    and publish ``session.deleted`` so every device removes the tab. Published even when no
    row exists — a delete must propagate whether or not the session was ever registered."""
    sid = str(session_id or "").strip()
    if not _is_chat_session(sid):
        return
    engine = _engine()
    if engine is not None:
        from sqlalchemy import delete

        try:
            async with engine.begin() as conn:
                table = await _ensure(conn)
                await conn.execute(delete(table).where(table.c.context_id == sid))
        except Exception:  # noqa: BLE001 — the delete already happened; the row drop is best-effort
            log.warning("[session-registry] list-row delete failed for %s", sid, exc_info=True)
    _publish(DELETED, sid)


async def migrate_sessions(items: list[dict]) -> int:
    """Register the client's open tabs + titles ONCE on first sync (ADR 0130 S6, r4): the
    device-local DISMISSED set and client-only titles become server-held so nobody loses a
    chat. Only CREATES rows that don't exist — an already-registered session keeps the server
    copy (the server wins after the first migrate). Returns the number of new rows.
    ``items``: ``[{session_id, title?, created_at?}]``."""
    engine = _engine()
    if engine is None or not items:
        return 0
    from sqlalchemy import insert, select

    created: list[tuple[str, str | None]] = []
    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            existing = {
                r.context_id for r in (await conn.execute(select(table.c.context_id))).fetchall()
            }
            for item in items:
                sid = str((item or {}).get("session_id") or "").strip()
                if not sid or sid in existing or not _is_chat_session(sid):
                    continue
                existing.add(sid)
                title = _clean_title((item or {}).get("title"))
                created_at = _utc((item or {}).get("created_at")) or datetime.now(timezone.utc)
                await conn.execute(
                    insert(table).values(context_id=sid, created_at=created_at, title=title, closed_at=None)
                )
                created.append((sid, title))
    except Exception:  # noqa: BLE001
        log.warning("[session-registry] migrate failed", exc_info=True)
        return 0
    for sid, title in created:
        _publish(CREATED, sid, title=title)
    return len(created)


# ── read ─────────────────────────────────────────────────────────────────────


async def read_session_list() -> dict[str, dict[str, Any]]:
    """Every registered session as ``{session_id: {title, created_at, closed}}`` (the registry
    is bounded by real console usage, so it is read whole). ``created_at`` is zoned ISO-8601
    UTC; ``closed`` is whether the tab is closed. Empty without a task store."""
    engine = _engine()
    if engine is None:
        return {}
    from sqlalchemy import select

    try:
        async with engine.begin() as conn:
            table = await _ensure(conn)
            rows = (
                await conn.execute(
                    select(table.c.context_id, table.c.title, table.c.created_at, table.c.closed_at)
                )
            ).fetchall()
    except Exception:  # noqa: BLE001 — discovery is opportunistic (matches the index route)
        log.warning("[session-registry] list read failed", exc_info=True)
        return {}
    return {
        r.context_id: {"title": r.title, "created_at": _iso(r.created_at), "closed": r.closed_at is not None}
        for r in rows
    }


def _reset_for_tests() -> None:
    """Drop the cached Table reflection (a fresh MetaData per test engine)."""
    _table_holder[0] = None
