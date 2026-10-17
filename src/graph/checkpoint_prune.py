"""Periodic pruning for the SQLite conversation checkpointer.

LangGraph writes ~3 checkpoint rows per turn (one per super-step), all retained
per ``thread_id`` — so the DB grows unbounded as chats accumulate. We don't use
time-travel/replay, only resume-from-latest, so older checkpoints are dead
weight. This trims the DB two ways:

- **Per-thread cap** — keep only the latest ``keep_per_thread`` checkpoints per
  ``(thread_id, checkpoint_ns)`` (resume needs only the most recent). Ordered by
  ``checkpoint_id``, which LangGraph generates as a time-sortable UUIDv6.
- **Age TTL** — delete whole threads whose newest checkpoint is older than
  ``max_age_days`` (idle conversations). The age comes from the UUIDv6
  timestamp, so no extra bookkeeping table is needed.

All pure SQL on a short-lived connection (the saver runs WAL mode, so this
coexists with live writes); failures are caught by the caller and never block.

After row deletions, ``reclaim()`` truncates the WAL and incrementally frees
pages back to the OS so the on-disk file shrinks rather than holding freed space
forever (a legacy ``auto_vacuum=NONE`` DB gets a full VACUUM only on opt-in).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid

_log = logging.getLogger("protoagent.checkpoint_prune")

# Thread-id prefix of background (A2A) runs, which get ``background_keep``.
_BACKGROUND_PREFIX = "a2a:background:"

# Sub-thread suffixes a chat owns, removed with it by ``delete_thread(cascade=True)``:
# goal-mode iterations (``server/chat.py``) and ephemeral asides (``graph/aside_op.py``).
_GOAL_ITER_SUFFIX = ":goal-iter-"
_ASIDE_SUFFIX = "::aside-"

# 100ns intervals between the UUID (Gregorian, 1582-10-15) and Unix epochs.
_GREGORIAN_OFFSET = 0x01B21DD213814000


def uuidv6_unix_seconds(checkpoint_id: str) -> float | None:
    """Unix seconds encoded in a UUIDv6, or None if it isn't a parseable v6."""
    try:
        u = uuid.UUID(checkpoint_id)
    except (ValueError, AttributeError):
        return None
    if u.version != 6:
        return None
    i = u.int
    time_high = (i >> 96) & 0xFFFFFFFF
    time_mid = (i >> 80) & 0xFFFF
    time_low = (i >> 64) & 0x0FFF
    ticks = (time_high << 28) | (time_mid << 12) | time_low  # 100ns since 1582
    return (ticks - _GREGORIAN_OFFSET) / 1e7


def find_aged_threads(db_path: str, max_age_seconds: float, *, now: float | None = None) -> list[str]:
    """Thread ids whose newest checkpoint is older than the cutoff (datable via
    UUIDv6). Used to harvest a thread to knowledge *before* deleting it."""
    import time as _time

    cutoff = (now if now is not None else _time.time()) - max_age_seconds
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        newest = _newest_stamps(conn)
    finally:
        conn.close()
    return [tid for tid, ts in newest.items() if ts is not None and ts < cutoff]


def _newest_stamps(conn: sqlite3.Connection) -> dict[str, float | None]:
    """Every thread id → its newest datable (UUIDv6) checkpoint time, ``None`` when none
    of its ids is datable. ONE scan of the table (#3973 — was a query per thread). The
    max is taken over parsed stamps, not ``MAX(checkpoint_id)``, so a non-v6 id that
    sorts high can't hide a thread's real age."""
    newest: dict[str, float | None] = {}
    for thread_id, checkpoint_id in conn.execute("SELECT thread_id, checkpoint_id FROM checkpoints"):
        ts = uuidv6_unix_seconds(checkpoint_id)
        prev = newest.get(thread_id)
        newest[thread_id] = ts if prev is None else (prev if ts is None else max(prev, ts))
    return newest


def thread_has_checkpoints_before(db_path: str, thread_id: str, before: float | None) -> bool:
    """Does ``thread_id`` hold a checkpoint written before ``before`` (unix seconds;
    ``None`` = any)? A checkpoint whose id isn't a datable UUIDv6 counts as before —
    the conservative answer for the forget sweep, whose "yes" restores rows."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchall()
    finally:
        conn.close()
    for (cid,) in rows:
        if before is None:
            return True
        ts = uuidv6_unix_seconds(cid)
        if ts is None or ts < before:
            return True
    return False


def _is_root(metadata_json: str | None) -> bool:
    """A checkpoint the thread's OWN graph wrote. ``task()`` subagents inherit the
    parent's checkpointer and write into the same ``(thread_id, checkpoint_ns)``, tagged
    with a non-empty ``metadata.parents``; those rows are never what a resume needs."""
    import json

    if not metadata_json:
        return True
    try:
        return not (json.loads(metadata_json) or {}).get("parents")
    except (ValueError, TypeError, AttributeError):
        return True


def trim_group(
    conn: sqlite3.Connection, thread_id: str, checkpoint_ns: str, keep: int, *, written_id: str | None = None
) -> int:
    """Keep the newest ``keep`` checkpoints of one (thread, ns); delete older rows and
    their writes. Runs on the caller's connection and transaction; returns checkpoints
    deleted.

    * In the thread's own namespace (``''``) only ROOT rows count toward ``keep``, and
      every row newer than the oldest kept root survives: rows tagged with
      ``metadata.parents`` (seen in ``''`` on live fleet DBs) are never the parent's
      resumable state and must not push it out.
    * A sub-namespace (``tools:<task_id>``, where a ``task()`` subagent's graph
      checkpoints; ``…|1`` per ``task_batch`` child) is its own graph: plain newest-N,
      every row counts — its rows ALL carry ``parents``, so root-counting would never
      trim them (#4306 review M1).
    * ``written_id`` (the write path): if the row just written is not the newest by id —
      the clock went backwards across a restart, so LangGraph's time-ordered ids sort it
      below older rows — skip the trim rather than cut the new turn (#4306 review m3).

    Streams newest-first and stops at the cutoff, so it reads only a handful of rows'
    metadata however long the thread already is (``metadata`` sits after the checkpoint
    blob in the row, so reading it walks the blob's overflow pages)."""
    keep = max(1, keep)
    root_aware = checkpoint_ns == ""
    roots, cutoff, first = 0, None, True
    for cid, md in conn.execute(
        "SELECT checkpoint_id, CAST(metadata AS TEXT) FROM checkpoints"
        " WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC",
        (thread_id, checkpoint_ns),
    ):
        if first:
            first = False
            if written_id is not None and cid != written_id:
                _log.warning(
                    "[checkpoint-prune] %s: the checkpoint just written (%s) sorts below an existing one (%s) "
                    "— clock skew? Skipping the write-path trim so the new turn isn't cut.",
                    thread_id,
                    written_id,
                    cid,
                )
                return 0
        if not root_aware or _is_root(md):
            roots += 1
            if roots == keep:
                cutoff = cid
                break
    if cutoff is None:
        return 0
    args = (thread_id, checkpoint_ns, cutoff)
    conn.execute("DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?", args)
    cur = conn.execute("DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?", args)
    return cur.rowcount


def _has_media_table(conn: sqlite3.Connection) -> bool:
    """The media-offload table (ADR 0120) exists only once a current saver opened the DB."""
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint_media'").fetchone()
        is not None
    )


#: The media GC leaves payloads younger than this alone: the serializer commits a payload
#: just BEFORE the row that references it, and a sweep landing in that gap must not take it.
MEDIA_GC_GRACE_S = 600.0


def _scan_refs(conn: sqlite3.Connection) -> set[bytes]:
    """Every media reference in checkpoint + write rows."""
    from graph.checkpoint_media import REF_BYTES_RE

    found: set[bytes] = set()
    for (blob,) in conn.execute("SELECT checkpoint FROM checkpoints"):
        found.update(REF_BYTES_RE.findall(bytes(blob or b"")))
    for (blob,) in conn.execute("SELECT value FROM writes"):
        found.update(REF_BYTES_RE.findall(bytes(blob or b"")))
    return found


def gc_media(
    db_path: str,
    *,
    grace_s: float = MEDIA_GC_GRACE_S,
    only: set[str] | None = None,
    now: float | None = None,
) -> int:
    """Delete offloaded media payloads that no checkpoint or write row references.

    Media is content-addressed per DB (a serializer has no thread context), so liveness
    is a reference scan over every row:

    1. Unlocked: scan, and take the payloads older than ``grace_s`` that nothing
       references as candidates (``only`` narrows them, e.g. to the payloads of a chat
       just deleted). No candidates — the steady state — means no write lock at all.
    2. ``BEGIN IMMEDIATE``: rescan EVERY row and delete the candidates still
       unreferenced. (A rowid-mark partial rescan missed rows that reused a deleted
       max rowid — #4313 review m1. Rows are bounded by retention on write, so the
       full rescan is cheap.)

    Writers re-check payload existence inside their own write transaction
    (``graph.checkpoint_serde.store_media``), so a row re-using a payload either sees
    this GC's delete and re-inserts it, or commits first and is seen here. Returns
    payloads removed."""
    import time as _time

    cutoff = (now if now is not None else _time.time()) - grace_s
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        if not _has_media_table(conn):
            return 0
        live = _scan_refs(conn)
        cands = [
            h
            for (h,) in conn.execute("SELECT hash FROM checkpoint_media WHERE created < ?", (cutoff,))
            if h.encode() not in live and (only is None or h in only)
        ]
        if not cands:
            return 0
        conn.execute("BEGIN IMMEDIATE")
        live = _scan_refs(conn)
        dead = [(h,) for h in cands if h.encode() not in live]
        conn.executemany("DELETE FROM checkpoint_media WHERE hash=?", dead)
        conn.commit()
        return len(dead)
    finally:
        conn.close()


def _refs_of(conn: sqlite3.Connection, where: str, args: tuple) -> set[str]:
    """Media hashes referenced by the rows ``where`` selects (before they're deleted)."""
    from graph.checkpoint_media import REF_BYTES_RE

    out: set[str] = set()
    if not _has_media_table(conn):
        return out
    for (blob,) in conn.execute(f"SELECT checkpoint FROM checkpoints WHERE {where}", args):
        out.update(h.decode() for h in REF_BYTES_RE.findall(bytes(blob or b"")))
    for (blob,) in conn.execute(f"SELECT value FROM writes WHERE {where}", args):
        out.update(h.decode() for h in REF_BYTES_RE.findall(bytes(blob or b"")))
    return out


def delete_thread(db_path: str, thread_id: str, *, cascade: bool = False) -> int:
    """Delete all checkpoints + writes for a thread. Returns checkpoints removed.

    When ``cascade`` is True, also deletes the sub-threads a chat owns: ids that start
    with ``thread_id`` followed by ``:goal-iter-`` (goal-mode iteration checkpoints) or
    ``::aside-`` (a ``/btw`` aside whose own post-answer cleanup failed — ``aside_op``
    deletes it best-effort, and a miss used to outlive the deleted chat)."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        if cascade:
            # An exact, case-sensitive prefix match (#3973) — not LIKE, whose ``%`` / ``_``
            # wildcards and ASCII case-folding made deleting ``a_b`` also take
            # ``aXb:goal-iter-*`` and ``chat-1`` take ``CHAT-1:goal-iter-*``.
            goal, aside = thread_id + _GOAL_ITER_SUFFIX, thread_id + _ASIDE_SUFFIX
            where = "thread_id=? OR substr(thread_id, 1, ?) = ? OR substr(thread_id, 1, ?) = ?"
            args = (thread_id, len(goal), goal, len(aside), aside)
        else:
            where, args = "thread_id=?", (thread_id,)
        refs = _refs_of(conn, where, args)
        n = conn.execute(f"SELECT COUNT(*) FROM checkpoints WHERE {where}", args).fetchone()[0]
        conn.execute(f"DELETE FROM checkpoints WHERE {where}", args)
        conn.execute(f"DELETE FROM writes WHERE {where}", args)
        conn.commit()
    finally:
        conn.close()
    if refs:
        # The chat's images go with it now, not at the next sweep — those no other chat
        # still references (media is shared per DB). Best-effort: the rows are already
        # gone, so a failure here must not fail the delete; the sweep's GC catches up
        # (#4313 review m2).
        try:
            gc_media(db_path, grace_s=0, only=refs)
        except sqlite3.Error:
            _log.warning("[checkpoint-prune] media GC after deleting %s failed; the sweep will retry", thread_id, exc_info=True)
    return n


def prune_checkpoints(
    db_path: str,
    *,
    keep_per_thread: int = 2,
    max_age_seconds: float | None = None,
    now: float | None = None,
    background_keep: int | None = None,
) -> dict[str, int]:
    """Trim the checkpoint DB. Returns counts of what was removed.

    ``max_age_seconds=None`` disables the age TTL (only the per-thread cap runs).
    ``now`` is injectable for tests.
    ``background_keep`` overrides the per-thread cap for ``a2a:background:*`` threads
    (resume-from-latest only — no time-travel, so retaining extras is waste).
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    threads_deleted = 0
    checkpoints_deleted = 0
    try:
        # 1. Age TTL — drop whole threads idle past the cutoff. One scan dates every
        #    thread (#3973 — was a query per thread).
        if max_age_seconds is not None:
            import time as _time

            cutoff = (now if now is not None else _time.time()) - max_age_seconds
            # Take the write lock BEFORE the scan (#3973): otherwise a chat reopened
            # mid-scan writes a new turn that the delete below then removes with the
            # thread it judged idle. Scan + delete are one short transaction; live
            # writers wait on their busy_timeout, readers (WAL) aren't blocked.
            conn.execute("BEGIN IMMEDIATE")
            # Only TTL threads we can date *and* that are entirely old.
            aged = [(tid,) for tid, ts in _newest_stamps(conn).items() if ts is not None and ts < cutoff]
            conn.executemany("DELETE FROM checkpoints WHERE thread_id=?", aged)
            conn.executemany("DELETE FROM writes WHERE thread_id=?", aged)
            conn.commit()
            threads_deleted = len(aged)

        # 2. Per-thread cap — keep the latest N ROOT checkpoints per namespace (ADR 0120:
        #    a ``task()`` subagent writes into its parent's thread, so a plain newest-N
        #    could delete the parent's resumable checkpoint mid-delegation). ONE grouped
        #    query finds the namespaces over the cap (#3973 — not a query per thread);
        #    only those are trimmed, newest-first, reading just the rows down to the
        #    cutoff. Background threads get a tighter cap (resume-from-latest only).
        keep = max(1, keep_per_thread)
        bg_keep = keep if background_keep is None else max(1, background_keep)
        over = conn.execute(
            "SELECT thread_id, checkpoint_ns FROM checkpoints GROUP BY thread_id, checkpoint_ns"
            " HAVING COUNT(*) > CASE WHEN substr(thread_id, 1, ?) = ? THEN ? ELSE ? END",
            (len(_BACKGROUND_PREFIX), _BACKGROUND_PREFIX, bg_keep, keep),
        ).fetchall()
        for tid, ns in over:
            checkpoints_deleted += trim_group(
                conn, tid, ns, bg_keep if tid.startswith(_BACKGROUND_PREFIX) else keep
            )

        conn.commit()
    finally:
        conn.close()
    return {"threads_deleted": threads_deleted, "checkpoints_deleted": checkpoints_deleted}


def reclaim(db_path: str, *, full_vacuum: bool = False) -> dict[str, int]:
    """Truncate the WAL and free unused DB pages back to the OS.

    Designed as a best-effort companion to ``prune_checkpoints``: after rows
    are deleted the DB file still holds their disk space (and the WAL may
    contain stale frames).  This call compacts both.

    * ``PRAGMA wal_checkpoint(TRUNCATE)`` — checkpoints the WAL and truncates
      it to zero, so the ``-wal`` file disappears.
    * ``PRAGMA incremental_vacuum`` — when ``auto_vacuum=INCREMENTAL`` (a DB
      ``build_sqlite_checkpointer`` creates, or one the boot migration converted),
      frees pages from the freelist back to the OS (``page_count`` drops). Cheap;
      safe alongside live writes.
    * A legacy DB (``auto_vacuum=NONE``: every DB the checkpointer created while it set
      WAL before the auto_vacuum pragma, which made that pragma a no-op) is NOT vacuumed unless ``full_vacuum=True`` (#3973). A full
      ``VACUUM`` rewrites the whole file under an exclusive lock, stalling every
      live checkpoint write for its duration — too costly for a periodic sweep on
      a running server. Left alone, the freed pages stay on the freelist and are
      reused by later writes, so the file stops growing; it just doesn't shrink.
      ``full_vacuum=True`` (an offline / maintenance caller) sets
      ``auto_vacuum=INCREMENTAL`` and then VACUUMs — the one-time migration, so
      every later reclaim is the cheap incremental kind. The boot path runs that
      migration automatically, before the checkpointer opens the DB
      (``graph/checkpoint_migrate.py``; ``protoagent checkpoint migrate`` on demand).
      By hand, with the server stopped: ``PRAGMA auto_vacuum=INCREMENTAL; VACUUM;``
      (a bare ``VACUUM`` shrinks once but leaves the mode NONE).

    Returns ``{"wal_truncated": int, "pages_reclaimed": int}``.
    Best-effort: any error is caught and logged; the returned counts are zero
    on failure.  Never raises.
    """
    result: dict[str, int] = {"wal_truncated": 0, "pages_reclaimed": 0}
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except Exception:
        _log.exception("[checkpoint-prune] reclaim failed on connect")
        return result
    try:
        # 1. Checkpoint + truncate the WAL. TRUNCATE shrinks the -wal file to
        #    zero bytes (it does NOT delete the file). The result row is
        #    (busy, log_frames, checkpointed_frames); busy == 0 means the
        #    checkpoint completed — i.e. no other open connection held it back.
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result["wal_truncated"] = 1 if (row is not None and row[0] == 0) else 0

        # 2. Determine auto_vacuum mode.  INCREMENTAL (2) → use the cheap
        #    incremental_vacuum PRAGMA; anything else → a full VACUUM, only when
        #    the caller opted in (it holds an exclusive lock for the whole rewrite).
        av_row = conn.execute("PRAGMA auto_vacuum").fetchone()
        av_mode = av_row[0] if av_row else 0
        page_count_before = conn.execute("PRAGMA page_count").fetchone()[0]

        if av_mode == 2:  # INCREMENTAL
            # The pragma frees ONE page per step of its result set: an undrained
            # ``execute`` handed back a single page per sweep (brandLaunch's log read
            # ``pages=1`` after deleting 1,988 checkpoints), leaving INCREMENTAL DBs at
            # 90-99% freelist. ``fetchall`` runs it to completion.
            conn.execute("PRAGMA incremental_vacuum").fetchall()
        elif full_vacuum:
            # The mode change only takes effect through the VACUUM that follows it —
            # together they migrate the DB, so later reclaims shrink it incrementally.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("VACUUM")
        else:
            _log.info(
                "[checkpoint-prune] %s predates auto_vacuum=INCREMENTAL — skipping the full "
                "VACUUM (it would lock out live writes); freed pages are reused. The next start "
                "migrates it (checkpoint.vacuum_legacy_on_start) so later prunes shrink the file; "
                "or, with the server stopped: protoagent checkpoint migrate (= sqlite3 %s "
                "'PRAGMA auto_vacuum=INCREMENTAL; VACUUM;')",
                db_path,
                db_path,
            )

        page_count_after = conn.execute("PRAGMA page_count").fetchone()[0]
        result["pages_reclaimed"] = max(0, page_count_before - page_count_after)
    except Exception:
        _log.exception("[checkpoint-prune] reclaim failed")
    finally:
        conn.close()
    return result


def plan_compaction(db_path: str, *, keep_per_thread: int = 2, background_keep: int = 1, top: int = 10) -> dict:
    """What :func:`compact` would remove — READ-ONLY (SELECTs only; safe on a live DB).

    Per (thread, ns) over the cap: the same root-aware cutoff :func:`trim_group` uses,
    then ``length()`` sums of the rows below it (header reads, not blob reads). Returns
    ``{"file_bytes", "page_size", "page_count", "freelist_pages", "auto_vacuum",
    "checkpoints_to_delete", "bytes_to_delete", "threads": [...top...],
    "expected_file_bytes"}``. ``expected_file_bytes`` assumes ``reclaim`` can return
    the space (auto_vacuum=INCREMENTAL); a NONE DB needs ``protoagent checkpoint
    migrate`` with the agent stopped."""
    from graph.checkpoint_health import file_bytes

    keep = max(1, keep_per_thread)
    bg_keep = max(1, background_keep)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        over = conn.execute(
            "SELECT thread_id, checkpoint_ns, COUNT(*) FROM checkpoints GROUP BY thread_id, checkpoint_ns"
            " HAVING COUNT(*) > CASE WHEN substr(thread_id, 1, ?) = ? THEN ? ELSE ? END",
            (len(_BACKGROUND_PREFIX), _BACKGROUND_PREFIX, bg_keep, keep),
        ).fetchall()
        threads = []
        for tid, ns, total in over:
            k = bg_keep if tid.startswith(_BACKGROUND_PREFIX) else keep
            roots, cutoff = 0, None
            for cid, md in conn.execute(
                "SELECT checkpoint_id, CAST(metadata AS TEXT) FROM checkpoints"
                " WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC",
                (tid, ns),
            ):
                if ns != "" or _is_root(md):
                    roots += 1
                    if roots == k:
                        cutoff = cid
                        break
            if cutoff is None:
                continue
            args = (tid, ns, cutoff)
            n, b = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(checkpoint)) + SUM(length(metadata)), 0) FROM checkpoints"
                " WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?",
                args,
            ).fetchone()
            wb = conn.execute(
                "SELECT COALESCE(SUM(length(value)), 0) FROM writes"
                " WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id<?",
                args,
            ).fetchone()[0]
            if n:
                threads.append({"thread_id": tid, "ns": ns, "checkpoints": total, "delete": n, "bytes": b + wb})
    finally:
        conn.close()
    threads.sort(key=lambda t: t["bytes"], reverse=True)
    to_delete = sum(t["bytes"] for t in threads)
    size = file_bytes(db_path)
    return {
        "file_bytes": size,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist,
        "auto_vacuum": auto_vacuum,
        "checkpoints_to_delete": sum(t["delete"] for t in threads),
        "bytes_to_delete": to_delete,
        "threads": threads[:top],
        "expected_file_bytes": max(0, size - to_delete - freelist * page_size),
    }


#: ``compact``'s media grace: shorter than the sweep's (an operator asked to reclaim now)
#: but never zero while an agent may be writing.
COMPACT_MEDIA_GRACE_S = 60.0


def compact(db_path: str, *, keep_per_thread: int = 2, background_keep: int = 1) -> dict:
    """Apply the plan: root-aware per-thread cap (no age TTL), drop unreferenced
    offloaded media, then :func:`reclaim` (WAL truncate + drained incremental vacuum).

    Safe alongside a running agent — the same deletes the periodic sweep makes, in
    short transactions, and an incremental (not full) vacuum — but the vacuum step
    rewrites the pages it moves through the WAL, so prefer a quiet moment."""
    res = prune_checkpoints(db_path, keep_per_thread=keep_per_thread, background_keep=background_keep)
    # A short grace even here: a live agent may have just committed a payload whose row
    # hasn't landed yet (#4306 review m1).
    res["blobs_deleted"] = gc_media(db_path, grace_s=COMPACT_MEDIA_GRACE_S)
    res.update(reclaim(db_path))
    return res
