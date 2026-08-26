"""Cross-layout factory reset for the current protoAgent instance.

The old shell implementation re-derived ``box_root/default`` and therefore could
not reach desktop or explicitly-scoped installs. This module consumes the same
frozen :class:`infra.paths.InstancePaths` object as the runtime, keeps planning
separate from execution, and makes box-shared OAuth preservation visible.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from infra.paths import InstancePaths, colocated_instances, instance_paths, pid_alive
from infra.proc import terminate_tree

BOX_SHARED_NAMES = frozenset(
    {
        "host-config.yaml",
        "commons",
        ".instances",
        ".data-version",
        "cache",
        "codex-oauth.json",
        "anthropic-oauth.json",
    }
)
OAUTH_STORES = {
    "codex-oauth.json": "ChatGPT",
    "anthropic-oauth.json": "Claude",
}
KEEP_SECRET_NAMES = ("secrets.yaml", "langgraph-config.yaml")


@dataclass(frozen=True)
class ResetPlan:
    paths: InstancePaths
    wipe_paths: tuple[Path, ...]
    preserved_shared: tuple[Path, ...]
    preserved_instances: tuple[Path, ...]
    unrecognized_box_entries: tuple[Path, ...]
    kept_secrets: tuple[Path, ...]
    signed_in: tuple[tuple[str, Path], ...]
    purge_box: bool

    @property
    def has_work(self) -> bool:
        return bool(self.wipe_paths)


@dataclass(frozen=True)
class ResetResult:
    changed: bool
    backup_path: Path | None = None


def _looks_like_instance_root(path: Path) -> bool:
    return path.is_dir() and ((path / "config").is_dir() or (path / ".instance-uid").exists())


def build_reset_plan(
    paths: InstancePaths | None = None,
    *,
    keep_secrets: bool = False,
    include_dev: bool = False,
    purge_box: bool = False,
) -> ResetPlan:
    """Describe exactly what a reset would wipe and preserve without mutating disk."""
    paths = paths or instance_paths()
    box = paths.box_root
    target = paths.instance_root

    if purge_box:
        wipe: tuple[Path, ...] = (box,) if box.exists() else ()
        try:
            target_inside_box = target.resolve().is_relative_to(box.resolve())
        except OSError:
            target_inside_box = False
        if target.exists() and not target_inside_box:
            wipe = (*wipe, target)
        signed_in = tuple((label, box / name) for name, label in OAUTH_STORES.items() if (box / name).exists())
        return ResetPlan(paths, wipe, (), (), (), (), signed_in, True)

    shared: list[Path] = []
    siblings: list[Path] = []
    unknown: list[Path] = []
    entries = sorted(box.iterdir(), key=lambda p: p.name) if box.is_dir() else []
    for entry in entries:
        if entry == target:
            continue
        if entry.name in BOX_SHARED_NAMES:
            shared.append(entry)
        elif _looks_like_instance_root(entry):
            siblings.append(entry)
        else:
            unknown.append(entry)

    wipe: list[Path] = []
    collapsed = target == box
    if collapsed:
        # Desktop sets PROTOAGENT_HOME == PROTOAGENT_BOX_ROOT. Its instance state
        # is loose beside machine-wide files, so wipe unclassified entries
        # individually while preserving explicit box state and nested siblings.
        wipe.extend(unknown)
    elif target.exists():
        wipe.append(target)

    dev = box / "dev"
    if include_dev and dev != target and dev.exists():
        if dev not in wipe:
            wipe.append(dev)
        siblings = [p for p in siblings if p != dev]
        unknown = [p for p in unknown if p != dev]

    kept = (
        tuple(target / "config" / name for name in KEEP_SECRET_NAMES if (target / "config" / name).is_file())
        if keep_secrets
        else ()
    )
    signed_in = tuple((label, box / name) for name, label in OAUTH_STORES.items() if (box / name).is_file())
    return ResetPlan(
        paths=paths,
        wipe_paths=tuple(wipe),
        preserved_shared=tuple(shared),
        preserved_instances=tuple(siblings),
        unrecognized_box_entries=tuple(unknown if not collapsed else ()),
        kept_secrets=kept,
        signed_in=signed_in,
        purge_box=False,
    )


def _assert_safe_delete(path: Path) -> None:
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home:
        raise ValueError(f"refusing to delete unsafe reset target: {resolved}")


def _delete(path: Path) -> None:
    _assert_safe_delete(path)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _backup(plan: ResetPlan) -> Path | None:
    candidates = list(plan.wipe_paths)
    if not plan.purge_box and plan.paths.host_config.exists() and plan.paths.host_config not in candidates:
        candidates.append(plan.paths.host_config)
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # The compatibility wrapper's contract is to place backups under ``$HOME``.
    # ``Path.home()`` ignores HOME on Windows and resolves USERPROFILE instead,
    # which made a successful reset report a backup somewhere the caller did not
    # request (and broke the same command across platforms).
    backup_home = Path(os.environ.get("HOME") or Path.home())
    destination = backup_home / f"protoagent-backup-{plan.paths.instance_id}-{stamp}.tar.gz"
    with tarfile.open(destination, "w:gz") as archive:
        for path in candidates:
            archive.add(path, arcname=path.name)
    return destination


def execute_reset(plan: ResetPlan, *, backup: bool = False) -> ResetResult:
    """Execute a previously-rendered plan. Returns false for a no-op plan."""
    if not plan.has_work:
        return ResetResult(False)
    backup_path = _backup(plan) if backup else None

    with tempfile.TemporaryDirectory(prefix="protoagent-reset-") as tmp:
        stash = Path(tmp)
        saved: list[tuple[Path, Path]] = []
        for original in plan.kept_secrets:
            if not original.is_file():
                continue
            copy = stash / original.name
            shutil.copy2(original, copy)
            saved.append((original, copy))

        for path in plan.wipe_paths:
            _delete(path)

        for original, copy in saved:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy, original)

    return ResetResult(True, backup_path)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _tracked_pid(paths: InstancePaths) -> int | None:
    try:
        record = json.loads((paths.instance_root / "server.pid").read_text(encoding="utf-8"))
        pid = int(record["pid"])
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pid = None
    if pid is not None and pid_alive(pid):
        return pid
    mine = str(paths.instance_root.resolve())
    for record in colocated_instances():
        try:
            other_root = record.get("instance_root")
            if other_root and str(Path(other_root).resolve()) == mine:
                return int(record["pid"])
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return None


def _guard_running_server(paths: InstancePaths, *, port: int, force: bool) -> bool:
    pid = _tracked_pid(paths)
    listening = _port_open(port)
    if pid is None and not listening:
        return True
    if pid is None:
        print(f"protoagent: something is listening on :{port}, but this instance did not start it; stop it before reset")
        return False
    if not force:
        print(f"protoagent: this instance is running (pid {pid}); stop it first or pass --force")
        return False
    if not terminate_tree(pid):
        print(f"protoagent: could not stop pid {pid}; reset aborted")
        return False
    return True


def _line(label: str, paths: tuple[Path, ...]) -> None:
    print(label)
    if not paths:
        print("  (none)")
    for path in paths:
        print(f"  {path}")


def render_plan(plan: ResetPlan, *, dry_run: bool) -> None:
    qualifier = "entire box" if plan.purge_box else f"instance {plan.paths.instance_id!r}"
    print(f"\nFactory reset — {qualifier}")
    print(f"  box:      {plan.paths.box_root}")
    print(f"  instance: {plan.paths.instance_root}")
    if dry_run:
        print("  mode:     DRY RUN (nothing will be deleted)")
    print()
    _line("Wipe:", plan.wipe_paths)
    if plan.kept_secrets:
        _line("Keep (--keep-secrets):", plan.kept_secrets)
    if not plan.purge_box:
        _line("Preserve (box-shared, machine-wide):", plan.preserved_shared)
        _line("Preserve (other instances):", plan.preserved_instances)
        _line("Preserve (unrecognized box-root entries):", plan.unrecognized_box_entries)
        if plan.signed_in:
            print("Still signed in after reset (machine-wide; use --purge-box to remove):")
            for label, path in plan.signed_in:
                print(f"  {label}: {path}")
    elif plan.signed_in:
        print("Remove protoAgent's machine-wide OAuth copies (vendor CLI logins are untouched):")
        for label, path in plan.signed_in:
            print(f"  {label}: {path}")


def run_reset_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="protoagent reset",
        description="Factory-reset the current instance using the runtime's resolved paths.",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="print the plan without changing anything")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the typed confirmation")
    parser.add_argument("--backup", action="store_true", help="create a timestamped tar.gz before deleting")
    parser.add_argument("--keep-secrets", action="store_true", help="preserve this instance's config credentials")
    parser.add_argument("--include-dev", action="store_true", help="also wipe the box's dev instance")
    parser.add_argument(
        "--purge-box",
        "--all",
        dest="purge_box",
        action="store_true",
        help="wipe every instance and machine-wide item, including protoAgent OAuth stores (machine handoff)",
    )
    parser.add_argument("--force", action="store_true", help="stop this instance's tracked server before reset")
    # Keep the compatibility wrapper's long-standing PORT contract. argparse applies
    # `type=int` to a string default too, so an invalid env value gets the same clear
    # usage error as an invalid --port rather than silently checking the wrong listener.
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT") or "7870",
        help="server port used by the running-process guard (default: PORT or 7870)",
    )
    args = parser.parse_args(argv)
    if args.purge_box and args.keep_secrets:
        parser.error("--purge-box cannot be combined with --keep-secrets")

    paths = instance_paths()
    plan = build_reset_plan(
        paths,
        keep_secrets=args.keep_secrets,
        include_dev=args.include_dev,
        purge_box=args.purge_box,
    )
    render_plan(plan, dry_run=args.dry_run)
    if not plan.has_work:
        print("\nNothing to reset; no instance state was found.")
        return 1
    if args.dry_run:
        print("\nDry run complete — nothing was changed.")
        return 0
    if not _guard_running_server(paths, port=args.port, force=args.force):
        return 1
    if not args.yes:
        word = "purge" if args.purge_box else "reset"
        reply = input(f"\nType {word!r} to continue: ").strip()
        if reply != word:
            print("aborted.")
            return 1

    result = execute_reset(plan, backup=args.backup)
    if not result.changed:
        print("Nothing changed.")
        return 1
    if result.backup_path:
        print(f"Backup: {result.backup_path}")
    print(f"\n✓ instance {paths.instance_id!r} reset. Next boot runs the setup wizard.")
    return 0


__all__ = ["ResetPlan", "ResetResult", "build_reset_plan", "execute_reset", "render_plan", "run_reset_cli"]
