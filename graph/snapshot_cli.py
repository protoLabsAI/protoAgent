"""``protoagent agent export`` — write this agent's secret-free snapshot to a zip.

The terminal half of ADR 0091 D1/D2 (#2103), re-parented into the ADR 0075 command tree
next to `plugin` / `workspace` / `fleet`. Same exporter as `POST /api/agent/export`, so
the two front doors can't drift.

Offline by design: it reads the instance root directly rather than calling the API, so a
snapshot can be taken from a **stopped** agent — which is the usual case when you are
about to move, fork, or archive one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def run_snapshot_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="protoagent agent",
        description="Export this agent as a portable, secret-free snapshot (ADR 0091).",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<export>")
    p_export = sub.add_parser("export", help="Write a secret-free snapshot zip")
    p_export.add_argument(
        "-o",
        "--out",
        default="",
        help="Output path (default: ./<agent>-snapshot-<timestamp>.zip). A directory writes the default name inside it.",
    )
    p_export.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the review — what is stripped, what the target must supply — and write nothing.",
    )
    args = parser.parse_args(argv)
    if args.cmd != "export":
        parser.print_help()
        return 2

    from graph.snapshot_op import build_snapshot
    from infra.paths import instance_paths

    paths = instance_paths()
    result = build_snapshot(
        config_yaml=paths.config_yaml,
        soul_path=paths.soul_path,
        plugins_lock=paths.plugins_lock,
        secrets_yaml=paths.secrets_yaml,  # read-only, for `was_set` only
        skills_dirs={"instance": paths.skills_dir, "config": paths.config_dir / "skills"},
    )

    # The review goes to STDERR so `--dry-run` stays greppable and a future `-o -` could
    # stream the zip to stdout without the summary corrupting it.
    def note(line: str = "") -> None:
        print(line, file=sys.stderr)

    note(f"Snapshot for {result.manifest['agent']['name']} — {len(result.data)} bytes")
    if result.required_secrets:
        note()
        note("The target must supply these credentials (names only — no values travel):")
        for r in result.required_secrets:
            note(f"  {r.name:<44} {r.kind:<12} {'set on source' if r.was_set else 'declared, unset'}")
    from graph.snapshot_op import NON_CREDENTIAL_KINDS

    creds = {w: [k for k in ks if k not in NON_CREDENTIAL_KINDS] for w, ks in result.pattern_redactions.items()}
    local = {w: [k for k in ks if k in NON_CREDENTIAL_KINDS] for w, ks in result.pattern_redactions.items()}
    if any(creds.values()):
        note()
        note("Credential-shaped text found and scrubbed — treat it as EXPOSED and rotate it:")
        for where, kinds in sorted(creds.items()):
            if kinds:
                note(f"  {where or '(root)'}: {', '.join(kinds)}")
    if any(local.values()):
        note()
        note("Machine-local paths scrubbed (not credentials — re-point these on the target):")
        for where, kinds in sorted(local.items()):
            if kinds:
                note(f"  {where or '(root)'}: {', '.join(kinds)}")
    for n in result.notes:
        note(f"  note: {n}")

    if args.dry_run:
        note()
        note("Dry run — nothing written.")
        return 0

    out = Path(args.out).expanduser() if args.out else Path.cwd() / result.filename
    if out.is_dir():
        out = out / result.filename
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(result.data)
    except OSError as exc:
        note(f"error: could not write {out}: {exc}")
        return 1

    note()
    note(f"Wrote {out}")
    note("Read REVIEW.md inside before publishing — pattern redaction is a safety net, not a guarantee.")
    return 0
