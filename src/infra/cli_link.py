"""``protoagent cli`` — put the desktop app's own ``protoagent`` command on PATH.

The desktop app ships the whole CLI inside its server binary
(``protoAgent.app/Contents/Resources/protoagent-server/protoagent-server`` on macOS), but
nothing ever put that binary on PATH. So an operator who typed ``protoagent`` got whatever
an older ``uv tool install`` left behind — in the v0.231.0 stress test a v0.207.0 copy with
no ``usage`` command and no ``fleet --plugins`` — and never knew it.

``protoagent-server cli link`` creates ``~/.local/bin/protoagent`` as a symlink to the
running binary (the tray's *Install Command-Line Tool…* runs exactly this). A symlink, not
a copy: the link follows the app through in-place updates, because the app path stays the
same. The rules, so a link never clobbers something the operator installed on purpose:

- nothing there → link it;
- already our link to this binary → nothing to do;
- a link an earlier desktop build made (it points at a ``protoagent-server`` binary,
  possibly one that no longer exists) → replace it;
- anything else (a ``uv tool`` / ``pip`` install, a file) → refuse and say what it is,
  unless ``--force``.

``unlink`` removes only a link this command would have made. ``status`` reports the link
and what ``protoagent`` on PATH actually runs. Only a frozen build can be linked: a source
checkout's ``protoagent`` comes from its own install (``uv tool install`` / ``pip``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

LINK_NAME = "protoagent"
#: Basenames of the frozen server binary (``build_sidecar.py`` ``NAME``). A symlink that
#: points at one of these was made by this command (or by hand, to the same effect).
_SERVER_NAMES = frozenset({"protoagent-server", "protoagent-server.exe"})


def default_link_dir() -> Path:
    """``~/.local/bin`` — the XDG user bin dir, which ``uv`` and ``pipx`` also use, so on
    most machines it's already on PATH and needs no ``sudo``."""
    return Path.home() / ".local" / "bin"


def frozen_target() -> tuple[Path | None, str]:
    """The binary a link should point at, or ``(None, why)`` when this build can't be linked."""
    if not getattr(sys, "frozen", False):
        return None, (
            "this protoagent runs from a Python install, not the desktop app; "
            "its command comes from that install (uv tool install / pip)"
        )
    if os.environ.get("APPIMAGE"):
        # An AppImage runs from a mount that disappears when it exits, so a link to
        # sys.executable would dangle at once.
        return None, "an AppImage runs from a temporary mount, so it can't be linked; install the .deb instead"
    return Path(sys.executable).resolve(), ""


def _is_our_link(path: Path) -> bool:
    return path.is_symlink() and Path(os.readlink(path)).name in _SERVER_NAMES


def _describe(path: Path) -> str:
    """A human description of whatever is at ``path`` that isn't our link."""
    if path.is_symlink():
        dest = os.readlink(path)
        if "/uv/tools/" in dest.replace("\\", "/"):
            return f"a uv tool install ({dest}); `uv tool uninstall protolabs-agent` removes it"
        return f"a link to {dest}"
    return "a file (a pip or manual install)"


def inspect(link_dir: Path, target: Path | None) -> dict:
    """The state of ``<link_dir>/protoagent``, without changing anything."""
    link = link_dir / LINK_NAME
    if not link.exists() and not link.is_symlink():
        state = "absent"
    elif target is not None and link.is_symlink() and link.resolve() == target:
        state = "linked"
    elif _is_our_link(link):
        state = "stale"  # an earlier desktop build's link (maybe dangling)
    else:
        state = "foreign"
    return {"link": str(link), "state": state, "points_to": os.readlink(link) if link.is_symlink() else None}


def _on_path(link_dir: Path) -> bool:
    want = link_dir.resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).expanduser().resolve() == want:
            return True
    return False


def _path_report(link_dir: Path, target: Path | None) -> dict:
    """What ``protoagent`` on PATH runs, and whether that's this build."""
    found = shutil.which(LINK_NAME)
    resolved = str(Path(found).resolve()) if found else None
    return {
        "dir_on_path": _on_path(link_dir),
        "which": found,
        "which_resolves_to": resolved,
        "is_this_build": bool(target is not None and resolved == str(target)),
    }


def link(link_dir: Path, target: Path, *, force: bool = False) -> dict:
    """Create or refresh ``<link_dir>/protoagent`` → ``target``. Never touches a foreign
    file unless ``force``. Returns the outcome (``ok`` False when it refused)."""
    before = inspect(link_dir, target)
    state = before["state"]
    out = {**before, "target": str(target), "ok": True}
    if state == "linked":
        out["action"] = "none"
        return out
    if state == "foreign" and not force:
        out.update(ok=False, action="refused", reason=f"{before['link']} is {_describe(Path(before['link']))}")
        return out
    link_dir.mkdir(parents=True, exist_ok=True)
    path = Path(before["link"])
    if state != "absent":
        path.unlink()
    path.symlink_to(target)
    out.update(action="replaced" if state != "absent" else "created", state="linked", points_to=str(target))
    return out


def unlink(link_dir: Path, target: Path | None) -> dict:
    """Remove ``<link_dir>/protoagent`` only when it's a link this command makes."""
    before = inspect(link_dir, target)
    out = {**before, "ok": True}
    if before["state"] == "absent":
        out["action"] = "none"
    elif before["state"] in ("linked", "stale"):
        Path(before["link"]).unlink()
        out.update(action="removed", state="absent", points_to=None)
    else:
        out.update(
            ok=False, action="refused", reason=f"{before['link']} is {_describe(Path(before['link']))}, not our link"
        )
    return out


def _print_human(cmd: str, res: dict) -> None:
    if not res.get("ok", True):
        print(f"protoagent cli {cmd}: {res.get('reason')}", file=sys.stderr)
        if res.get("action") == "refused" and cmd == "link":
            print("Remove it first, or re-run with --force to replace it.", file=sys.stderr)
        return
    action = res.get("action")
    if action in ("created", "replaced"):
        print(f"Linked {res['link']} -> {res['target']}")
    elif action == "removed":
        print(f"Removed {res['link']}")
    elif cmd == "link":
        print(f"Already linked: {res['link']} -> {res['points_to']}")
    elif cmd == "unlink":
        print(f"Nothing to remove at {res['link']}")
    else:
        print(f"{res['link']}: {res['state']}" + (f" -> {res['points_to']}" if res.get("points_to") else ""))
    p = res.get("path") or {}
    if not p.get("dir_on_path"):
        d = str(Path(res["link"]).parent)
        print(f"{d} is not on your PATH. Add it, e.g.:\n  echo 'export PATH=\"{d}:$PATH\"' >> ~/.zshrc")
    elif cmd != "unlink" and not p.get("is_this_build"):
        if p.get("which"):
            print(f"Note: `protoagent` on your PATH still runs {p['which']} (it comes first); remove that one.")


def run_cli_link_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="protoagent cli",
        description="Put this desktop build's `protoagent` command on your PATH (a symlink in ~/.local/bin).",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<link|unlink|status>")
    for name, help_ in (
        ("link", "Create or refresh the `protoagent` link to this build"),
        ("unlink", "Remove the link (only one this command made)"),
        ("status", "Show the link and what `protoagent` on PATH runs"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dir", type=Path, default=None, help="where to put the link (default ~/.local/bin)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "link":
            p.add_argument("--force", action="store_true", help="replace a `protoagent` this command didn't make")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2

    link_dir = (args.dir or default_link_dir()).expanduser()
    target, why = frozen_target()
    if os.name == "nt":
        res = {"ok": False, "action": "unsupported", "reason": "Windows: add the app's install folder to PATH instead"}
    elif args.cmd == "link" and target is None:
        res = {"ok": False, "action": "unsupported", "reason": why}
    elif args.cmd == "link":
        res = link(link_dir, target, force=args.force)
    elif args.cmd == "unlink":
        res = unlink(link_dir, target)
    else:
        res = {**inspect(link_dir, target), "ok": True, "action": "none", "target": str(target) if target else None}
    if res.get("action") != "unsupported":
        res["path"] = _path_report(link_dir, target)

    if args.json:
        print(json.dumps(res, indent=2))
    elif res.get("action") == "unsupported":
        print(f"protoagent cli {args.cmd}: {res['reason']}", file=sys.stderr)
    else:
        _print_human(args.cmd, res)
    if res.get("action") == "unsupported":
        return 2
    return 0 if res.get("ok") else 1
