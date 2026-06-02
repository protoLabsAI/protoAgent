"""LocalScheduler — bundled sqlite + asyncio backend.

The default scheduler when no protoWorkstacean install is configured.
Every protoAgent instance gets a private ``jobs.db`` namespaced by
``AGENT_NAME`` so spinning up gina-personal alongside gina-work
doesn't cross-fire prompts.

Architecture:

- One ``jobs`` table — ``id``, ``prompt``, ``schedule``, ``next_fire``,
  ``agent_name``, ``last_fire``, ``enabled``, ``created_at``.
- Polling coroutine runs on FastAPI's startup hook (``server.py``)
  and ticks once per ``_POLL_INTERVAL_S`` (1s default). Cheap because
  sqlite reads with an indexed ``next_fire`` filter cost microseconds.
- Firing = HTTP POST to the running agent's own ``/a2a`` endpoint as
  a ``message/send``. Going through HTTP rather than calling into the
  graph directly gets us free parity with real callers — same audit
  log, same cost-v1 capture, same auth path.
- One-shot ISO schedules are deleted after firing. Cron schedules
  reschedule via croniter.
- On startup: any job whose ``next_fire`` is in the past but within a
  24h window fires immediately (BFCL-style "missed fires" recovery,
  matching Workstacean's behaviour). Older missed fires are
  rescheduled forward without firing — better than waking the agent
  to a flood of stale prompts after a long downtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import croniter

from events import ACTIVITY_CONTEXT
from scheduler.interface import Job, is_cron, parse_iso_to_utc

log = logging.getLogger(__name__)

DEFAULT_DB_DIR = "/sandbox/scheduler"
_POLL_INTERVAL_S = 1.0
_MISSED_FIRE_WINDOW_S = 24 * 60 * 60  # 24h — matches Workstacean


# Owner-lock interlock (ADR 0004): one live instance owns a given jobs.db. Two
# instances sharing it would both poll and race to claim due jobs (a fired job
# vanishes from the other's view). The in-process set catches same-process /
# test collisions; fcntl.flock catches separate processes on a shared filesystem.
_LOCKED_PATHS: set[str] = set()


def _acquire_jobs_lock(path: Path):
    """Try to take the exclusive owner-lock for ``path``'s jobs.db.

    Returns the held lock file object on success, or ``None`` if another live
    instance already owns it (caller should log + skip starting the scheduler).
    """
    key = str(path)
    if key in _LOCKED_PATHS:
        return None
    try:
        import fcntl

        fd = open(key + ".lock", "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return None
    except ImportError:  # pragma: no cover - non-POSIX; fall back to in-proc guard
        fd = None
    _LOCKED_PATHS.add(key)
    return fd if fd is not None else _InProcLock(key)


class _InProcLock:
    """Marker returned when fcntl is unavailable — release just drops the path."""

    def __init__(self, key: str):
        self._key = key

    def close(self):
        pass


def _release_jobs_lock(path: Path, fd) -> None:
    _LOCKED_PATHS.discard(str(path))
    try:
        if fd is not None:
            fd.close()
    except Exception:  # noqa: BLE001
        pass


def _resolve_db_path(db_dir: str | Path | None, agent_name: str) -> Path:
    """Pick a writable jobs.db path namespaced by agent name.

    ``agent_name`` is sanitized to a single path segment before being
    appended — operators set it via env or YAML, but defence in depth
    against a value like ``../etc/passwd`` or ``/tmp/elsewhere`` is
    cheap and prevents an exotic typo from putting a sqlite file
    outside the configured scheduler dir.
    """
    from paths import scope_leaf  # ADR 0004 — per-instance scoping (no-op when unset)

    safe_name = _safe_segment(agent_name)
    raw = os.environ.get("SCHEDULER_DB_DIR") or db_dir or DEFAULT_DB_DIR
    base = scope_leaf(Path(str(raw)).expanduser() / safe_name)
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-probe"
        probe.touch()
        probe.unlink()
        return base / "jobs.db"
    except OSError:
        fallback = scope_leaf(Path.home() / ".protoagent" / "scheduler" / safe_name)
        fallback.mkdir(parents=True, exist_ok=True)
        log.info("[scheduler] %s not writable; using %s instead", base, fallback)
        return fallback / "jobs.db"


def _safe_segment(name: str) -> str:
    """Reduce ``name`` to a single safe path segment.

    Replaces path separators, ``..``, and absolute-path prefixes with
    underscores; falls back to ``"default"`` when nothing usable
    remains. Preserves the common slug shape (``gina-personal``,
    ``ginavision``) without surprises.
    """
    if not name:
        return "default"
    cleaned = name.replace("/", "_").replace("\\", "_").replace("..", "_")
    cleaned = cleaned.lstrip(".").strip()
    return cleaned or "default"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compute_next_fire(schedule: str, *, after: datetime | None = None) -> str:
    """Resolve a schedule string to the next ISO timestamp it fires.

    ``after`` controls when "next" starts — current time by default;
    pass an explicit reference when rescheduling a cron job after a
    fire so successive fires don't drift.
    """
    after = after or datetime.now(UTC)
    if is_cron(schedule):
        return croniter(schedule, after).get_next(datetime).astimezone(UTC).isoformat()
    return parse_iso_to_utc(schedule).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    prompt      TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    next_fire   TEXT NOT NULL,
    last_fire   TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_next_fire   ON jobs(next_fire);
CREATE INDEX IF NOT EXISTS idx_jobs_agent_name  ON jobs(agent_name);
"""


class LocalScheduler:
    """Sqlite-backed scheduler with an asyncio polling loop.

    Construct once at server startup, ``await scheduler.start()`` to
    spawn the polling task, ``await scheduler.stop()`` on shutdown.
    The agent-facing tools call ``add_job`` / ``cancel_job`` /
    ``list_jobs`` synchronously.
    """

    name = "local"

    def __init__(
        self,
        agent_name: str,
        *,
        invoke_url: str,
        api_key: str | None = None,
        bearer_token: str | None = None,
        db_dir: str | Path | None = None,
    ):
        self.agent_name = agent_name
        self._invoke_url = invoke_url.rstrip("/")
        self._api_key = api_key or ""
        self._bearer = bearer_token or ""
        self.path = _resolve_db_path(db_dir, agent_name)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._lock_fd = None  # owner-lock fd (ADR 0004) held while polling
        self._init_db()

    # ── DB plumbing ─────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            log.debug("[scheduler] WAL skipped: %s", exc)
        return db

    def _init_db(self) -> None:
        try:
            db = self._connect()
            db.executescript(_SCHEMA)
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[scheduler] schema init failed at %s", self.path)

    # ── public API (matches SchedulerBackend) ───────────────────────────────

    def add_job(self, prompt: str, schedule: str, *, job_id: str | None = None) -> Job:
        if not prompt or not prompt.strip():
            raise ValueError("scheduler: prompt is required")
        next_fire = _compute_next_fire(schedule)  # raises ValueError for malformed input

        job = Job(
            id=job_id or self._generate_id(),
            prompt=prompt,
            schedule=schedule,
            agent_name=self.agent_name,
            next_fire=next_fire,
        )
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO jobs (id, prompt, schedule, agent_name, next_fire, "
                "last_fire, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.prompt, job.schedule, job.agent_name,
                 job.next_fire, job.last_fire, int(job.enabled), job.created_at),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"job id {job.id!r} already exists") from exc
        finally:
            db.close()
        return job

    def cancel_job(self, job_id: str) -> bool:
        db = self._connect()
        try:
            cur = db.execute(
                "DELETE FROM jobs WHERE id = ? AND agent_name = ?",
                (job_id, self.agent_name),
            )
            db.commit()
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            log.warning("[scheduler] cancel_job failed: %s", exc)
            return False
        finally:
            db.close()

    def list_jobs(self) -> list[Job]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM jobs WHERE agent_name = ? ORDER BY next_fire ASC",
                (self.agent_name,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[scheduler] list_jobs failed: %s", exc)
            return []
        finally:
            db.close()
        return [_row_to_job(r) for r in rows]

    async def start(self) -> None:
        if self._task is not None:
            return
        # Owner-lock interlock (ADR 0004): refuse to poll a jobs.db another live
        # instance owns, rather than silently racing it. Loud error + skip the
        # scheduler (the rest of the agent still serves normally).
        self._lock_fd = _acquire_jobs_lock(self.path)
        if self._lock_fd is None:
            log.error(
                "[scheduler] jobs.db at %s is already owned by another live instance — "
                "not starting the scheduler. Run each instance with a distinct "
                "PROTOAGENT_INSTANCE (or agent name) so they don't share a jobs.db.",
                self.path,
            )
            return
        self._stopping = False
        self._recover_missed_fires()
        self._task = asyncio.create_task(self._poll_loop(), name="scheduler.local.poll")
        log.info(
            "[scheduler] local backend started: agent=%s db=%s",
            self.agent_name, self.path,
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected — we just cancelled it.
                pass
            except Exception:  # noqa: BLE001
                # Anything else means the polling loop crashed during
                # shutdown. Log with traceback so we can debug; don't
                # re-raise (caller is in shutdown path, raising would
                # mask the original shutdown trigger).
                log.exception("[scheduler] polling task raised during stop")
            self._task = None
            log.info("[scheduler] local backend stopped")
        # Release the owner-lock (ADR 0004) so another instance can take over.
        if self._lock_fd is not None:
            _release_jobs_lock(self.path, self._lock_fd)
            self._lock_fd = None

    # ── polling + firing ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("[scheduler] poll tick failed")
            try:
                await asyncio.sleep(_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        due = self._claim_due_jobs(now)
        for job in due:
            # Reschedule (or delete) only when delivery actually
            # succeeded. A transient HTTP failure leaves the row in
            # place so the next tick retries; a one-shot stays alive
            # until it lands rather than vanishing on the first
            # network blip.
            if await self._fire(job):
                self._reschedule_or_delete(job, fired_at=now)
            else:
                log.warning(
                    "[scheduler] fire failed for job %s; leaving in place for retry",
                    job.id,
                )

    def _claim_due_jobs(self, now: datetime) -> list[Job]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM jobs WHERE agent_name = ? AND enabled = 1 "
                "AND next_fire <= ? ORDER BY next_fire ASC",
                (self.agent_name, now.isoformat()),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[scheduler] _claim_due_jobs failed: %s", exc)
            return []
        finally:
            db.close()
        return [_row_to_job(r) for r in rows]

    def _reschedule_or_delete(self, job: Job, *, fired_at: datetime) -> None:
        """Cron jobs roll forward; one-shot jobs are deleted."""
        db = self._connect()
        try:
            if is_cron(job.schedule):
                next_iso = _compute_next_fire(job.schedule, after=fired_at)
                db.execute(
                    "UPDATE jobs SET next_fire = ?, last_fire = ? WHERE id = ?",
                    (next_iso, fired_at.isoformat(), job.id),
                )
            else:
                db.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
            db.commit()
        except sqlite3.DatabaseError:
            log.exception("[scheduler] reschedule failed for job %s", job.id)
        finally:
            db.close()

    def _recover_missed_fires(self) -> None:
        """Roll past-due jobs forward on startup.

        - Missed fires within the last 24h fire immediately on the next
          tick (we leave their ``next_fire`` in the past so the polling
          loop picks them up naturally).
        - Older missed fires are rescheduled forward without firing —
          firing a flood of stale prompts after a long downtime is worse
          than dropping them.
        """
        cutoff_recent = datetime.now(UTC) - timedelta(seconds=_MISSED_FIRE_WINDOW_S)
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM jobs WHERE agent_name = ? AND enabled = 1 "
                "AND next_fire <= ?",
                (self.agent_name, cutoff_recent.isoformat()),
            ).fetchall()
            for row in rows:
                job = _row_to_job(row)
                if is_cron(job.schedule):
                    next_iso = _compute_next_fire(job.schedule)
                    db.execute(
                        "UPDATE jobs SET next_fire = ? WHERE id = ?",
                        (next_iso, job.id),
                    )
                    log.info(
                        "[scheduler] dropped stale fire for job %s; next at %s",
                        job.id, next_iso,
                    )
                else:
                    db.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
                    log.info("[scheduler] dropped stale one-shot job %s", job.id)
            db.commit()
        except sqlite3.DatabaseError:
            log.exception("[scheduler] missed-fire recovery failed")
        finally:
            db.close()

    async def _fire(self, job: Job) -> bool:
        """Deliver a job by POSTing to the agent's own A2A endpoint.

        Returns ``True`` on a 2xx response, ``False`` on any HTTP
        error or network exception. Callers use the return value to
        decide whether to advance the schedule (success) or leave
        the row in place for the next tick to retry (failure).
        """
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        message_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "message/send",
            "params": {
                # Route into the durable Activity thread (ADR 0003) so the
                # fired turn lands somewhere visible/continuable instead of a
                # throwaway context. Without this, the agent mints a fresh
                # context per fire and the response surfaces nowhere.
                "contextId": ACTIVITY_CONTEXT,
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": job.prompt}],
                    "messageId": message_id,
                },
                # Scheduler bookkeeping for this fire, sent as params.metadata
                # per the A2A message/send shape (origin + job id). These keys
                # are informational — the handler does not require them.
                "metadata": {
                    "scheduler_job_id": job.id,
                    "scheduler_kind": "local",
                    "origin": "scheduler",
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{self._invoke_url}/a2a", headers=headers, json=body)
            if r.status_code >= 400:
                log.error(
                    "[scheduler] fire failed for job %s: HTTP %d %s",
                    job.id, r.status_code, r.text[:200],
                )
                return False
            log.info("[scheduler] fired job %s", job.id)
            return True
        except Exception:  # noqa: BLE001
            log.exception("[scheduler] fire exception for job %s", job.id)
            return False

    def _generate_id(self) -> str:
        # Agent-name prefix keeps cross-agent IDs distinct in shared
        # observability surfaces (audit log, dashboards) even though
        # the DB row is already namespaced by agent_name.
        return f"{self.agent_name}-{uuid.uuid4().hex[:12]}"


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row["id"],
        prompt=row["prompt"],
        schedule=row["schedule"],
        agent_name=row["agent_name"],
        next_fire=row["next_fire"],
        last_fire=row["last_fire"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )
