"""Tiny SHA256-keyed, TTL'd response cache (SQLite-backed).

A generic helper for memoising expensive deterministic calls — an LLM
classification, a relevance grade, a web fetch — keyed by the inputs. Reused
across the protoLabs fleet (protoResearcher/quinn ``guardrails.py``),
generalised here into an injectable class.

Best-effort by design: every operation swallows errors (a cache that can't
read/write must never break the caller). Falls back to a per-user path when
the configured location isn't writable (same pattern as ``audit.py``).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_TTL_S = 86_400  # 24h


class ResponseCache:
    """Key→value cache with per-entry TTL. Keys are the SHA256 of the inputs."""

    def __init__(self, path: str | Path = "/sandbox/cache/responses.db", ttl_seconds: float = _DEFAULT_TTL_S):
        self.ttl = float(ttl_seconds)
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.path = Path.home() / ".protoagent" / "cache" / self.path.name
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self._init_db()

    def _init_db(self) -> None:
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS response_cache "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            self._conn.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[cache] init failed (%s) — caching disabled", exc)
            self._conn = None

    @staticmethod
    def _key(*parts: object) -> str:
        raw = "\x1f".join(str(p).strip().lower() for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, *parts: object) -> str | None:
        """Return the cached value for *parts*, or None on miss/expiry/error."""
        if self._conn is None:
            return None
        key = self._key(*parts)
        try:
            row = self._conn.execute(
                "SELECT value, created_at FROM response_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, created_at = row
            if time.time() - created_at < self.ttl:
                return value
            self._conn.execute("DELETE FROM response_cache WHERE key = ?", (key,))
            self._conn.commit()
        except sqlite3.DatabaseError:
            return None
        return None

    def set(self, value: str, *parts: object) -> None:
        """Cache *value* under *parts*. No-op on error."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO response_cache (key, value, created_at) VALUES (?, ?, ?)",
                (self._key(*parts), str(value), time.time()),
            )
            self._conn.commit()
        except sqlite3.DatabaseError:
            pass

    def clear(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("DELETE FROM response_cache")
            self._conn.commit()
        except sqlite3.DatabaseError:
            pass
