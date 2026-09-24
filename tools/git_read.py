"""Hardened, read-only git primitives for the console code pane's Diff tab (ADR 0112).

``git diff`` is not a passive read. Repository-controlled configuration and attributes
can make it EXECUTE programs (https://nesbitt.io/2026/03/30/git-diff-drivers.html):

* ``diff.<driver>.command`` / ``diff.external`` / ``$GIT_EXTERNAL_DIFF`` — external diff
  programs, selected per path by a ``.gitattributes`` ``diff=<driver>`` line;
* ``diff.<driver>.textconv`` — a converter run over both sides before diffing;
* ``filter.<driver>.clean`` / ``.process`` — run by ``git status`` AND ``git diff`` to
  hash a stat-dirty working-tree file (``filter=<driver>`` in ``.gitattributes``);
* ``core.fsmonitor`` — a hook run on every index refresh;
* hooks (``post-index-change``) — run when a command writes the index;
* submodule recursion — every one of the above again, from the submodule's config.

``.gitattributes`` ships with a clone, and ``.git/config`` is one ``write_file`` away for
an agent with a writable project. Without hardening, "open the Diff tab" would be a
code-execution path from the agent's WRITE permission to the server user's shell even
when ``filesystem.allow_run`` is off. So every invocation here:

* runs a FIXED argv (no caller-supplied options; the only paths are git's own output,
  passed back after ``--`` as ``:(exclude,literal)`` pathspecs);
* passes ``--no-ext-diff --no-textconv``, ``-c core.fsmonitor=false``, ``-c diff.external=``,
  ``-c core.hooksPath=<devnull>``, ``--ignore-submodules=all``, and neutralises every
  ``filter.<name>.*`` driver found in the effective config (``-c filter.<name>.clean=`` …);
* scrubs every inherited ``GIT_*`` variable (``GIT_DIR``, ``GIT_EXTERNAL_DIFF``,
  ``GIT_CONFIG_*`` …), then sets ``GIT_OPTIONAL_LOCKS=0`` (never write the index) and
  ``GIT_TERMINAL_PROMPT=0``;
* is bounded by one overall deadline.

``tests/test_fs_diff_route.py`` proves this against a REAL repository whose config and
attributes try every vector above to create a marker file.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.fs_secrets import is_secret_path
from tools.fs_view import open_regular

DIFF_TIMEOUT_S = 10.0
MAX_PATCH_BYTES = 1024 * 1024
MAX_UNTRACKED_BYTES = 256 * 1024
# A repository with an un-ignored node_modules can have 10^5 untracked files; the list is
# for a human, so past this many entries it stops and says `truncated`.
MAX_FILES = 5000
_BINARY_SNIFF_BYTES = 8192


class GitTimeout(Exception):
    """The overall deadline passed before git finished."""


class GitError(Exception):
    """git failed in a way that isn't "this isn't a repository"."""


@dataclass
class DiffFile:
    path: str
    status: str  # "M" | "A" | "D" | "R" | "?"
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    denied: bool = False
    old_path: str | None = None

    def as_dict(self) -> dict:
        d = {
            "path": self.path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "denied": self.denied,
        }
        if self.old_path is not None:
            d["old_path"] = self.old_path
        return d


@dataclass
class WorkingTreeDiff:
    is_git: bool
    head: str | None = None
    branch: str | None = None
    files: list[DiffFile] = field(default_factory=list)
    patch: str = ""
    truncated: bool = False


def git_env() -> dict[str, str]:
    """The inherited environment minus every ``GIT_*`` variable, plus our safe settings.

    A superset of the specific scrub list (``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``/
    ``GIT_OBJECT_DIRECTORY``/``GIT_ALTERNATE_OBJECT_DIRECTORIES``): the server may itself
    run inside a git hook or a coder's worktree, and ``GIT_EXTERNAL_DIFF`` or
    ``GIT_CONFIG_PARAMETERS`` from there must not steer this read either.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


# Applied to EVERY invocation, including the config read that discovers filter names.
_BASE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.pager=cat",
)


class _Git:
    def __init__(self, cwd: Path, timeout: float):
        self.cwd = cwd
        self.deadline = time.monotonic() + timeout
        self.env = git_env()
        self.extra_config: list[str] = []

    def run(self, *args: str, ok_codes: tuple[int, ...] = (0,), stdin: bytes | None = None) -> bytes:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise GitTimeout()
        argv = ["git", "--no-pager", *_BASE_CONFIG, *self.extra_config, *args]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.cwd),
                env=self.env,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=left,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout() from exc
        except FileNotFoundError as exc:
            raise GitError("git is not installed") from exc
        if proc.returncode not in ok_codes:
            msg = proc.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {args[0]} failed ({proc.returncode}): {msg[:300]}")
        return proc.stdout

    def run_capped(self, *args: str, cap: int) -> tuple[bytes, bool]:
        """Like :meth:`run`, but STREAMS stdout and stops at ``cap`` bytes.

        ``git diff`` over a changed multi-GB file would otherwise be buffered whole before
        any cap applied. Past ``cap`` the process is killed and ``(first cap bytes, True)``
        returned; the deadline is enforced by a timer that kills git (a blocking pipe read
        can't time out on its own). stderr is discarded so it can't fill and deadlock.
        """
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise GitTimeout()
        argv = ["git", "--no-pager", *_BASE_CONFIG, *self.extra_config, *args]
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed") from exc
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(left, _expire)
        timer.daemon = True
        timer.start()
        buf = bytearray()
        capped = False
        try:
            while chunk := proc.stdout.read(65536):
                buf += chunk
                if len(buf) > cap:
                    capped = True
                    proc.kill()
                    break
        finally:
            timer.cancel()
            proc.stdout.close()
            proc.wait()
        if timed_out.is_set() and not capped:
            raise GitTimeout()
        if not capped and proc.returncode != 0:
            raise GitError(f"git {args[0]} failed ({proc.returncode})")
        return bytes(buf[:cap]), capped


def _neutralise_filters(git: _Git) -> None:
    """Blank every ``filter.<name>.*`` command the effective config defines.

    Filter drivers can't be switched off by a flag, and ``.gitattributes`` (which picks
    them) is repository content. Reading the CONFIG executes nothing, so enumerate the
    defined drivers and override each one's commands with ``-c`` (which outranks every
    config file). Verified: an empty ``clean``/``process`` with ``required=false`` makes
    git hash the raw bytes instead of running anything.
    """
    out = git.run("config", "-z", "--name-only", "--get-regexp", r"^filter\.", ok_codes=(0, 1))
    names: set[str] = set()
    for key in out.decode("utf-8", errors="surrogateescape").split("\0"):
        if not key.startswith("filter.") or key.count(".") < 2:
            continue
        names.add(key[len("filter.") : key.rindex(".")])
    for name in sorted(names):
        for var, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            git.extra_config += ["-c", f"filter.{name}.{var}={value}"]


def _z_fields(raw: bytes) -> list[str]:
    # "replace", not surrogateescape: these strings end up in a JSON response.
    parts = raw.decode("utf-8", errors="replace").split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def _synthetic_new_file(rel: str, data: bytes, *, mode: str = "100644") -> tuple[str, int]:
    """A ``git diff``-shaped "new file" patch for an untracked file, built in Python.

    Deliberately not ``git diff --no-index``: that would be another git invocation over
    repository-controlled attributes, and it can be pointed outside the fence. Returns
    (patch text, added line count).
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    no_newline = bool(lines) and lines[-1] != ""
    if not no_newline:
        lines.pop()
    head = f"diff --git a/{rel} b/{rel}\nnew file mode {mode}\n--- /dev/null\n+++ b/{rel}\n"
    if not lines:
        return head, 0
    body = f"@@ -0,0 +1,{len(lines)} @@\n" if len(lines) != 1 else "@@ -0,0 +1 @@\n"
    body += "".join(f"+{ln}\n" for ln in lines)
    if no_newline:
        body += "\\ No newline at end of file\n"
    return head + body, len(lines)


def working_tree_diff(root: Path, timeout: float = DIFF_TIMEOUT_S) -> WorkingTreeDiff:
    """The working tree of ``root`` vs ``HEAD`` — tracked changes plus untracked files.

    Scoped to ``root`` even when ``root`` is a subdirectory of a larger repository
    (``--relative`` + a ``.`` pathspec): the fence is the project root, not the repo.
    Raises :class:`GitTimeout` past the deadline, :class:`GitError` on other failures.
    """
    git = _Git(root, timeout)
    probe = git.run("rev-parse", "--is-inside-work-tree", ok_codes=(0, 128))
    if probe.strip() != b"true":
        return WorkingTreeDiff(is_git=False)
    _neutralise_filters(git)
    head = git.run("rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1)).decode().strip() or None
    branch = git.run("symbolic-ref", "--short", "-q", "HEAD", ok_codes=(0, 1)).decode("utf-8", "replace").strip()
    # An unborn branch has no HEAD to diff against: use the empty tree (computed, so it
    # is right for sha1 and sha256 repositories alike).
    base = head or git.run("hash-object", "-t", "tree", "--stdin", stdin=b"").decode().strip()

    diff_opts = ("--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all", "--relative", "-M")

    files: dict[str, DiffFile] = {}
    status_raw = git.run("diff", *diff_opts, "--name-status", "-z", base, "--", ".")
    fields = _z_fields(status_raw)
    i = 0
    while i < len(fields):
        code = fields[i]
        letter = code[:1]
        if letter in ("R", "C"):
            old, new = fields[i + 1], fields[i + 2]
            i += 3
            files[new] = DiffFile(path=new, status="R" if letter == "R" else "A", old_path=old)
        else:
            path = fields[i + 1]
            i += 2
            files[path] = DiffFile(path=path, status=letter if letter in ("A", "D") else "M")

    numstat_raw = git.run("diff", *diff_opts, "--numstat", "-z", base, "--", ".")
    nfields = _z_fields(numstat_raw)
    i = 0
    while i < len(nfields):
        adds, dels, path = (nfields[i].split("\t", 2) + ["", ""])[:3]
        i += 1
        if path == "":  # a rename: "adds\tdels\t", then the old and the new path
            path = nfields[i + 1] if i + 1 < len(nfields) else ""
            i += 2
        f = files.get(path)
        if f is None:
            continue
        if adds == "-" and dels == "-":
            f.binary = True
        else:
            f.additions, f.deletions = int(adds or 0), int(dels or 0)

    denied_specs: list[str] = []
    for f in files.values():
        if is_secret_path(f.path) or (f.old_path and is_secret_path(f.old_path)):
            f.denied = True
            f.additions = f.deletions = 0
            denied_specs.append(f":(exclude,literal){f.path}")
            if f.old_path:
                denied_specs.append(f":(exclude,literal){f.old_path}")

    raw_patch, patch_capped = git.run_capped("diff", *diff_opts, base, "--", ".", *denied_specs, cap=MAX_PATCH_BYTES)
    patch = raw_patch.decode("utf-8", errors="replace")

    # Untracked files. Porcelain paths are relative to the repository top, so strip the
    # project's prefix within it (empty when the project IS the repository).
    prefix = git.run("rev-parse", "--show-prefix").decode("utf-8", "surrogateescape").strip()
    st = git.run(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
        "--no-renames",
        "--",
        ".",
    )
    sfields = _z_fields(st)
    extra: list[str] = []
    files_truncated = False
    root_resolved = root.resolve()
    extra_bytes = 0
    for entry in sfields:
        if not entry.startswith("?? "):
            continue
        if time.monotonic() > git.deadline:
            raise GitTimeout()
        if len(files) >= MAX_FILES:
            files_truncated = True
            break
        top_rel = entry[3:]
        if prefix and not top_rel.startswith(prefix):
            continue
        rel = top_rel[len(prefix) :]
        f = DiffFile(path=rel, status="?")
        files[rel] = f
        if is_secret_path(rel):
            f.denied = True
            continue
        target = root / rel
        try:
            if target.is_symlink():
                # git records a symlink as its target string — show exactly that, and
                # never follow it (it may point outside the fence).
                chunk, adds = _synthetic_new_file(rel, os.readlink(target).encode(), mode="120000")
                f.additions = adds
                extra.append(chunk)
                continue
            resolved = target.resolve()
            if resolved != root_resolved and root_resolved not in resolved.parents:
                continue
            if is_secret_path(resolved.relative_to(root_resolved)):
                f.denied = True
                continue
            if not resolved.is_file():
                continue  # a nested repo's directory, a FIFO (reading one blocks forever), …
            if patch_capped or len(patch) + extra_bytes > MAX_PATCH_BYTES:
                # The patch is already past its cap: reading more content would only be
                # thrown away.
                extra.append(f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n")
                continue
            # A verified-regular descriptor (a FIFO or symlink swapped in after the checks
            # above is refused, never blocked on or followed), read no further than needed.
            with open_regular(resolved) as fh:
                size = os.fstat(fh.fileno()).st_size
                data = fh.read(MAX_UNTRACKED_BYTES + 1) if size <= MAX_UNTRACKED_BYTES else b""
            if size > MAX_UNTRACKED_BYTES or len(data) > MAX_UNTRACKED_BYTES:
                # Too big to show.
                extra.append(f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n")
                continue
        except OSError:
            continue
        if _looks_binary(data):
            f.binary = True
            extra.append(f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nBinary files /dev/null and b/{rel} differ\n")
            continue
        chunk, adds = _synthetic_new_file(rel, data)
        f.additions = adds
        extra.append(chunk)
        extra_bytes += len(chunk)

    full = patch + "".join(extra)
    truncated = files_truncated or patch_capped
    encoded = full.encode("utf-8")
    if len(encoded) > MAX_PATCH_BYTES or patch_capped:
        cut = encoded[:MAX_PATCH_BYTES]
        # End on a line boundary so the client's parser never sees half a line.
        nl = cut.rfind(b"\n")
        full = cut[: nl + 1 if nl >= 0 else len(cut)].decode("utf-8", errors="ignore")
        truncated = True
    return WorkingTreeDiff(
        is_git=True,
        head=head,
        branch=branch or None,
        files=sorted(files.values(), key=lambda f: f.path),
        patch=full,
        truncated=truncated,
    )
