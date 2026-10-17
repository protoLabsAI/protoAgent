"""One-time migration of a legacy checkpoint DB to ``auto_vacuum=INCREMENTAL``.

A checkpoint DB in ``auto_vacuum=NONE`` never shrinks. That is every DB
``build_sqlite_checkpointer`` created until it set the pragma before ``journal_mode=WAL``:
switching to WAL writes the header first, after which the ``auto_vacuum`` pragma is a
silent no-op. So these DBs are "legacy" by accident, not just by age. The prune pass deletes its rows
correctly, but ``reclaim()`` can't hand the freed pages back to the OS: a NONE DB only
shrinks through a full ``VACUUM``, which ``reclaim()`` deliberately never runs on a live
server (#3973). The documented by-hand fix (stop the server, then
``PRAGMA auto_vacuum=INCREMENTAL; VACUUM;``) was never run in practice, and those files
grew without bound. Real cases: 34 GB holding ~85 MB of live rows (99.8% freelist), and
30 GB holding ~480 MB.

:func:`migrate_legacy_auto_vacuum` performs the migration once, at a moment when nothing
else can be writing:

* **At boot**, ``server.stores._build_checkpointer`` calls it before the checkpointer (or
  anything else in this process) opens the DB, on the synchronous path that runs before
  the server serves. Turned off with ``checkpoint.vacuum_legacy_on_start: false``.
* **On demand**, with the server stopped: ``protoagent checkpoint migrate`` (this
  instance), ``--workspace <member>`` (a fleet member's DB under this instance's
  ``workspaces/``), or ``--db <path>``. ``python -m graph.checkpoint_migrate`` does the
  same.

Guards. Every one of them skips with a log line instead of failing:

* **Exclusive access.** The connection takes ``locking_mode=EXCLUSIVE`` and a
  ``BEGIN EXCLUSIVE`` without waiting. Any other connection to the file, from any
  process (another server for the same instance, a CLI, a sibling still shutting down),
  holds at least a shared lock and makes that fail. The lock is then held until the
  migration finishes, so nothing can open the file mid-``VACUUM``.
* **Worth it.** Only when the freelist is at least half the file or at least 256 MB.
  Smaller amounts of freed space are reused by later writes anyway.
* **Disk space.** ``VACUUM`` builds a temporary copy of the live data (in SQLite's temp
  directory) and, in WAL mode, writes the new pages through the WAL. It needs about twice
  the live data free, plus a margin.
* **Best-effort.** Nothing raises. A failure is logged and the caller carries on with the
  DB as it was. ``VACUUM`` is transactional, so an interrupted run leaves the original
  intact.

The run logs the size before and after, and logs progress while a long ``VACUUM`` runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_log = logging.getLogger("protoagent.checkpoint_migrate")

#: Migrate when the freelist is at least this fraction of the file's pages...
MIN_FREE_FRACTION = 0.5
#: ...or holds at least this many bytes.
MIN_FREE_BYTES = 256 * 1024 * 1024
#: Headroom kept free on top of the VACUUM's own estimated needs.
DISK_MARGIN_BYTES = 64 * 1024 * 1024
#: How often a long VACUUM logs that it is still running.
PROGRESS_LOG_INTERVAL_S = 10.0

_AUTO_VACUUM_INCREMENTAL = 2


def _file_bytes(db: Path) -> int:
    """The DB plus its WAL: what the store really occupies on disk."""
    total = 0
    for p in (db, db.with_name(db.name + "-wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"  # pragma: no cover - unreachable


def _sqlite_temp_dir() -> str:
    """Where SQLite puts VACUUM's temporary copy (unix order: SQLITE_TMPDIR, TMPDIR, ...)."""
    return os.environ.get("SQLITE_TMPDIR") or tempfile.gettempdir()


def _disk_shortfall(db: Path, live_bytes: int) -> str | None:
    """Why there isn't room for the VACUUM, or None when there is.

    The temp copy is about ``live_bytes``. The WAL grows by about ``live_bytes`` as the
    rebuilt pages are written back. When the temp directory is on the DB's volume, both
    land there."""
    db_dir = str(db.parent)
    tmp_dir = _sqlite_temp_dir()
    try:
        same_volume = os.stat(db_dir).st_dev == os.stat(tmp_dir).st_dev
    except OSError:
        same_volume = True  # can't tell: assume the worst case, both on the DB volume
    need_db = live_bytes * (2 if same_volume else 1) + DISK_MARGIN_BYTES
    free_db = shutil.disk_usage(db_dir).free
    if free_db < need_db:
        return f"{_human(free_db)} free on the DB's volume, needs ~{_human(need_db)}"
    if not same_volume:
        need_tmp = live_bytes + DISK_MARGIN_BYTES
        free_tmp = shutil.disk_usage(tmp_dir).free
        if free_tmp < need_tmp:
            return f"{_human(free_tmp)} free in the temp dir {tmp_dir}, needs ~{_human(need_tmp)}"
    return None


def migrate_legacy_auto_vacuum(
    db_path: str | os.PathLike,
    *,
    min_free_fraction: float = MIN_FREE_FRACTION,
    min_free_bytes: int = MIN_FREE_BYTES,
    force: bool = False,
    progress_interval_s: float = PROGRESS_LOG_INTERVAL_S,
) -> dict:
    """Migrate a legacy (``auto_vacuum != INCREMENTAL``) checkpoint DB in place.

    Returns ``{"status": ..., "size_before": int, "size_after": int, "reason": str}``.
    ``status`` is one of:

    * ``missing``: no DB yet (a new one is created INCREMENTAL by the checkpointer).
    * ``healthy``: already INCREMENTAL, untouched.
    * ``not_worth_it``: legacy, but the freelist is below both thresholds.
    * ``busy``: another connection has the file open, so it was left alone.
    * ``low_disk``: not enough free space for the VACUUM.
    * ``migrated``: now INCREMENTAL and compacted.
    * ``error``: something failed. It was logged and the DB was left as SQLite left it.

    ``force`` skips the worth-it thresholds. It never skips the exclusivity or disk
    checks. Never raises."""
    db = Path(db_path)
    result: dict = {"status": "missing", "size_before": 0, "size_after": 0, "reason": ""}
    try:
        if not db.is_file() or db.stat().st_size == 0:
            return result
        result["size_before"] = result["size_after"] = _file_bytes(db)
        conn = sqlite3.connect(str(db), timeout=0.5, isolation_level=None)
    except Exception as exc:
        _log.exception("[checkpoint-migrate] couldn't open %s", db)
        result.update(status="error", reason=str(exc))
        return result
    try:
        # Exclusive access first, before reading anything: EXCLUSIVE locking mode keeps
        # the lock until close, so no other connection can open the file mid-VACUUM.
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            result.update(status="busy", reason="another connection has the DB open")
            _log.warning(
                "[checkpoint-migrate] %s is open elsewhere (another server or process for this "
                "instance?) — skipping the auto_vacuum migration; it is retried next start",
                db,
            )
            return result
        mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.execute("COMMIT")  # VACUUM can't run inside a transaction; the lock stays held

        if mode == _AUTO_VACUUM_INCREMENTAL:
            result.update(status="healthy")
            return result
        free_bytes = freelist * page_size
        live_bytes = max(0, page_count - freelist) * page_size
        fraction = freelist / page_count if page_count else 0.0
        if not force and fraction < min_free_fraction and free_bytes < min_free_bytes:
            result.update(
                status="not_worth_it",
                reason=f"{_human(free_bytes)} free ({fraction:.0%} of the file)",
            )
            _log.info(
                "[checkpoint-migrate] %s is auto_vacuum=%s with only %s free (%.0f%%) — not worth "
                "a VACUUM yet; leaving it",
                db,
                mode,
                _human(free_bytes),
                fraction * 100,
            )
            return result
        shortfall = _disk_shortfall(db, live_bytes)
        if shortfall:
            result.update(status="low_disk", reason=shortfall)
            _log.warning(
                "[checkpoint-migrate] NOT migrating %s (%s on disk, ~%s live): %s. Free some "
                "space, then restart or run `protoagent checkpoint migrate` with the server stopped",
                db,
                _human(result["size_before"]),
                _human(live_bytes),
                shortfall,
            )
            return result

        _log.warning(
            "[checkpoint-migrate] migrating %s to auto_vacuum=INCREMENTAL: %s on disk, ~%s live, "
            "%s free (%.0f%%). One-time VACUUM; startup waits for it",
            db,
            _human(result["size_before"]),
            _human(live_bytes),
            _human(free_bytes),
            fraction * 100,
        )
        started = time.monotonic()
        last_log = [started]

        def _progress() -> int:
            now = time.monotonic()
            if now - last_log[0] >= progress_interval_s:
                last_log[0] = now
                _log.warning("[checkpoint-migrate] still vacuuming %s (%.0fs so far)", db, now - started)
            return 0  # never abort

        conn.set_progress_handler(_progress, 100_000)
        try:
            # The mode change only takes effect through the VACUUM that follows it.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("VACUUM")
        finally:
            conn.set_progress_handler(None, 0)
        # The rebuilt pages went through the WAL; fold them in and drop it to zero.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        new_mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        conn.close()
        result["size_after"] = _file_bytes(db)
        if new_mode != _AUTO_VACUUM_INCREMENTAL:
            result.update(status="error", reason=f"auto_vacuum is {new_mode} after VACUUM")
            _log.error("[checkpoint-migrate] %s: VACUUM ran but auto_vacuum is still %s", db, new_mode)
            return result
        result.update(status="migrated")
        _log.warning(
            "[checkpoint-migrate] migrated %s: %s -> %s in %.1fs (auto_vacuum=INCREMENTAL; "
            "later prunes now shrink it)",
            db,
            _human(result["size_before"]),
            _human(result["size_after"]),
            time.monotonic() - started,
        )
        return result
    except Exception as exc:
        _log.exception("[checkpoint-migrate] migration of %s failed; continuing with it as is", db)
        result.update(status="error", reason=str(exc))
        try:
            result["size_after"] = _file_bytes(db)
        except Exception:
            pass
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI: `protoagent checkpoint migrate` / `python -m graph.checkpoint_migrate`
# ---------------------------------------------------------------------------


def _resolve_db(args) -> Path:
    if args.db:
        return Path(args.db).expanduser()
    from infra.paths import instance_paths

    paths = instance_paths()
    if args.workspace:
        return paths.workspaces_dir / args.workspace / "checkpoints.db"
    return paths.store("checkpoints.db")


def _run_compact(args) -> int:
    from graph.checkpoint_prune import compact, plan_compaction

    db = _resolve_db(args)
    if not db.exists():
        print(f"no checkpoint DB at {db}")
        return 2
    plan = plan_compaction(str(db), keep_per_thread=args.keep, background_keep=args.background_keep)
    plan["db"] = str(db)
    plan["mode"] = "apply" if args.apply else "dry-run"
    if args.apply:
        before = plan["file_bytes"]
        plan["result"] = compact(str(db), keep_per_thread=args.keep, background_keep=args.background_keep)
        plan["file_bytes_after"] = _file_bytes(db)
        plan["file_bytes_before"] = before
    if args.json:
        print(json.dumps(plan))
        return 0
    print(f"{db}  [{plan['mode']}]")
    print(
        f"  file {_human(plan['file_bytes'])} · freelist {_human(plan['freelist_pages'] * plan['page_size'])} · "
        f"auto_vacuum={'INCREMENTAL' if plan['auto_vacuum'] == 2 else plan['auto_vacuum']}"
    )
    print(
        f"  would delete {plan['checkpoints_to_delete']} checkpoint(s), {_human(plan['bytes_to_delete'])}"
        f" → expected file ≈ {_human(plan['expected_file_bytes'])}"
    )
    for t in plan["threads"]:
        ns = f" ns={t['ns']!r}" if t["ns"] else ""
        print(f"    {t['thread_id']}{ns}: {t['delete']}/{t['checkpoints']} rows, {_human(t['bytes'])}")
    if plan["auto_vacuum"] != 2:
        print("  note: auto_vacuum is not INCREMENTAL — freed space is reused but the file only shrinks after "
              "`protoagent checkpoint migrate` with the agent stopped")
    if args.apply:
        r = plan["result"]
        print(
            f"  applied: {r['checkpoints_deleted']} checkpoint(s), {r['media_deleted']} media payload(s) removed; "
            f"file {_human(plan['file_bytes_before'])} → {_human(plan['file_bytes_after'])}"
        )
    else:
        print("  dry run — nothing changed. Re-run with --apply to reclaim.")
    return 0


def run_checkpoint_cli(argv: list[str]) -> int:
    """``protoagent checkpoint migrate [--db PATH | --workspace NAME] [--force] [--json]``;
    ``protoagent checkpoint compact [--db PATH | --workspace NAME] [--keep N] [--apply] [--json]``."""
    parser = argparse.ArgumentParser(
        prog="protoagent checkpoint",
        description="Maintain this instance's conversation checkpoint DB (checkpoints.db).",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    mig = sub.add_parser(
        "migrate",
        help="One-time auto_vacuum=INCREMENTAL migration + VACUUM of a legacy DB (stop the server first)",
        description=(
            "Migrate a legacy checkpoint DB (auto_vacuum=NONE) to auto_vacuum=INCREMENTAL and "
            "VACUUM it, so the freed space goes back to the OS and later prunes keep it small. "
            "The agent that owns the DB must be stopped: an open DB is reported busy and left alone."
        ),
    )
    where = mig.add_mutually_exclusive_group()
    where.add_argument("--db", help="path to a checkpoints.db (default: this instance's)")
    where.add_argument("--workspace", help="a fleet member's DB, under this instance's workspaces/<name>/")
    mig.add_argument("--force", action="store_true", help="migrate even when little space would be freed")
    mig.add_argument("--json", action="store_true", help="print the result as JSON")
    cmp = sub.add_parser(
        "compact",
        help="Reclaim space from a bloated checkpoint DB (DRY RUN unless --apply)",
        description=(
            "Trim every session to its newest checkpoints (root-aware: an in-flight subagent's rows and "
            "the parent's resumable checkpoint survive), drop unreferenced offloaded media, and hand the "
            "freed pages back to the disk. Without --apply it only reports what it would remove "
            "(SELECT-only, safe on a live DB). With --apply it is safe alongside a running agent, but "
            "prefer a quiet moment: the vacuum step rewrites the pages it moves. ADR 0120."
        ),
    )
    cwhere = cmp.add_mutually_exclusive_group()
    cwhere.add_argument("--db", help="path to a checkpoints.db (default: this instance's)")
    cwhere.add_argument("--workspace", help="a fleet member's DB, under this instance's workspaces/<name>/")
    cmp.add_argument("--keep", type=int, default=2, help="root checkpoints kept per session (default 2)")
    cmp.add_argument("--background-keep", type=int, default=1, help="kept per a2a:background:* thread (default 1)")
    cmp.add_argument("--apply", action="store_true", help="actually delete + reclaim (default: dry run)")
    cmp.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    if args.action == "compact":
        return _run_compact(args)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    db = _resolve_db(args)
    res = migrate_legacy_auto_vacuum(db, force=args.force)
    res["db"] = str(db)
    if args.json:
        print(json.dumps(res))
    else:
        status = res["status"]
        if status == "missing":
            print(f"no checkpoint DB at {db}")
        elif status == "healthy":
            print(f"{db} is already auto_vacuum=INCREMENTAL; nothing to do")
        elif status == "migrated":
            print(f"migrated {db}: {_human(res['size_before'])} -> {_human(res['size_after'])}")
        elif status == "not_worth_it":
            print(f"{db}: {res['reason']} — not worth a VACUUM (use --force to run it anyway)")
        elif status == "busy":
            print(f"{db} is open in another process — stop the agent that owns it, then retry")
        else:
            print(f"{db}: {status}: {res['reason']}")
    return {"missing": 2, "busy": 1, "low_disk": 1, "error": 1}.get(res["status"], 0)


if __name__ == "__main__":  # pragma: no cover - thin entrypoint
    raise SystemExit(run_checkpoint_cli(sys.argv[1:]))
