"""Durable store for A2A push-notification configs (ADR 0003 / A2A spec).

Task records live in memory (lost on restart), but a client's registered
webhook config is cheap to persist and worth keeping: it survives the task's
1h terminal eviction and a process restart, so ``pushNotificationConfig/get``
and ``/list`` still answer (within a TTL) and the config is available to
re-attach if the task reappears. Mirrors protoWorkstacean's
``push-notifications.db`` (24h TTL).

Write-through: the A2A handler calls ``set``/``delete`` whenever a config is
registered or removed; ``load`` rehydrates the non-expired set on boot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_TTL_S = 24 * 60 * 60  # 24h, matching protoWorkstacean


class A2APushStore:
    def __init__(self, db_path: str, *, ttl_s: int = _DEFAULT_TTL_S) -> None:
        self.path = str(db_path)
        self._ttl_s = ttl_s
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS push_configs (
                    task_id    TEXT PRIMARY KEY,
                    config_id  TEXT,
                    url        TEXT NOT NULL,
                    token      TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.commit()
        finally:
            db.close()

    def set(self, task_id: str, *, url: str, token: str = "", config_id: str = "", now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO push_configs (task_id, config_id, url, token, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET config_id=excluded.config_id, "
                "url=excluded.url, token=excluded.token, created_at=excluded.created_at",
                (task_id, config_id or None, url, token or None, now.isoformat()),
            )
            db.commit()
        finally:
            db.close()

    def get(self, task_id: str) -> dict | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM push_configs WHERE task_id = ?", (task_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        return dict(row)

    def delete(self, task_id: str) -> None:
        db = self._connect()
        try:
            db.execute("DELETE FROM push_configs WHERE task_id = ?", (task_id,))
            db.commit()
        finally:
            db.close()

    def load(self, *, now: datetime | None = None) -> dict[str, dict]:
        """Sweep expired rows, then return the surviving configs by task_id."""
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(seconds=self._ttl_s)).isoformat()
        db = self._connect()
        try:
            db.execute("DELETE FROM push_configs WHERE created_at < ?", (cutoff,))
            db.commit()
            rows = db.execute("SELECT * FROM push_configs").fetchall()
        finally:
            db.close()
        return {r["task_id"]: dict(r) for r in rows}
