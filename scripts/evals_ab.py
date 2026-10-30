#!/usr/bin/env python
"""Learned-context A/B — does a source agent's memories + skills actually help?

Runs the eval suite twice, on two *throwaway* instances that differ only in what
they start with (ADR 0126 D6):

  * **Arm A (baseline)** — the bundled template only. No learned memories, no
    private skills: the agent as it ships.
  * **Arm B (learned)** — the same template, seeded with a COPY of a source
    instance's learned memories and its private skills.

Score B higher than A and the learned context earned its keep; score it the
same (or worse) and it did not. Part 2 (bd-kucq.5) adds the side-by-side
comparison report, a deliberately-bad-memory fixture and the docs; this half
produces the two arm reports the comparison reads.

**Copy, never mount or write.** The source instance is read-only throughout. Its
SQLite stores are snapshotted through the sqlite3 backup API over a ``mode=ro``
connection (a consistent image even of a live WAL DB, where a raw file copy could
tear); its on-disk store directories are copied recursively into a throwaway
staging dir. The source path is never handed to the arm agent, never opened
read-write and never symlinked — arm B boots on its own real copy, so nothing the
agent does can reach back to the instance being measured. The per-instance cases
file is copied the same way, so even the suite definition the runner loads is a
copy, not a source path (bd-1cky).

Usage::

    python scripts/evals_ab.py --ab-learned-from default
    python scripts/evals_ab.py --ab-learned-from dev --model vendor/some-model
    python scripts/evals_ab.py --ab-learned-from default --port-base 7992 --keep

Each arm's normal report lands in ``evals/results/`` named so the arm is obvious:
``ab-A-<ts>.json`` (baseline) and ``ab-B-<ts>.json`` (learned).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# Imported as a module (not by-value) so a test can monkeypatch sweep._RESULTS_DIR
# and both the arm reports and the sweep's own run reports follow it to the tmp dir.
from evals import sweep  # noqa: E402

# The source instance's learned stores, each relative to its instance root. SQLite
# files get a consistent snapshot via the backup API over a read-only connection;
# directories are copied recursively. An absent store is simply skipped, so a source
# with memories but no private skills (or vice versa) still A/Bs cleanly.
_SQLITE_STORES: tuple[str, ...] = ("knowledge/agent.db", "skills.db")
_TREE_STORES: tuple[str, ...] = ("memory", "skills")


def _sqlite_backup_ro(src: Path, dst: Path) -> None:
    """Snapshot ``src`` into a fresh ``dst`` via the sqlite3 backup API, opening the
    source READ-ONLY (``file:...?mode=ro``).

    The backup API copies a transactionally-consistent image — even of a database
    with a live WAL tail, where a plain file copy could catch a torn page or miss
    uncheckpointed pages. The read-only URI is the second belt: the source is never
    opened for writing, so an A/B run can never mutate the instance it measures."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{src.resolve().as_posix()}?mode=ro"
    src_conn = sqlite3.connect(uri, uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_learned_stores(source_root: Path, seed_dir: Path) -> list[str]:
    """Copy the source instance's learned stores into ``seed_dir`` without touching
    the source: SQLite stores through :func:`_sqlite_backup_ro`, directories through
    a real recursive copy. Returns the store names actually copied."""
    copied: list[str] = []
    for rel in _SQLITE_STORES:
        src = source_root / rel
        if src.is_file():
            _sqlite_backup_ro(src, seed_dir / rel)
            copied.append(rel)
    for rel in _TREE_STORES:
        src = source_root / rel
        if src.is_dir():
            # symlinks=False dereferences any symlink in the source, so the seed (and
            # therefore arm B's instance dir) holds real files, never a link back.
            shutil.copytree(src, seed_dir / rel, symlinks=False)
            copied.append(rel)
    return copied


def _default_model() -> str:
    """The default config's model — reuse ``scripts/evals_nightly.default_model`` so
    the arms sweep exactly the model the nightly would."""
    from scripts.evals_nightly import default_model

    return default_model()


def _run_arm(
    model: str,
    *,
    arm: str,
    port: int,
    instance: str,
    ts: int,
    keep: bool,
    seed_from_dir: Path | None,
    instance_cases: Path | None,
) -> dict | None:
    """Boot one throwaway arm and run the suite once; return its report dict (or
    ``None`` if the agent never came up). Reuses ``evals.sweep._run_one_model`` — the
    same boot/health/run/teardown path the model sweep uses — with the arm's learned
    seed (``seed_from_dir``) applied before boot."""
    runs = sweep._run_one_model(
        model,
        port=port,
        instance=instance,
        ts=ts,
        category=None,
        tasks=None,
        keep=keep,
        label=f"ab-{arm}",
        seed_steps=None,
        instance_cases=instance_cases,
        seed_from_dir=seed_from_dir,
    )
    return runs[0] if runs else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="A/B the eval suite: template-only baseline vs template + a source agent's learned context.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--ab-learned-from",
        required=True,
        help="source instance id whose learned memories + private skills seed arm B (copied, never mounted)",
    )
    p.add_argument("--model", default=None, help="model alias to run both arms on (default: the config's model)")
    p.add_argument("--port-base", type=int, default=7990, help="first port (arm A = port-base, arm B = port-base+1)")
    p.add_argument("--keep", action="store_true", help="keep each arm's instance data, logs and the copy staging dir")
    args = p.parse_args(argv)

    # FRICTION_LOG pollution guard, same reasoning as evals_nightly.sweep_env: the
    # throwaway arm agents run the friction plugin, and an inherited FRICTION_LOG
    # would pour their own tool errors into the operator's ledger. Pop it before any
    # arm subprocess inherits this process's environment.
    os.environ.pop("FRICTION_LOG", None)

    source = args.ab_learned_from.strip()
    if not source:
        sys.stderr.write("--ab-learned-from needs a source instance id\n")
        return 2

    if os.environ.get("PROTOAGENT_HOME", "").strip():
        # PROTOAGENT_HOME is terminal: every instance resolves to that one directory,
        # so the source, arm A and arm B would all share it. The source could not be
        # isolated from the arms (nor safely copied for them) — refuse rather than
        # measure a copy of the operator's own live data against itself.
        sys.stderr.write(
            "PROTOAGENT_HOME is set, which pins every instance to one root — the source instance and "
            "both arms would share it, so the source cannot be isolated from (or safely copied for) the "
            "arms. Unset it, or run the A/B on a plain host install.\n"
        )
        return 2

    source_root = sweep._instance_root(source)
    if not source_root.is_dir():
        sys.stderr.write(f"source instance {source!r} has no data dir at {source_root} — nothing to A/B\n")
        return 2

    model = args.model or _default_model()
    ts = int(time.time())
    staging = Path(tempfile.mkdtemp(prefix=f"evals-ab-{ts}-"))
    try:
        seed_dir = staging / "seed"
        seed_dir.mkdir()
        copied = _copy_learned_stores(source_root, seed_dir)
        print(f"copied {len(copied)} learned store(s) from {source!r}: {', '.join(copied) or '(none)'}")

        # Carry the source's per-instance cases (ADR 0126 D5) to BOTH arms as a COPY,
        # so A and B run the same case ids and the agent is never handed a path under
        # the source instance. Absent → both arms run the bundled suite alone.
        instance_cases: Path | None = None
        src_cases = source_root / "config" / "evals" / "cases.json"
        if src_cases.is_file():
            instance_cases = staging / "cases.json"
            shutil.copy2(src_cases, instance_cases)

        sweep._RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Arm A is the baseline (no seed); arm B is the learned arm (template + the copy).
        reports: dict[str, dict | None] = {}
        for i, (arm, seed) in enumerate((("A", None), ("B", seed_dir))):
            report = _run_arm(
                model,
                arm=arm,
                port=args.port_base + i,
                instance=f"eval-ab-{ts}-{arm}",
                ts=ts,
                keep=args.keep,
                seed_from_dir=seed,
                instance_cases=instance_cases,
            )
            out = sweep._RESULTS_DIR / f"ab-{arm}-{ts}.json"
            if report is not None:
                out.write_text(json.dumps(report, indent=2))
                print(f"arm {arm} report: {out}")
            else:
                print(f"  ✗ arm {arm} produced no report (agent never became healthy?)", file=sys.stderr)
            reports[arm] = report

        if reports["A"] is None or reports["B"] is None:
            sys.stderr.write("one or both arms produced no report — not a comparable A/B\n")
            return 1
        print(f"\nA/B arms written to {sweep._RESULTS_DIR} (ab-A-{ts}.json, ab-B-{ts}.json)")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
