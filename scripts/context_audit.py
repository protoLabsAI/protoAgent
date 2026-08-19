#!/usr/bin/env python3
"""Audit what a chat thread's context window is actually made of.

    python scripts/context_audit.py chat-1787112385957-ljzrpn
    python scripts/context_audit.py a2a:chat-… --db /path/to/checkpoints.db --json

Loads the thread's checkpoint, sizes it with ``graph.context_audit_op`` (tool
arguments counted exactly once — see graph/message_blocks.py for the contract),
and joins telemetry's per-turn ``context_tokens`` to expose the FIXED per-call
overhead (system prompt + SOUL + bound tool schemas + hot memory) that no
checkpoint contains: ``fixed ≈ last context_tokens − measured history``.

Safe against a LIVE instance: the checkpoint/telemetry DBs are copied (db + -wal
+ -shm) to a temp dir and read from the copy — never opened read-write under a
running server. Paths default to the current instance (PROTOAGENT_INSTANCE
honored); point --db/--telemetry at another agent's store to audit a fleet
member (e.g. ``<box>/workspaces/<member>/checkpoints.db``).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _copy_store(db: Path, tmp: Path) -> Path:
    """Copy a sqlite store (+wal/shm sidecars) so a live writer is never disturbed."""
    dest = tmp / db.name
    shutil.copy2(db, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


def _load_messages(db: Path, thread_id: str) -> list:
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(db)) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            return []
        return list(tup.checkpoint.get("channel_values", {}).get("messages") or [])


def _last_context_tokens(db: Path, session_id: str) -> int | None:
    import sqlite3

    try:
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT context_tokens FROM turns WHERE session_id = ? ORDER BY ended_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        con.close()
        return int(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("session", help="session id (chat-…) or full thread id (a2a:chat-…)")
    p.add_argument("--db", default=None, help="checkpoints.db path (default: this instance's)")
    p.add_argument("--telemetry", default=None, help="telemetry.db path (default: beside --db)")
    p.add_argument("--top", type=int, default=15, help="how many biggest blocks to list")
    p.add_argument("--json", action="store_true", help="emit the raw breakdown as JSON")
    args = p.parse_args()

    from graph.context_audit_op import audit_messages

    if args.db:
        db = Path(args.db).expanduser()
    else:
        from infra.paths import instance_paths

        db = Path(instance_paths().store("checkpoints.db"))
    if not db.exists():
        print(f"no checkpoint DB at {db}", file=sys.stderr)
        return 2
    tele = Path(args.telemetry).expanduser() if args.telemetry else db.parent / "telemetry.db"

    session = args.session
    thread_id = session if session.startswith("a2a:") else f"a2a:{session}"
    session_id = thread_id.removeprefix("a2a:")

    with tempfile.TemporaryDirectory(prefix="ctx-audit-") as tmp:
        tmpdir = Path(tmp)
        messages = _load_messages(_copy_store(db, tmpdir), thread_id)
        if not messages:
            print(f"thread {thread_id!r} not found (or empty) in {db}", file=sys.stderr)
            return 1
        report = audit_messages(messages, top_n=args.top)
        window = _last_context_tokens(_copy_store(tele, tmpdir), session_id) if tele.exists() else None

    history = report["total_est_tokens"]
    if window:
        report["window_context_tokens"] = window
        report["fixed_overhead_est_tokens"] = max(0, window - history)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"thread {thread_id} · {report['message_count']} messages · history ≈ {history:,} tok")
    if window:
        fixed = report["fixed_overhead_est_tokens"]
        pct = 100 * fixed // window if window else 0
        print(f"window (telemetry) = {window:,} tok → fixed per-call overhead ≈ {fixed:,} tok ({pct}%)")
        print("  (system prompt + SOUL + bound tool schemas + hot memory — rides on EVERY model call)")
    print("\n== categories ==")
    for k, v in report["categories"].items():
        print(f"{v:>9,}  {k}")
    print("\n== tool call args (counted once) ==")
    for k, v in list(report["tool_call_args"].items())[:12]:
        print(f"{v:>9,}  {k}")
    print("\n== tool results ==")
    for k, row in list(report["tool_results"].items())[:12]:
        print(f"{row['est_tokens']:>9,}  ×{row['calls']:<4} {k}")
    print(f"\n== top {args.top} single blocks ==")
    for b in report["top_blocks"]:
        print(f"{b['est_tokens']:>9,}  {b['kind']:32} {b['preview']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
