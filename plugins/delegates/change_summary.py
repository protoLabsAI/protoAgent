"""What an unmanaged ACP delegate changed in a git project — the diff it hands back.

A ``delegate_to(project=…)`` dispatch without managed git (ADR 0076) used to return only
the coder's reply text, so the lead had to trust the coder's own account of its edits.
This snapshots the project's working tree before the dispatch and again after, and
renders the difference: a ``--stat``, the new files, and the unified diff capped at
``DIFF_CAP`` characters.

**Snapshot, not ``git diff HEAD``.** Each snapshot is a real git tree written through a
TEMPORARY index (a copy of the repo's own, so stat data is reused and it stays fast):
``git add -A`` into it, then ``git write-tree``. That captures tracked edits AND
untracked, non-ignored files without touching the operator's index, stash or working
tree. Diffing the two trees shows exactly what changed during the dispatch — a file that
was already dirty before it is not attributed to the delegate, and a commit the delegate
made is still visible as content. The tree ids stay in the object store, so the footer's
``git diff <before> <after>`` reproduces the full change after truncation.

Everything here is blocking ``git`` subprocess work; callers run it in a thread.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field

DIFF_CAP = 20_000
_GIT_TIMEOUT = 60.0
_MAX_LISTED_FILES = 50


class ChangeCaptureError(Exception):
    pass


@dataclass
class Snapshot:
    root: str
    head: str  # "" for a repo with no commits yet
    tree: str
    dirty: list[str] = field(default_factory=list)  # porcelain lines present BEFORE the dispatch


def _git(root: str, *args: str, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeCaptureError(f"git {args[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise ChangeCaptureError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def _base_env() -> dict:
    # No optional locks: `git status` must never refresh (and so write) the operator's
    # index while a delegate is working in the same tree.
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def is_git_worktree(root: str) -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", env=_base_env()).strip() == "true"
    except ChangeCaptureError:
        return False


def _head(root: str, env: dict) -> str:
    try:
        return _git(root, "rev-parse", "--verify", "-q", "HEAD", env=env).strip()
    except ChangeCaptureError:
        return ""  # unborn branch


def _write_tree(root: str, head: str, env: dict) -> str:
    """Tree id of the working tree under ``root`` (tracked + untracked, minus ignored)."""
    real_index = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "index", env=env).strip()
    with tempfile.TemporaryDirectory(prefix="pa-delegate-idx-") as td:
        tmp_index = os.path.join(td, "index")
        idx_env = {**env, "GIT_INDEX_FILE": tmp_index}
        try:
            if os.path.isfile(real_index):
                shutil.copyfile(real_index, tmp_index)  # reuse stat data → no full rehash
            elif head:
                _git(root, "read-tree", head, env=idx_env)
            _git(root, "add", "-A", "--", ".", env=idx_env)
        except ChangeCaptureError:
            # A split/sparse index doesn't survive being copied; rebuild from HEAD instead.
            if os.path.exists(tmp_index):
                os.unlink(tmp_index)
            if head:
                _git(root, "read-tree", head, env=idx_env)
            _git(root, "add", "-A", "--", ".", env=idx_env)
        return _git(root, "write-tree", env=idx_env).strip()


def snapshot(root: str) -> Snapshot:
    env = _base_env()
    head = _head(root, env)
    dirty = [
        ln for ln in _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".", env=env).splitlines() if ln
    ]
    return Snapshot(root=root, head=head, tree=_write_tree(root, head, env), dirty=dirty)


def after(before: Snapshot) -> Snapshot:
    env = _base_env()
    head = _head(before.root, env)
    return Snapshot(root=before.root, head=head, tree=_write_tree(before.root, head, env))


def render(before: Snapshot, after_: Snapshot, *, project: str = "", cap: int = DIFF_CAP, overlapped: bool = False) -> str:
    """The compact change summary appended to the delegate's reply."""
    root = before.root
    env = _base_env()
    where = f"project `{project}`" if project else f"`{root}`"
    lines = [f"── Changes in {where} during this delegation ──"]
    if before.head != after_.head:
        lines.append(f"HEAD moved {before.head[:12] or '(unborn)'} → {after_.head[:12] or '(unborn)'} (the delegate committed).")
    if before.tree == after_.tree:
        lines.append("No file changes.")
    else:
        stat = _git(root, "diff", "--relative", "--stat=120", before.tree, after_.tree, env=env).rstrip()
        lines.append(stat)
        added = [
            ln.split("\t", 1)[1]
            for ln in _git(
                root, "diff", "--relative", "--name-status", "--diff-filter=A", before.tree, after_.tree, env=env
            ).splitlines()
            if "\t" in ln
        ]
        if added:
            shown = ", ".join(added[:_MAX_LISTED_FILES])
            more = f" (+{len(added) - _MAX_LISTED_FILES} more)" if len(added) > _MAX_LISTED_FILES else ""
            lines.append(f"New files: {shown}{more}")
        patch = _git(root, "diff", "--relative", before.tree, after_.tree, env=env)
        if len(patch) > cap:
            lines.append("```diff\n" + patch[:cap].rstrip("\n") + "\n```")
            lines.append(
                f"[diff truncated at {cap:,} of {len(patch):,} chars — full change: "
                f"`git -C {root} diff {before.tree} {after_.tree}`]"
            )
        else:
            lines.append("```diff\n" + patch.rstrip("\n") + "\n```")
    if before.dirty:
        lines.append(
            f"Note: {len(before.dirty)} path(s) were already modified or untracked BEFORE this delegation; "
            "those pre-existing changes are excluded above."
        )
    if overlapped:
        lines.append(
            "Warning: another delegation was working in this project at the same time — "
            "some of these changes may be its, not this delegate's."
        )
    return "\n".join(lines)


# ── overlap tracking: two captures on one root can't tell whose edits are whose ──

_LOCK = threading.Lock()
_ACTIVE: dict[str, set[int]] = {}
_OVERLAPPED: set[int] = set()
_SEQ = itertools.count(1)


def begin(root: str) -> int:
    token = next(_SEQ)
    with _LOCK:
        live = _ACTIVE.setdefault(root, set())
        if live:
            _OVERLAPPED.update(live)
            _OVERLAPPED.add(token)
        live.add(token)
    return token


def end(root: str, token: int) -> bool:
    """Close a capture; True when another capture on the same root overlapped it."""
    with _LOCK:
        live = _ACTIVE.get(root)
        if live is not None:
            live.discard(token)
            if not live:
                _ACTIVE.pop(root, None)
        overlapped = token in _OVERLAPPED
        _OVERLAPPED.discard(token)
    return overlapped
