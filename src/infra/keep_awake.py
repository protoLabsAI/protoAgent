"""Best-effort, process-wide sleep inhibition for scheduled desktop runs."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)
_lock = threading.Lock()
_users = 0
_release = None
_warned = False


def _log_failure(exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.info("Scheduled-run keep-awake unavailable: %s", exc)


def _enabled() -> bool:
    value = os.environ.get("PROTOAGENT_KEEP_AWAKE")
    if value is not None:
        return value != "0"
    return bool(os.environ.get("PROTOAGENT_PARENT_PID"))


def _acquire(reason: str):
    if sys.platform == "win32":
        # Execution state belongs to the calling thread. Scheduler fires and
        # their finally blocks run on the same asyncio event-loop thread.
        set_state = ctypes.windll.kernel32.SetThreadExecutionState
        if not set_state(0x80000000 | 0x00000001):
            raise OSError("SetThreadExecutionState failed")

        def release():
            if not set_state(0x80000000):
                raise OSError("SetThreadExecutionState reset failed")

        return release
    if sys.platform == "darwin":
        argv = ["caffeinate", "-i", "-w", str(os.getpid())]
    elif sys.platform.startswith("linux"):
        argv = ["systemd-inhibit", "--what=sleep", f"--why={reason}", "--mode=block", "sleep", "infinity"]
    else:
        return None
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def release():
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    return release


@contextmanager
def hold(reason: str):
    """Share one platform assertion until the last scheduled fire exits.

    Backend errors never interrupt a fire; report only the first failure.
    """
    global _users, _release
    if not _enabled():
        yield
        return
    with _lock:
        if _users == 0:
            try:
                _release = _acquire(reason)
            except Exception as exc:
                _log_failure(exc)
        _users += 1
    try:
        yield
    finally:
        with _lock:
            _users -= 1
            if _users == 0:
                release, _release = _release, None
                if release is not None:
                    try:
                        release()
                    except Exception as exc:
                        _log_failure(exc)
