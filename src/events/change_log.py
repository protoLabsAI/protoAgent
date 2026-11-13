"""Durable change log for server-authoritative live sync (ADR 0130, slice S1).

An append-only, per-instance SQLite table of ``(seq, kind, entity_id, ts)`` — one
row per retained bus publish, i.e. every change a client renders. Rows carry
*identities, not payloads* (ADR 0130 D2): a resuming client reacts to a replayed
row by refetching the entity's current server state, so a duplicate or stale row
is harmless.

The log is the durable half of the bus's in-memory replay ring: the ring keeps
serving live/short-reconnect catch-up with full payloads, while the log lets a
client that was away for hours — or across a server restart — resume from its
``Last-Event-ID`` cursor. Retention is 7 days / 100k rows (defaults); ``floor``
persists the highest seq ever swept so a cursor older than retention is detected
across restarts and answered with ``resync_required`` instead of a silent gap.

Progress frames (``retain=False`` publishes) stay live-only and never land here.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("protoagent.events")

#: Default retention window (ADR 0130 D2): rows older than this are swept.
RETENTION_DAYS = 7.0
#: Default retention cap: at most this many newest rows survive a sweep.
RETENTION_MAX_ROWS = 100_000
#: Appends between opportunistic sweeps (the bus appends on every retained publish,
#: so a busy instance sweeps continuously; a quiet one sweeps at boot).
SWEEP_EVERY_APPENDS = 1_000

#: Payload keys tried, in order, for the entity identity a change event names (D2:
#: events carry identities, not payloads). Producers aren't forced onto one key —
#: ``session_id``, ``job_id``, ``task_id`` etc. all predate this log.
_ENTITY_KEYS = ("entity_id", "id", "session_id", "context_id", "task_id", "job_id", "item_id", "name")


def entity_id_of(data: dict[str, Any] | None) -> str:
    """Best-effort entity identity of a bus payload (``""`` when it names none)."""
    if not isinstance(data, dict):
        return ""
    for key in _ENTITY_KEYS:
        val = data.get(key)
        if isinstance(val, (str, int)) and str(val):
            return str(val)
    return ""


class ChangeLog:
    """Append-only SQLite log of every client-rendered change (ADR 0130 D2).

    The bus appends under the same single-threaded loop step that assigned the
    seq, so rows land in seq order; :meth:`read_since` streams them back in that
    order for ``Last-Event-ID`` / ``?since=`` resume. A lock still guards the
    shared connection — no-loop contexts (sync tests, CLI) publish inline from
    arbitrary threads, and ``check_same_thread=False`` adds no locking of its own.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        retention_days: float = RETENTION_DAYS,
        max_rows: int = RETENTION_MAX_ROWS,
        sweep_every: int = SWEEP_EVERY_APPENDS,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._retention_s = retention_days * 86400.0
        self._max_rows = max_rows
        self._sweep_every = max(1, sweep_every)
        self._appends_since_sweep = 0
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # busy_timeout FIRST: the WAL pragma itself takes a lock (#2428).
        self._conn.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS change_log (
                    seq       INTEGER PRIMARY KEY,
                    kind      TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    ts        REAL NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_change_log_ts ON change_log(ts)")
            # ``floor_seq`` = the highest seq retention has ever swept, persisted
            # independently of surviving rows so a cursor older than retention is
            # still detected after a restart (and after a sweep that emptied the
            # table entirely — the bus also reseeds its seq from this high-water).
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS change_log_meta ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), floor_seq INTEGER NOT NULL)"
            )
            self._conn.execute("INSERT OR IGNORE INTO change_log_meta VALUES (1, 0)")

    # ── writes ─────────────────────────────────────────────────────────────────

    def append(self, seq: int, kind: str, entity_id: str = "", ts: float | None = None) -> None:
        """One retained publish → one row. The caller (the bus) owns seq ordering."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO change_log (seq, kind, entity_id, ts) VALUES (?, ?, ?, ?)",
                (int(seq), str(kind), str(entity_id or ""), float(time.time() if ts is None else ts)),
            )
            self._appends_since_sweep += 1
        if self._appends_since_sweep >= self._sweep_every:
            self.sweep()

    def sweep(self, *, now: float | None = None) -> int:
        """Apply retention: drop rows past the age window or beyond the row cap.

        Raises ``floor_seq`` to the highest seq dropped (monotonic), so ``covers``
        keeps answering correctly even if clock skew interleaved an old-``ts`` row
        between retained seqs. Returns the number of rows swept."""
        cutoff = (time.time() if now is None else now) - self._retention_s
        with self._lock, self._conn:
            self._appends_since_sweep = 0
            # The (max_rows+1)-th newest row, if any: it and everything older is over the cap.
            cap_row = self._conn.execute(
                "SELECT seq FROM change_log ORDER BY seq DESC LIMIT 1 OFFSET ?", (self._max_rows,)
            ).fetchone()
            cap_seq = cap_row["seq"] if cap_row is not None else -1
            doomed = self._conn.execute(
                "SELECT MAX(seq) AS top, COUNT(*) AS n FROM change_log WHERE ts < ? OR seq <= ?",
                (cutoff, cap_seq),
            ).fetchone()
            if not doomed["n"]:
                return 0
            self._conn.execute("DELETE FROM change_log WHERE ts < ? OR seq <= ?", (cutoff, cap_seq))
            self._conn.execute(
                "UPDATE change_log_meta SET floor_seq = MAX(floor_seq, ?) WHERE singleton = 1",
                (doomed["top"],),
            )
            return doomed["n"]

    # ── reads ──────────────────────────────────────────────────────────────────

    def read_since(self, since: int) -> list[dict[str, Any]]:
        """Retained rows with ``seq > since``, in seq order (the resume batch)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, entity_id, ts FROM change_log WHERE seq > ? ORDER BY seq ASC",
                (int(since),),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_seq(self) -> int:
        """High-water seq this log has seen — the bus reseeds from it at attach so a
        restart never re-mints seqs that client cursors already point past. Falls
        back to the sweep floor when retention emptied the table."""
        with self._lock:
            top = self._conn.execute("SELECT MAX(seq) AS top FROM change_log").fetchone()["top"]
            floor = self._conn.execute("SELECT floor_seq FROM change_log_meta WHERE singleton = 1").fetchone()[0]
        return max(int(top or 0), int(floor or 0))

    def floor_seq(self) -> int:
        """Highest seq retention has swept (0 = nothing swept yet)."""
        with self._lock:
            row = self._conn.execute("SELECT floor_seq FROM change_log_meta WHERE singleton = 1").fetchone()
        return int(row[0] or 0)

    def covers(self, since: int) -> bool:
        """Can a cursor at ``since`` be fully replayed from retained rows? False means
        the client's gap extends past retention → it must get ``resync_required``."""
        return int(since) >= self.floor_seq()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — closing twice (tests, shutdown races) is fine
                pass
