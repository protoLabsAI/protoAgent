"""Plugin metric timeseries store — small named numeric series (#1632).

One row per ``(series, ts, value)`` sample: the persistence behind
``graph.sdk.record_metric`` / ``metric_history`` / ``metric_last``. Series keys are
namespaced ``<plugin_id>:<name>`` by the SDK (never here), so one instance-dir file
(``metrics.db``) holds every plugin's series without collisions. Timestamps are Unix
epoch seconds (REAL) — the natural unit for verifier math (drawdown windows, flatline
gaps) and sparkline rendering.

This is deliberately NOT the per-turn :class:`~observability.telemetry_store.TelemetryStore`:
that store is cost/latency rollups behind the ``telemetry.enabled`` operator toggle and a
configurable path, while metric series are *functional* plugin state — a
history-dependent watch verifier (ADR 0067) goes blind without them — so they live in
their own always-on store, never gated by an observability preference.

Concurrency: connection-per-call + WAL + busy_timeout (the TelemetryStore /
BackgroundStore pattern). Plugin engines record from worker threads and background
loops; the FTS-index race (#1500) is why a single shared connection is never held
across threads here.

Retention is capped per series (defaults: 90 days AND 10k points) and trimmed inside
the same write transaction — a metric series is a sparkline/verifier substrate, not an
archive, so the file stays small without a maintenance loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Hardcoded retention defaults (per the issue: ~90d / 10k points per series). Not a
# config knob yet — hosts that want different caps pass them to the constructor.
RETENTION_DAYS = 90
MAX_POINTS = 10_000


class MetricsStore:
    def __init__(
        self,
        db_path: str,
        *,
        retention_days: int = RETENTION_DAYS,
        max_points: int = MAX_POINTS,
    ) -> None:
        self.path = str(db_path)
        # <= 0 disables the corresponding cap (age / point-count).
        self.retention_days = int(retention_days)
        self.max_points = int(max_points)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    series TEXT NOT NULL,
                    ts     REAL NOT NULL,
                    value  REAL NOT NULL
                )
                """
            )
            # (series, ts) drives every query: history range scans, last(), and the
            # per-series retention trims below.
            db.execute("CREATE INDEX IF NOT EXISTS ix_metrics_series_ts ON metrics(series, ts)")
            db.commit()
        finally:
            db.close()

    def record(self, series: str, value: float, ts: float | None = None) -> None:
        """Append one sample to ``series`` (``ts`` defaults to now) and trim the series
        to the retention caps in the same transaction. Duplicate timestamps are allowed
        (two samples in the same second are both kept, insert order preserved)."""
        now = time.time()
        ts = now if ts is None else float(ts)
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO metrics (series, ts, value) VALUES (?, ?, ?)",
                (series, ts, float(value)),
            )
            if self.retention_days > 0:
                db.execute(
                    "DELETE FROM metrics WHERE series = ? AND ts < ?",
                    (series, now - self.retention_days * 86400.0),
                )
            if self.max_points > 0:
                # Keep the newest max_points rows (rowid breaks ts ties so exactly
                # max_points survive); LIMIT -1 OFFSET n = "everything past the newest n".
                db.execute(
                    "DELETE FROM metrics WHERE rowid IN ("
                    " SELECT rowid FROM metrics WHERE series = ?"
                    " ORDER BY ts DESC, rowid DESC LIMIT -1 OFFSET ?)",
                    (series, self.max_points),
                )
            db.commit()
        finally:
            db.close()

    def history(self, series: str, *, since: float | None = None, limit: int = 500) -> list[tuple[float, float]]:
        """The newest ``limit`` samples of ``series`` (at/after ``since`` when given),
        returned **oldest→newest** as ``(ts, value)`` tuples — chronological order is
        what verifier math and sparklines consume."""
        where, params = "series = ?", [series]
        if since is not None:
            where += " AND ts >= ?"
            params.append(float(since))
        params.append(max(1, int(limit)))
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT ts, value FROM metrics WHERE {where} ORDER BY ts DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            db.close()
        rows.reverse()  # window is the NEWEST `limit`; presentation is chronological
        return [(row["ts"], row["value"]) for row in rows]

    def last(self, series: str) -> tuple[float, float] | None:
        """The most recent ``(ts, value)`` of ``series``, or ``None`` when it has no
        samples (never recorded, or fully aged out)."""
        db = self._connect()
        try:
            row = db.execute(
                "SELECT ts, value FROM metrics WHERE series = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
                (series,),
            ).fetchone()
            return (row["ts"], row["value"]) if row else None
        finally:
            db.close()
