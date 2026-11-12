"""Host-free, durable scheduler execution ownership (ADR 0129).

The caller owns the SQLite transaction. The database is instance-private; UUIDs
also keep execution names distinct when stores belong to different instances.
Mappings outlive scheduling rows and never rewrite accepted dispatch snapshots.
"""

import json
import sqlite3
import uuid

from events import ACTIVITY_CONTEXT as LEGACY_ACTIVITY
LEGACY_WAIT_HOLD = "Legacy background wait needs an execution owner; not automatically fired"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS job_sessions (
        job_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        context_id TEXT,
        hold_reason TEXT,
        root_job_id TEXT NOT NULL,
        root_created_at TEXT NOT NULL,
        parent_message_id TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (job_id, created_at)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS job_enqueue_receipts (
        enqueue_key TEXT PRIMARY KEY,
        job_snapshot TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_job_sessions_context ON job_sessions(context_id)")


def resolve(
    db: sqlite3.Connection, job: dict, *, context_held=None, legacy: bool = False
) -> tuple[str | None, str | None]:
    """Assign once per generation, retaining unresolved replacement ownership.

    ``context_held(context_id) -> bool`` optionally reports continuations that
    live outside this database (an armed watch in the host's watch store).

    ``legacy`` marks a pre-existing row discovered by ``migrate``. Only those may
    be held as ambiguous Activity waits: their creating turn is long gone, so
    auto-firing them would resume a conversation nobody owns. A FRESHLY created
    job is never held — the wait tool still writes ``wait:system:activity`` /
    sessionless ``wait:activity`` ids and reports success, so those must receive
    a live ``job:`` owner and fire like any other background job.
    """
    key = (job["id"], job["created_at"])
    existing = db.execute(
        "SELECT context_id, hold_reason FROM job_sessions WHERE job_id = ? AND created_at = ?", key
    ).fetchone()
    if existing:
        return existing[0], existing[1]
    context = job.get("context_id")
    hold = None
    root = key
    parent_message = None
    if context in (None, "", LEGACY_ACTIVITY):
        if legacy and (
            job["id"] == "wait:system:activity"
            or (job["id"] == "wait:activity" and not job.get("origin_session"))
        ):
            hold = LEGACY_WAIT_HOLD
        else:
            # An unresolved predecessor occurrence captures the replacement into the
            # same owner ONLY when it is reconcilable: an accepted send whose persisted
            # task handle lets GetTask recovery settle it (ADR 0129 D6 — "retain an
            # ACCEPTED predecessor's occurrence and queue its replacement behind the
            # same owner"). A handle-less claim can never settle ("claims without
            # recoverable handles stay quarantined"), so inheriting its owner would
            # park every later generation of this job id behind it forever: it keeps
            # holding only its own, now orphaned, owner while the replacement runs
            # under a fresh one — the pre-S1 blast radius of one crashed version.
            predecessor = db.execute(
                "SELECT s.context_id, s.root_job_id, s.root_created_at FROM job_sessions s JOIN occurrences o "
                "ON s.job_id = o.job_id AND s.created_at = o.created_at "
                "WHERE s.job_id = ? AND o.state IN ('claimed', 'unknown') AND o.task_id IS NOT NULL "
                "AND s.hold_reason IS NULL LIMIT 1", (job["id"],)
            ).fetchone()
            if predecessor is None:
                predecessor = _held_predecessor(db, job["id"], context_held)
            context = predecessor[0] if predecessor else f"job:{uuid.uuid4()}"
            if predecessor:
                root = (predecessor[1], predecessor[2])
    elif context.startswith("job:"):
        owner = db.execute(
            "SELECT root_job_id, root_created_at FROM job_sessions WHERE context_id = ? LIMIT 1",
            (context,),
        ).fetchone()
        if owner:
            root = (owner[0], owner[1])
            parents = db.execute(
                "SELECT o.message_id FROM occurrences o JOIN job_sessions s "
                "ON o.job_id = s.job_id AND o.created_at = s.created_at "
                "WHERE s.context_id = ? AND o.state IN ('claimed', 'unknown')",
                (context,),
            ).fetchall()
            if len(parents) == 1:
                parent_message = parents[0][0]
    db.execute(
        "INSERT INTO job_sessions (job_id, created_at, context_id, hold_reason, "
        "root_job_id, root_created_at, parent_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*key, context, hold, *root, parent_message),
    )
    return context, hold


def _held_predecessor(db: sqlite3.Connection, job_id: str, context_held):
    """A settled predecessor generation whose owner still has outstanding work.

    While any continuation of the predecessor's owner is outstanding — a pending
    wait/watch continuation job here, or (via ``context_held``) an armed watch
    outside this database — a replacement reuses the SAME owner and queues behind
    that work with a distinct generation and claim (ADR 0129 D6). A fresh owner
    would let the replacement bypass the continuation's ownership hold. A new
    owner is minted only when nothing of the predecessor is unresolved.
    """
    seen: set[str] = set()
    for candidate in db.execute(
        "SELECT context_id, root_job_id, root_created_at FROM job_sessions "
        "WHERE job_id = ? AND hold_reason IS NULL AND context_id LIKE 'job:%' "
        "ORDER BY created_at DESC",
        (job_id,),
    ).fetchall():
        if candidate[0] in seen:
            continue
        seen.add(candidate[0])
        outstanding = db.execute(
            "SELECT 1 FROM jobs j JOIN job_sessions cs "
            "ON j.id = cs.job_id AND j.created_at = cs.created_at "
            "WHERE cs.context_id = ? AND cs.root_job_id != j.id LIMIT 1",
            (candidate[0],),
        ).fetchone()
        if outstanding or (context_held is not None and context_held(candidate[0])):
            return candidate
    return None


def migrate(db: sqlite3.Connection) -> None:
    """Version-one routing migration; all stages commit or roll back together.

    Inventory orphan snapshots first, so replacements inherit unresolved owners.
    No checkpoint/task/decision writes are needed: accepted work drains in place.
    """
    ensure_schema(db)
    for row in db.execute("SELECT job_snapshot FROM occurrences WHERE job_snapshot IS NOT NULL").fetchall():
        resolve(db, json.loads(row[0]), legacy=True)
    for row in db.execute("SELECT * FROM jobs").fetchall():
        context, hold = resolve(db, dict(row), legacy=True)
        db.execute("UPDATE jobs SET context_id = ?, execution_hold = ? WHERE id = ? "
                   "AND (context_id IS NOT ? OR execution_hold IS NOT ?)",
                   (context, hold, row["id"], context, hold))
