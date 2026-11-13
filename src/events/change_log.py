"""Durable change log for server-authoritative live sync (ADR 0130, slice S1).

An append-only, per-instance SQLite table of ``(seq, kind, entity_id, ts)`` — one
row per retained bus publish, i.e. every change a client renders. The D2 *client*
contract is identities, not payloads: a resuming client reacts to a replayed row
by refetching the entity's current server state, so a duplicate or stale row is
harmless. But today's consumers predate that contract and branch on payload
fields (the deck's feed reads ``turn.usage``'s ``cost_usd``/``model``/``state``
and ``turn.finished``'s ``ok``; the console's topic listeners likewise), so each
row ALSO stores the publish payload verbatim (JSON, up to
:data:`MAX_PAYLOAD_BYTES`) — a replay past the live ring is frame-identical to
the original publish, never an identity-only husk that zeroes costs, renders a
failure as success, or unparks a session still waiting on the operator. A payload
that cannot be stored (unencodable — live SSE skips those frames too — or over
the ceiling) degrades that one row to identity-only: ``entity_id`` plus
``entity_key``, the payload key the identity was named under (``session_id``,
``context_id``, ``id``, …), so even then consumers keep keying correctly instead
of minting ``""``-keyed state.

The log is the durable half of the bus's in-memory replay ring: the ring keeps
serving live/short-reconnect catch-up with full payloads, while the log lets a
client that was away for hours — or across a server restart — resume from its
``Last-Event-ID`` cursor. Retention is 7 days / 100k rows (defaults); ``floor``
persists the highest seq ever swept so a cursor older than retention is detected
across restarts and answered with ``resync_required`` instead of a silent gap.

Progress frames (``retain=False`` publishes) stay live-only and never land here
as rows — but their seqs DO reach clients (the payload ``seq`` and the SSE
``id:``), so the log also tracks the bus counter's high-water across them
(:meth:`ChangeLog.note_seq`, throttled to one meta write per
:data:`LIVE_SEQ_PERSIST_EVERY` seqs). A restarted bus reseeds past that mark plus
one throttle window of slack (:meth:`ChangeLog.resume_seq`), so no seq a client's
cursor can hold — retained or not — is ever re-minted for a new event.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

#: Default retention window (ADR 0130 D2): rows older than this are swept.
RETENTION_DAYS = 7.0
#: Default retention cap: at most this many newest rows survive a sweep.
RETENTION_MAX_ROWS = 100_000
#: Appends between opportunistic sweeps (the bus appends on every retained publish,
#: so a busy instance sweeps continuously; a quiet one sweeps at boot).
SWEEP_EVERY_APPENDS = 1_000
#: Unretained (progress-frame) seqs between live high-water meta writes — and the
#: slack :meth:`ChangeLog.resume_seq` adds back to cover the unpersisted tail.
LIVE_SEQ_PERSIST_EVERY = 64
#: Ceiling on a stored payload's JSON size — the event-payload cap the publish route
#: already enforces (64 KiB, operator_api). A payload over it (or one json can't
#: encode) stores as NULL and that row replays identity-only instead of bloating a
#: 100k-row log.
MAX_PAYLOAD_BYTES = 64 * 1024

#: Payload keys tried, in order, for the entity identity a change event names (D2:
#: events carry identities, not payloads). Producers aren't forced onto one key —
#: ``session_id``, ``job_id``, ``task_id`` etc. all predate this log.
_ENTITY_KEYS = ("entity_id", "id", "session_id", "context_id", "task_id", "job_id", "item_id", "name")


def entity_ref_of(data: dict[str, Any] | None) -> tuple[str, str]:
    """Best-effort ``(key, id)`` of the entity a bus payload names (``("", "")`` when
    it names none): the identity value plus the payload key it arrived under, so a
    log replay can re-frame the row the way the original publish named its entity."""
    if not isinstance(data, dict):
        return "", ""
    for key in _ENTITY_KEYS:
        val = data.get(key)
        if isinstance(val, (str, int)) and str(val):
            return key, str(val)
    return "", ""


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
        live_persist_every: int = LIVE_SEQ_PERSIST_EVERY,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._retention_s = retention_days * 86400.0
        self._max_rows = max_rows
        self._sweep_every = max(1, sweep_every)
        self._live_every = max(1, live_persist_every)
        self._appends_since_sweep = 0
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # busy_timeout FIRST: the WAL pragma itself takes a lock (#2428).
        self._conn.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # Highest seq known durably recorded (a row, or the live high-water meta):
        # ``note_seq`` throttles against it, so a progress-frame storm costs at most
        # one meta write per ``live_persist_every`` seqs.
        self._persisted_seq = self.last_seq()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            # ``data`` = the publish payload as JSON; NULL = identity-only (the payload
            # was unencodable or over MAX_PAYLOAD_BYTES).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS change_log (
                    seq        INTEGER PRIMARY KEY,
                    kind       TEXT NOT NULL,
                    entity_id  TEXT NOT NULL DEFAULT '',
                    entity_key TEXT NOT NULL DEFAULT '',
                    data       TEXT,
                    ts         REAL NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_change_log_ts ON change_log(ts)")
            # ``floor_seq`` = the highest seq retention has ever swept, persisted
            # independently of surviving rows so a cursor older than retention is
            # still detected after a restart (and after a sweep that emptied the
            # table entirely). ``live_seq`` = the throttled high-water of UNRETAINED
            # seqs (progress frames leave no row but their seqs reach clients) — the
            # bus reseeds past ``max(rows, floor, live)`` at attach.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS change_log_meta ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "floor_seq INTEGER NOT NULL, live_seq INTEGER NOT NULL DEFAULT 0)"
            )
            self._conn.execute("INSERT OR IGNORE INTO change_log_meta (singleton, floor_seq, live_seq) VALUES (1, 0, 0)")

    # ── writes ─────────────────────────────────────────────────────────────────

    def append(
        self,
        seq: int,
        kind: str,
        entity_id: str = "",
        ts: float | None = None,
        *,
        entity_key: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """One retained publish → one row, payload and all. The caller (the bus) owns
        seq ordering. A payload json can't encode — live SSE skips those frames too —
        or one over :data:`MAX_PAYLOAD_BYTES` stores as NULL (identity-only replay)."""
        blob: str | None = None
        if data is not None:
            try:
                blob = json.dumps(data)
            except (TypeError, ValueError):
                blob = None
            else:
                if len(blob) > MAX_PAYLOAD_BYTES:
                    blob = None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO change_log (seq, kind, entity_id, entity_key, data, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(seq),
                    str(kind),
                    str(entity_id or ""),
                    str(entity_key or ""),
                    blob,
                    float(time.time() if ts is None else ts),
                ),
            )
            self._appends_since_sweep += 1
            self._persisted_seq = max(self._persisted_seq, int(seq))
        if self._appends_since_sweep >= self._sweep_every:
            self.sweep()

    def note_seq(self, seq: int) -> None:
        """Record the bus counter passing ``seq`` for a publish that leaves no row (a
        ``retain=False`` progress frame). Clients still see that seq (the payload and
        the SSE ``id:``), so it must survive a restart or the new process would re-mint
        seqs a resume cursor already points past. Throttled to one meta write per
        ``live_persist_every`` seqs — plus the very first write on a log that has never
        recorded anything, so :meth:`resume_seq` knows to add slack at all —
        :meth:`resume_seq` covers the unpersisted tail."""
        seq = int(seq)
        if self._persisted_seq and seq - self._persisted_seq < self._live_every:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE change_log_meta SET live_seq = MAX(live_seq, ?) WHERE singleton = 1", (seq,)
            )
            self._persisted_seq = max(self._persisted_seq, seq)

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
        """Retained rows with ``seq > since``, in seq order (the resume batch). The
        stored payload comes back decoded under ``data`` — ``None`` means the row is
        identity-only (payload unstorable, or the column corrupted on disk)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, entity_id, entity_key, data, ts FROM change_log WHERE seq > ? ORDER BY seq ASC",
                (int(since),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d["data"] is not None:
                try:
                    decoded = json.loads(d["data"])
                except ValueError:
                    decoded = None
                d["data"] = decoded if isinstance(decoded, dict) else None
            out.append(d)
        return out

    def last_seq(self) -> int:
        """High-water seq durably recorded here — the max of the surviving rows, the
        sweep floor (retention may have emptied the table), and the throttled live
        mark (``note_seq``) unretained publishes leave."""
        with self._lock:
            top = self._conn.execute("SELECT MAX(seq) AS top FROM change_log").fetchone()["top"]
            meta = self._conn.execute(
                "SELECT floor_seq, live_seq FROM change_log_meta WHERE singleton = 1"
            ).fetchone()
        return max(int(top or 0), int(meta["floor_seq"] or 0), int(meta["live_seq"] or 0))

    def resume_seq(self) -> int:
        """The seq a new bus must reseed to at attach (ADR 0130 S1): everything durably
        recorded, plus one live-persist window of slack for the unretained seqs stamped
        on SSE frames after the last ``note_seq`` write — so a restart never re-mints a
        seq any client's ``Last-Event-ID`` can hold, and a later resume never silently
        skips the re-minted range. A log that never recorded anything (``note_seq``
        writes the very first live seq eagerly) has issued no cursors: no slack."""
        last = self.last_seq()
        return last + self._live_every if last else 0

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
