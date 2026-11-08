"""What build is THIS process actually running (#4876).

"Which version are you on?" was answered wrong: an agent reached for a local
checkout's ``git describe``, which describes a working tree on disk — not the
process that is serving. ``/healthz`` already reports the right ``version`` (and a
monotonic uptime), but nothing pointed the agent at it.

This captures the running build ONCE, at import (≈ process start) — the same
posture as ``operator_api.chat_routes._PROCESS_STARTED_AT``, which this cannot
import (``infra`` must never reach up into ``operator_api``), so it keeps its own
monotonic anchor with the same semantics. ``version`` comes from the one resolver
every surface shares (:func:`infra.paths.package_version`), so it can never
disagree with the ``/healthz`` value. Git identity is read only from a source
checkout, with a short timeout; any failure degrades to ``"unknown"``/``None`` and
never raises — a status field is never worth taking a turn down for.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from infra.paths import app_root, package_version

# Captured once, at import — the closest this module gets to process start, and the
# same monotonic-anchor semantics /healthz uses. uptime is derived from the monotonic
# clock (immune to wall-clock steps); started_at is the wall time, for a human-readable
# "since when".
_STARTED_MONOTONIC = time.monotonic()
_STARTED_AT_ISO = datetime.now(timezone.utc).isoformat()

# A git call that answers a status field must stay cheap and never hang the caller.
_GIT_TIMEOUT_S = 2.0


def _uptime_s() -> int:
    """Seconds this process has been up — monotonic, so a wall-clock adjustment can't
    make it jump or go negative. Computed fresh on every read."""
    return max(0, int(time.monotonic() - _STARTED_MONOTONIC))


def _install_kind(root: Path) -> str:
    """How this build is installed: ``"desktop"`` (a PyInstaller-frozen sidecar),
    ``"pypi"`` (a wheel under site-/dist-packages), or ``"source"`` (a checkout or
    ``COPY .`` image, where the app root still ships its own ``pyproject.toml``).

    Mirrors the signals ``infra.paths`` already trusts: ``sys.frozen`` for the frozen
    desktop sidecar and the ``src``-layout app root for a checkout. Anything that is
    neither frozen, nor site-packages, nor a pyproject-bearing root is reported as
    ``"pypi"`` — the installed default, never a guess that would mislead."""
    if getattr(sys, "frozen", False):
        return "desktop"
    parts = {p.lower() for p in root.parts}
    if "site-packages" in parts or "dist-packages" in parts:
        return "pypi"
    if (root / "pyproject.toml").is_file():
        return "source"
    return "pypi"


def _git(root: Path, *args: str) -> str | None:
    """Run ``git`` inside ``root`` and return its stripped stdout, or ``None`` on ANY
    failure — git absent, not a repo, non-zero exit, or the short timeout. Never raises:
    this feeds a status field, so a broken git must degrade, not propagate."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_sha_dirty(root: Path) -> tuple[str, bool | None]:
    """``(short_sha, dirty)`` for the checkout at ``root``. When git can't answer (not a
    checkout, git missing, timeout) the sha is ``"unknown"`` and dirty is ``None`` — the
    honest "we don't know", never a fabricated clean/dirty."""
    sha = _git(root, "rev-parse", "--short", "HEAD")
    if sha is None or not sha:
        return "unknown", None
    status = _git(root, "status", "--porcelain")
    dirty = bool(status) if status is not None else None
    return sha, dirty


def _collect() -> dict:
    """The static half of the build identity, captured once. ``git_sha``/``git_dirty``
    are read only from a source checkout — a wheel/desktop install has no repo to
    describe, so they stay ``"unknown"``/``None`` rather than shelling out pointlessly."""
    root = app_root()
    kind = _install_kind(root)
    if kind == "source":
        sha, dirty = _git_sha_dirty(root)
    else:
        sha, dirty = "unknown", None
    return {
        "version": package_version(),
        "git_sha": sha,
        "git_dirty": dirty,
        "install_kind": kind,
        "install_path": str(root),
        "started_at": _STARTED_AT_ISO,
    }


# Captured once at import, mirroring _PROCESS_STARTED_AT's import-time anchor.
_STATIC_BUILD_INFO: dict = _collect()


def build_info() -> dict:
    """The RUNNING build — what to answer "which version are you on?" with.

    The static fields (``version`` matching ``/healthz``, git ``sha``/``dirty`` when
    running from a source checkout, ``install_kind``/``install_path``, ``started_at``)
    are the snapshot captured at import; ``uptime_s`` is recomputed on each read."""
    return {**_STATIC_BUILD_INFO, "uptime_s": _uptime_s()}
