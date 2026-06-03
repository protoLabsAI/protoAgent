"""Discord turn log — persistent record of inbound + outbound Discord messages (ADR 0015).

Long-window context: when a conversation goes cold (the ConversationManager
window expires) or the process restarts, the next message would otherwise reach
the agent with no prior context. Logging every user message + assistant reply to
SQLite lets us query the last N turns for a ``(channel, user)`` pair and prepend
them, restoring continuity across timeouts and restarts.

Separate DB from the knowledge store so chat-history bulk doesn't pollute
semantic memory. Instance-scoped via ``paths.scope_leaf`` (ADR 0004), ``/sandbox``
→ ``~/.protoagent`` fallback; override with ``DISCORD_LOG_PATH``. Ported from
``-deprecated-gina``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.discord.turn_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    channel_id      TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT    NOT NULL,
    conversation_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_discord_turns_lookup
  ON discord_turns(channel_id, user_id, ts DESC);
"""


@dataclass(frozen=True)
class Turn:
    """One side of one Discord message exchange."""
    ts: int                # ms since epoch
    channel_id: str
    user_id: str
    role: str              # "user" | "assistant"
    content: str
    conversation_id: str | None = None


def _default_db_path() -> Path:
    override = os.environ.get("DISCORD_LOG_PATH")
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    from paths import scope_leaf

    leaf = Path("discord") / "turns.db"
    configured = scope_leaf(Path("/sandbox") / leaf)
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if os.access(configured.parent, os.W_OK):
            return configured
    except OSError:
        pass
    fallback = scope_leaf(Path.home() / ".protoagent" / leaf)
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


class TurnLog:
    """SQLite-backed log of Discord turns. Connection opened per-call (writes are
    infrequent; keeps the threading story simple)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return db

    def _init_db(self) -> None:
        try:
            db = self._connect()
            db.executescript(_SCHEMA)
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[discord-log] schema init failed at %s", self.db_path)

    def record_user_turn(self, channel_id, user_id, content, conversation_id=None, ts_ms=None) -> None:
        self._record("user", channel_id, user_id, content, conversation_id, ts_ms)

    def record_assistant_turn(self, channel_id, user_id, content, conversation_id=None, ts_ms=None) -> None:
        self._record("assistant", channel_id, user_id, content, conversation_id, ts_ms)

    def _record(self, role, channel_id, user_id, content, conversation_id, ts_ms) -> None:
        if not content or not content.strip():
            return
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        try:
            db = self._connect()
            db.execute(
                "INSERT INTO discord_turns (ts, channel_id, user_id, role, content, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts_ms, channel_id, user_id, role, content, conversation_id),
            )
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[discord-log] insert failed (%s, %s)", channel_id, user_id)

    def get_recent_turns(self, channel_id, user_id, *, limit: int = 8, max_age_hours: int = 24) -> list[Turn]:
        """Most-recent-N turns for a ``(channel, user)`` pair, **oldest first** (so
        they read as a transcript). Empty list when nothing matches."""
        if limit <= 0:
            return []
        since_ms = int(time.time() * 1000) - max_age_hours * 3600 * 1000
        try:
            db = self._connect()
            rows = db.execute(
                "SELECT ts, channel_id, user_id, role, content, conversation_id "
                "FROM discord_turns WHERE channel_id = ? AND user_id = ? AND ts >= ? "
                # tie-break by id (insertion order) so same-millisecond turns stay
                # deterministic — bursts often land in the same ms.
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (channel_id, user_id, since_ms, limit),
            ).fetchall()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[discord-log] query failed (%s, %s)", channel_id, user_id)
            return []

        return [
            Turn(ts=r["ts"], channel_id=r["channel_id"], user_id=r["user_id"],
                 role=r["role"], content=r["content"], conversation_id=r["conversation_id"])
            for r in reversed(rows)
        ]

    def prune_older_than(self, max_age_days: int) -> int:
        """Delete turns older than ``max_age_days``; returns rows removed. Best-effort."""
        cutoff_ms = int(time.time() * 1000) - max_age_days * 86_400 * 1000
        try:
            db = self._connect()
            cur = db.execute("DELETE FROM discord_turns WHERE ts < ?", (cutoff_ms,))
            db.commit()
            n = cur.rowcount
            db.close()
            return n
        except sqlite3.DatabaseError:
            log.exception("[discord-log] prune failed")
            return 0
