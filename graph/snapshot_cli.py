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
    sub = parser.add_subparsers(dest="cmd", metavar="<export|import>")
    p_export = sub.add_parser("export", help="Write a secret-free snapshot zip")
    p_export.add_argument(
        "-o",
        "--out",
        default="",
        help="Output path (default: ./<agent>-snapshot-<timestamp>.zip). A directory writes the default name inside it.",
    )
    p_export.add_argument(
        "--include-knowledge",
        action="store_true",
        help="ALSO export this agent's knowledge as text. NOT publishable — see REVIEW.md.",
    )
    p_export.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the review — what is stripped, what the target must supply — and write nothing.",
    )
    p_import = sub.add_parser("import", help="Stand up a fresh agent from a snapshot zip")
    p_import.add_argument("zip", help="Path to the snapshot zip")
    p_import.add_argument("--name", default="", help="Name for the new agent (default: the snapshot's)")
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan — plugins that would be installed, capabilities granted — and change nothing.",
    )
    p_import.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the plan non-interactively. Applying a snapshot RUNS the plugin code it names.",
    )
    p_import.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Supply a required credential (repeatable). Values are written 0600 to the new agent only.",
    )
    args = parser.parse_args(argv)
    if args.cmd == "import":
        return _cmd_import(args)
    if args.cmd != "export":
        parser.print_help()
        return 2

    from graph.snapshot_op import build_snapshot
    from infra.paths import instance_paths

    paths = instance_paths()
    knowledge = _collect_knowledge() if getattr(args, "include_knowledge", False) else None
    result = build_snapshot(
        config_yaml=paths.config_yaml,
        soul_path=paths.soul_path,
        plugins_lock=paths.plugins_lock,
        secrets_yaml=paths.secrets_yaml,  # read-only, for `was_set` only
        skills_dirs={"instance": paths.skills_dir, "config": paths.config_dir / "skills"},
        knowledge=knowledge,
    )

    # The review goes to STDERR so `--dry-run` stays greppable and a future `-o -` could
    # stream the zip to stdout without the summary corrupting it.
    def note(line: str = "") -> None:
        print(line, file=sys.stderr)

    note(f"Snapshot for {result.manifest['agent']['name']} — {len(result.data)} bytes")
    if result.carries_knowledge:
        total = sum(result.knowledge.values())
        note()
        note(f"!! CARRIES A KNOWLEDGE SEED — {total} chunk(s). This file is NOT publishable.")
        note("   Secret-free is not safe-to-share: no credentials, possibly private content.")
        for domain, n in sorted(result.knowledge.items()):
            note(f"     {domain:<28} {n}")
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


def _cmd_import(args) -> int:
    """`protoagent agent import <zip>` — inspect, show the plan, then apply on acknowledgement.

    The plan is printed BEFORE anything is written, every time, including with ``--yes``:
    the point is that the operator (or the log of a scripted run) has a record of which
    URLs were about to be executed, not just that someone said yes.
    """
    import sys
    from pathlib import Path

    from graph.snapshot_import import SnapshotError, apply_snapshot, inspect_snapshot

    def out(line: str = "") -> None:
        print(line, file=sys.stderr)

    src = Path(args.zip).expanduser()
    if not src.exists():
        out(f"error: no such file: {src}")
        return 1
    data = src.read_bytes()

    try:
        from graph.plugins.installer import configured_allowlist

        known = configured_allowlist()
    except Exception:  # noqa: BLE001 — familiarity is advisory; never block an import on it
        known = None

    try:
        plan = inspect_snapshot(data, known_sources=known)
    except SnapshotError as exc:
        out(f"error: {exc}")
        return 1

    name = (args.name or "").strip() or plan.agent_name
    out(f"Import plan — {src.name} → agent {name!r}")
    out()
    if plan.plugins:
        out("Plugins that will be INSTALLED AND RUN on this machine:")
        for p in plan.plugins:
            flag = "" if p.recognized else "   ⚠ unfamiliar source"
            out(f"  {p.id:<24} {p.url}@{p.ref or 'HEAD'}{flag}")
    else:
        out("Plugins: none.")
    if plan.capabilities:
        out()
        out("This snapshot's config grants:")
        for key, grants in plan.capabilities:
            out(f"  {key:<28} {grants}")
    needed = [r for r in plan.required_secrets if r.get("was_set")]
    if needed:
        out()
        out("Credentials you will need to supply (none travel in the snapshot):")
        for r in needed:
            out(f"  {r.get('name')}")
    for n in plan.notes:
        out(f"  note: {n}")

    if args.dry_run:
        out()
        out("Dry run — nothing written.")
        return 0

    if not args.yes:
        out()
        out("Refusing to apply without acknowledgement: this installs and runs the code above.")
        out("Re-run with --yes once you have read the plan.")
        return 2

    secrets: dict[str, str] = {}
    for pair in args.secret or []:
        key, sep, value = str(pair).partition("=")
        if sep and key.strip():
            secrets[key.strip()] = value

    try:
        res = apply_snapshot(data, name=name, acknowledged=True, secrets=secrets, plan=plan)
    except SnapshotError as exc:
        out(f"error: {exc}")
        return 1

    out()
    out(f"Created {res.name!r} at {res.path}")
    if res.installed:
        out(f"  installed: {', '.join(res.installed)}")
    for f in res.failed:
        out(f"  FAILED {f.get('id')}: {f.get('error')}")
    for n in res.notes:
        out(f"  {n}")
    if res.missing_secrets:
        out()
        out("INCOMPLETE — supply these before the agent will work:")
        for m in res.missing_secrets:
            out(f"  {m}")
        out("  (`protoagent agent import … --secret NAME=VALUE`, or Settings ▸ Secrets on the new agent)")
    return 0 if res.complete else 1


def _collect_knowledge():
    """Build the opt-in knowledge seed from this instance's store. Returns None when there is
    no store — an export must not fail because knowledge is unavailable."""
    from graph.snapshot_op import collect_knowledge_seed

    try:
        from runtime.state import STATE

        store = getattr(STATE, "knowledge_store", None)
        if store is None:
            # The CLI runs without a booted server, so build a store against this instance's
            # db directly rather than reporting "no knowledge" for a store that plainly exists.
            from infra.paths import instance_paths
            from knowledge import KnowledgeStore

            db = instance_paths().store("knowledge") / "agent.db"
            if not db.exists():
                return None
            store = KnowledgeStore(str(db))
        return collect_knowledge_seed(store)
    except Exception:  # noqa: BLE001 — the seed is optional; never lose the export over it
        import logging

        logging.getLogger(__name__).warning("[snapshot] knowledge seed collection failed", exc_info=True)
        return None
