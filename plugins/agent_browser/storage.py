"""The write fence for captured files — screenshots and PDFs.

The manifest has always declared ``filesystem: scoped``, but nothing enforced it:
``browser_screenshot(path)`` handed the path straight to the CLI, so
``browser_screenshot("~/.ssh/authorized_keys")`` would have Chrome overwrite it, and a
prompt-injected page ("save a screenshot to …") could pick the target. #3451 makes the
declaration true: every capture path is resolved INSIDE this plugin's own instance store
and an escape is **refused**, not silently redirected — writing an operator's data
somewhere unexpected is worse than failing loudly (the same rule
``graph/sdk.plugin_store`` applies to its ``subdir``).

The root is the host's plugin store (``sdk.plugin_store(plugin_id="agent_browser")`` →
``<instance_root>/agent_browser/captures``), so it is per-instance (ADR 0004: the dev
sandbox writes to its own root, never the default instance's) and it lands where the rest
of this plugin's state would. ``protoagent config explain`` prints the instance root, so
an operator can always find the files.

Containment is checked AFTER ``Path.resolve()``, which normalizes ``..`` and follows
symlinks — so ``../../etc/x``, an absolute path, and a symlink inside the root pointing
out are all caught by the same check.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("protoagent.plugins.agent_browser")

CAPTURE_SUBDIR = "captures"
PLUGIN_ID = "agent_browser"

# Retention. Captures are disposable by nature — a screenshot the agent took to read a
# page, a PDF it already handed to save_file_artifact (which copies the bytes into its own
# blob store). Nothing re-reads them, so an unbounded directory is pure growth: the fence
# is inside the instance root, which the operator backs up and `config explain` points at.
# Pruned oldest-first after each successful capture, on BOTH axes.
MAX_CAPTURE_FILES = 200
MAX_CAPTURE_BYTES = 512 * 1024 * 1024

# The artifact plugin's default `max_blob_kb` (25 MB, plugins/artifact/_config.py). A
# capture bigger than this is fine on disk but will be REFUSED by save_file_artifact, so
# the tool says so up front instead of letting the agent discover it one tool later.
ARTIFACT_BLOB_LIMIT_BYTES = 25 * 1024 * 1024


def capture_root() -> Path:
    """The only directory the capture tools may write to (created on demand)."""
    from graph import sdk

    return sdk.plugin_store(CAPTURE_SUBDIR, plugin_id=PLUGIN_ID)


def unique_default_name(default_name: str) -> str:
    """``page.pdf`` → ``page-20260911-174233-9f3a.pdf``.

    Used when the caller names no file. Concurrent agents (or one agent in a loop) all
    calling ``browser_pdf()`` would otherwise resolve to the same ``page.pdf`` and
    silently clobber each other's output — the second caller hands ``save_file_artifact``
    the first caller's page.
    """
    stem, _, suffix = default_name.rpartition(".")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stem or default_name}-{stamp}-{secrets.token_hex(2)}" + (f".{suffix}" if suffix else "")


# ── writing a capture without ever touching the previous file ──────────────────────
# The CLI writes to a short TEMP name beside the target; a finished, non-empty temp is
# swapped into place with one os.replace (atomic on one filesystem). The previous file is
# never moved, so there is no window in which it exists only under another name: a turn
# cancelled mid-capture, a `kill -9`, a concurrent export to the same name, or another
# capture's prune can each lose at most a disposable temp. (#3451's second round "parked"
# the old file as `.name.<hex>.prev` and restored it on failure; every one of those
# interruptions lost the user's last good export.)
#
# Temp names are fixed-length — prefix + 16 hex + the real extension (so the CLI still
# infers the format) — and never embed the target's stem, so a name near the 255-char
# limit can be re-exported as often as the first time.
TEMP_PREFIX = ".abtmp-"
_MAX_TEMP_SUFFIX = 16
# A live capture's temp is at most `timeout_s` old (60 s by default). One an hour old
# belongs to a capture that was cancelled or killed after the CLI got going, and is swept.
STALE_TEMP_S = 3600.0

_INFLIGHT: dict[str, int] = {}
_INFLIGHT_LOCK = threading.Lock()


def temp_path_for(target: Path) -> Path:
    """A fresh, short temp path beside ``target`` with the same extension."""
    suffix = target.suffix if 0 < len(target.suffix) <= _MAX_TEMP_SUFFIX else ""
    return target.with_name(f"{TEMP_PREFIX}{secrets.token_hex(8)}{suffix}")


def is_temp(path: Path) -> bool:
    return path.name.startswith(TEMP_PREFIX)


@contextlib.contextmanager
def in_flight(target: Path):
    """Mark ``target`` as being exported for the duration, so no prune — this capture's or
    a concurrent one's — deletes a file someone is in the middle of replacing."""
    key = str(target)
    with _INFLIGHT_LOCK:
        _INFLIGHT[key] = _INFLIGHT.get(key, 0) + 1
    try:
        yield
    finally:
        with _INFLIGHT_LOCK:
            left = _INFLIGHT.get(key, 1) - 1
            if left > 0:
                _INFLIGHT[key] = left
            else:
                _INFLIGHT.pop(key, None)


def commit(temp: Path, target: Path) -> None:
    """Swap a finished temp into place. ``os.replace`` replaces the directory entry — it
    never follows a symlink planted at ``target`` — and when two exports race for one name,
    the last to finish wins without deleting anyone's output."""
    os.replace(temp, target)


def discard(temp: Path) -> None:
    """Drop a temp this capture no longer wants. Never raises."""
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        log.exception("[agent_browser] discarding capture temp %s failed", temp)


def prune_captures(*, max_files: int | None = None, max_bytes: int | None = None,
                   keep: Path | None = None) -> int:
    """Drop the oldest captures past either budget, and sweep orphaned temps. Returns how
    many files were removed.

    Never deleted: ``keep`` (the capture the caller just reported as saved — a single file
    bigger than the whole budget used to prune ITSELF), any target with an export in flight
    (``in_flight``), and any temp younger than ``STALE_TEMP_S``. Live temps aren't counted
    against the budget either: they're someone's capture in progress. Best-effort and never
    raises. The budgets are read at CALL time so an operator fork — or a test — can retune.
    """
    max_files = MAX_CAPTURE_FILES if max_files is None else max_files
    max_bytes = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    now = time.time()
    try:
        root = capture_root()
        entries = [p for p in root.rglob("*") if p.is_file()]
        protected = {str(keep.resolve())} if keep is not None else set()
    except OSError:
        return 0
    with _INFLIGHT_LOCK:
        protected |= set(_INFLIGHT)
    removed = 0
    files: list[tuple[float, int, Path]] = []
    for p in entries:
        try:
            st = p.stat()
        except OSError:
            continue
        if is_temp(p):
            if now - st.st_mtime > STALE_TEMP_S:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
            continue
        files.append((st.st_mtime, st.st_size, p))
    files.sort(key=lambda f: f[0])
    count = len(files)
    total = sum(size for _, size, _ in files)
    for _, size, p in files:
        if count <= max_files and total <= max_bytes:
            break
        try:
            if str(p.resolve()) in protected:
                continue
            p.unlink()
            removed += 1
            count -= 1
            total -= size
        except OSError:
            pass
    if removed:
        log.info("[agent_browser] pruned %d old capture file(s)", removed)
    return removed


def resolve_capture_path(path: str | None, *, default_name: str) -> Path:
    """A capture path fenced to :func:`capture_root`, or ``ValueError`` if it escapes.

    * blank ``path`` → ``<root>/<default_name>``;
    * a relative path (``shots/home.png``) → under the root, parents created;
    * an absolute path → accepted ONLY if it already resolves inside the root (so an
      agent can re-use a path a previous call returned), refused otherwise.
    """
    root = capture_root().resolve()
    raw = str(path or "").strip() or default_name
    candidate = Path(raw).expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(
            f"refusing to write outside the plugin's capture directory: pass a name or a "
            f"relative path (e.g. {default_name!r}) — files land in {root}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
