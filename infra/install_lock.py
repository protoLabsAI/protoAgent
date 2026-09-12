"""One package install at a time per Python environment.

Two ``pip install`` runs writing the same site-packages at once can leave it half
written: a dist-info from one run beside files the other replaced, a RECORD that
names files that aren't there. The refresh that follows an install re-imports a
plugin, so it can read that state mid-write. Nothing stopped this. Two clicks on
**Install deps**, two tabs, two plugins' installs, or the CLI beside the console
each ran its own pip into the same environment.

:func:`install_lock` holds an environment for the length of one install. It is
**non-blocking**: a second caller gets :class:`InstallBusy` at once, naming what
holds the environment, rather than queueing behind a pip run that can take
minutes. It is held in two layers, because either one alone misses a case:

* a per-process table, keyed by the environment's resolved :class:`~pathlib.Path`,
  for two request threads in one server;
* an OS file lock (``flock`` on POSIX, an ``msvcrt`` byte-range lock on Windows)
  on a sidecar under the box root's ``locks/`` directory, for two processes that
  share one environment. The default and dev instances run from one checkout's
  venv; every desktop fleet member shares the managed Python runtime; and
  ``protoagent plugin install-deps`` can run beside a live server.

It is reentrant on the owning thread, so an outer hold makes an inner one a no-op.
The install-deps route holds the lock across install and refresh, and
``install_deps`` also takes it for itself.

Sidecars are never deleted: unlinking a lock file that another process has open, or
is about to open, splits the lock in two.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Windows byte-range locks are mandatory: a locked byte can't be read through another
# handle. The lock sits one byte far past EOF, which Windows allows, so a refused
# process can still read the holder note at the head of the file.
_WIN_LOCK_OFFSET = 1 << 20
_WIN_LOCK_BUSY = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
_POSIX_LOCK_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}


class InstallBusy(RuntimeError):
    """An install into this environment is already running, in this process or another."""

    def __init__(self, label: str, holder: dict | None) -> None:
        self.label = label
        self.holder = dict(holder or {})
        bits: list[str] = []
        if self.holder.get("what"):
            bits.append(str(self.holder["what"]))
        pid = self.holder.get("pid")
        if isinstance(pid, int) and pid != os.getpid():
            bits.append(f"pid {pid}")
        since = self.holder.get("since")
        if isinstance(since, (int, float)):
            bits.append(f"started {max(0, int(time.time() - since))}s ago")
        who = f" ({', '.join(bits)})" if bits else ""
        super().__init__(f"an install into {label} is already running{who}. Wait for it to finish, then retry.")


@dataclass
class _Hold:
    owner: int  # thread ident: only the owning thread may re-enter
    info: dict
    fd: int | None  # the OS lock's fd, or None where the filesystem can't lock
    depth: int = 1


_GUARD = threading.Lock()
_HOLDS: dict[Path, _Hold] = {}
_unlockable_warned = False


def env_key(env: Path) -> Path:
    """The identity of an environment: its resolved path. Paths, never strings: Path
    equality is case-insensitive on Windows, and resolving follows a symlinked venv."""
    try:
        return Path(env).resolve()
    except OSError:
        return Path(os.path.abspath(env))


def lock_file_for(env: Path) -> Path:
    """The sidecar that serialises installs into ``env`` across processes. It lives under
    the box root, which every instance on this machine shares, never inside the
    environment: a venv can be read-only, and a runtime's ``current/`` directory is
    swapped out whole on reprovision. The name is a hash of the normcased path, so
    spellings of one environment that differ only in case share one lock on Windows."""
    from infra.paths import instance_paths

    digest = hashlib.sha256(os.path.normcase(str(env_key(env))).encode("utf-8")).hexdigest()[:16]
    return instance_paths().box_root / "locks" / f"install-{digest}.lock"


def _try_os_lock(fd: int) -> bool:
    """Take the exclusive OS lock without waiting. True when held, False when another
    process holds it; any other failure (a filesystem without locking) propagates."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in _WIN_LOCK_BUSY:
                return False
            raise
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in _POSIX_LOCK_BUSY:
            return False
        raise


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_holder(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _acquire_os_lock(path: Path, info: dict, label: str) -> int | None:
    """The held fd. Raises :class:`InstallBusy` when another process holds the lock.
    Returns ``None`` when the filesystem refuses locking (a network mount without lock
    support, say). That degrades to the per-process table, with one warning, rather than
    refusing every install."""
    global _unlockable_warned
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        held = _try_os_lock(fd)
    except OSError:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not _unlockable_warned:
            _unlockable_warned = True
            log.warning(
                "[install] can't take the cross-process install lock at %s; installs are "
                "serialised within this process only",
                path,
                exc_info=True,
            )
        return None
    if not held:
        os.close(fd)
        raise InstallBusy(label, _read_holder(path))
    # Record who holds it, for the message a refused process shows. Best-effort.
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode("utf-8"))
    return fd


def _release_os_lock(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)  # the holder note goes with the hold
    try:
        _os_unlock(fd)
    except OSError:
        log.debug("[install] unlock failed (closing the fd releases it)", exc_info=True)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def install_lock(env: Path, *, label: str, what: str = "") -> Iterator[dict]:
    """Hold ``env`` for one install, or raise :class:`InstallBusy` at once when an install
    into it is already running. ``label`` names the environment in that message ("this
    server's Python environment"); ``what`` names this install (a plugin id). Yields the
    holder note ``{what, pid, since}``."""
    key = env_key(env)
    me = threading.get_ident()
    with _GUARD:
        hold = _HOLDS.get(key)
        if hold is not None:
            if hold.owner != me:
                raise InstallBusy(label, hold.info)
            hold.depth += 1
        else:
            info = {"what": what, "pid": os.getpid(), "since": time.time()}
            fd = _acquire_os_lock(lock_file_for(key), info, label)
            hold = _Hold(owner=me, info=info, fd=fd)
            _HOLDS[key] = hold
    try:
        yield dict(hold.info)
    finally:
        with _GUARD:
            hold.depth -= 1
            if hold.depth == 0:
                _HOLDS.pop(key, None)
                if hold.fd is not None:
                    _release_os_lock(hold.fd)


def holder(env: Path) -> dict | None:
    """The note of the install holding ``env`` in THIS process, or None. It deliberately
    doesn't probe other processes, because a probe is itself an acquisition and would
    refuse a real install racing it."""
    with _GUARD:
        hold = _HOLDS.get(env_key(env))
        return dict(hold.info) if hold is not None else None
