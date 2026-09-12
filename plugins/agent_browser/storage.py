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

import logging
import secrets
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


def set_aside(target: Path) -> Path | None:
    """Move an existing capture out of the way before a run that will write ``target``.

    A re-export to an existing name (the resume flow re-exports ``resume.pdf``) could
    otherwise "succeed" while writing nothing: the OLD file is still there and non-empty,
    so no size check can tell it from a new one. With the old file moved aside, the target
    has to be re-created by THIS run to count. Returns the parked path, or None.
    """
    if not target.is_file():
        return None
    parked = target.with_name(f".{target.name}.{secrets.token_hex(4)}.prev")
    target.replace(parked)
    return parked


def settle(target: Path, parked: Path | None, *, keep_new: bool) -> None:
    """Finish a capture started with :func:`set_aside`.

    Success: keep the new file, drop the parked one. Failure: discard whatever this run
    left at ``target`` (a partial or empty file) and put the previous capture back, so a
    failed re-export never destroys the last good one. Never raises."""
    try:
        if keep_new:
            if parked is not None:
                parked.unlink(missing_ok=True)
            return
        target.unlink(missing_ok=True)
        if parked is not None:
            parked.replace(target)
    except OSError:
        log.exception("[agent_browser] settling capture %s failed", target)


def prune_captures(*, max_files: int | None = None, max_bytes: int | None = None,
                   keep: Path | None = None) -> int:
    """Drop the oldest captures past either budget. Returns how many were removed.

    ``keep`` is never deleted: it is the capture the caller just reported as saved, and a
    single file bigger than the whole budget used to prune ITSELF while the tool still said
    "Saved to". Best-effort and never raises: losing a disposable screenshot must not fail
    the tool call that just succeeded. The budgets are read from the module at CALL time
    (not bound as defaults) so an operator fork — or a test — can retune them.
    """
    max_files = MAX_CAPTURE_FILES if max_files is None else max_files
    max_bytes = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    try:
        root = capture_root()
        files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
        protected = keep.resolve() if keep is not None else None
    except OSError:
        return 0
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    removed = 0
    for p in files:
        if len(files) - removed <= max_files and total <= max_bytes:
            break
        try:
            if protected is not None and p.resolve() == protected:
                continue
            total -= p.stat().st_size
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("[agent_browser] pruned %d old capture(s)", removed)
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
