#!/usr/bin/env python3
"""Find (and optionally remove) finished git worktrees under a directory of repos.

Sessions and agents make a worktree per task (`git worktree add ../<repo>-wt-<slug>`, a
board's `.worktrees/feat-*`) and don't always remove it after the PR merges. Each one is a
full checkout, often with its own `.venv` and `node_modules`, so they pile up: on 2026-11-18
the dev box was at 99% disk and 42 finished worktrees held about 19 GB.

A worktree is **finished** when all of these hold:
- its tracked files are unchanged (`git status --porcelain --untracked-files=no` is empty);
- it is not locked (`git worktree lock`);
- its branch is merged into the default branch, OR no longer exists on origin (repos that
  auto-delete merged branches squash-merge, so "gone upstream" is the merged signal there).

Removing a worktree never deletes its branch, so local commits stay reachable. Untracked
files (a `.venv`, scratch output) block a plain `git worktree remove`; with `--apply` those
worktrees are listed as needing a look, never forced.

Usage:
    python scripts/worktree_audit.py                 # report only (default)
    python scripts/worktree_audit.py --apply         # remove finished worktrees
    python scripts/worktree_audit.py --root ~/dev --json report.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def _git(repo: pathlib.Path, *args: str) -> tuple[int, str, str]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _size_gb(path: pathlib.Path) -> float:
    r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=600)
    try:
        return int(r.stdout.split()[0]) / 1048576
    except (IndexError, ValueError):
        return 0.0


def _worktrees(main: pathlib.Path) -> list[dict]:
    rc, out, _ = _git(main, "worktree", "list", "--porcelain")
    if rc:
        return []
    found, cur = [], {}
    for line in [*out.splitlines(), ""]:
        if not line:
            if cur:
                found.append(cur)
            cur = {}
        elif " " in line:
            key, value = line.split(" ", 1)
            cur[key] = value
        else:
            cur[line] = True
    return [w for w in found if pathlib.Path(w["worktree"]).resolve() != main.resolve()]


def audit(root: pathlib.Path) -> list[dict]:
    rows = []
    for main in sorted(p for p in root.iterdir() if (p / ".git").is_dir()):
        linked = _worktrees(main)
        if not linked:
            continue
        _git(main, "fetch", "-q", "--prune", "origin")
        _, head, _ = _git(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        base = head or "origin/main"
        for w in linked:
            path = pathlib.Path(w["worktree"])
            row = {"repo": main.name, "main": str(main), "path": str(path), "branch": "", "gb": 0.0}
            if not path.exists():
                rows.append({**row, "state": "missing", "finished": True})
                continue
            branch = str(w.get("branch", "")).replace("refs/heads/", "")
            ref = branch or w.get("HEAD", "HEAD")
            merged = _git(main, "merge-base", "--is-ancestor", ref, base)[0] == 0
            gone = (not branch) or not _git(main, "ls-remote", "--heads", "origin", branch)[1]
            dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=no")[1])
            locked = "locked" in w
            finished = not dirty and not locked and (merged or gone)
            state = " ".join(s for s, on in (("dirty", dirty), ("locked", locked), ("merged", merged), ("gone-upstream", gone)) if on)
            rows.append({**row, "branch": branch or "(detached)", "gb": round(_size_gb(path), 2),
                         "state": state or "active", "finished": finished})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(pathlib.Path.home() / "dev"), help="directory holding the repos (default ~/dev)")
    ap.add_argument("--apply", action="store_true", help="remove finished worktrees (default: report only)")
    ap.add_argument("--json", help="also write the full report to this file")
    args = ap.parse_args(argv)

    rows = audit(pathlib.Path(args.root).expanduser())
    rows.sort(key=lambda r: r["gb"], reverse=True)
    for r in rows:
        mark = "FINISHED" if r["finished"] else "keep    "
        print(f"{mark} {r['gb']:6.2f} GB  {r['repo']:26.26} {pathlib.Path(r['path']).name:50.50} {r['branch'][:44]:44.44} {r['state']}")
    done = [r for r in rows if r["finished"]]
    print(f"\n{len(done)} finished worktree(s), ~{sum(r['gb'] for r in done):.1f} GB; {len(rows) - len(done)} kept.")

    if args.apply:
        removed, blocked = 0, []
        for r in done:
            if r["state"] != "missing":
                rc, _, err = _git(pathlib.Path(r["main"]), "worktree", "remove", r["path"])
                if rc:
                    blocked.append((r["path"], (err.splitlines() or ["?"])[-1]))
                    continue
            removed += 1
        for main in {r["main"] for r in done}:
            _git(pathlib.Path(main), "worktree", "prune")
        print(f"removed {removed}; {len(blocked)} need a look (untracked files, never forced):")
        for path, err in blocked:
            print(f"  {path}: {err}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
