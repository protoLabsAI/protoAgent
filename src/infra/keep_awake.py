"""Best-effort, process-wide sleep inhibition for scheduled desktop runs.

Two independent holds share this module. ``hold(reason)`` is the ref-counted,
per-fire assertion used by the scheduler. ``start_serving_hold()`` /
``stop_serving_hold()`` is a process-lifetime assertion so a plugged-in hub
stays reachable by paired phones. They keep separate state and never release
each other.
"""

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
# Serving hold: its own release handle, guarded by the shared _lock but kept
# wholly separate from _users/_release so the two holds never touch each other.
_serving_release = None
_serving_warned = False


def _log_failure(exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.info("Scheduled-run keep-awake unavailable: %s", exc)


def _log_serving_unsupported() -> None:
    global _serving_warned
    if not _serving_warned:
        _serving_warned = True
        log.info("Serving keep-awake is unsupported on %s", sys.platform)


def _terminate(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


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
        # EXECUTION_STATE is a DWORD, including the high ES_CONTINUOUS bit.
        set_state.argtypes = [ctypes.c_uint32]
        set_state.restype = ctypes.c_uint32
        # The return value is the PREVIOUS state, and NULL means failure, but a
        # thread with no prior state can also return 0 on success. A zero return is
        # ambiguous, so don't treat it as failure: keep-awake is best-effort, and
        # refusing here would silently drop the assertion on a fresh thread.
        set_state(0x80000000 | 0x00000001)

        def release():
            set_state(0x80000000)

        return release
    if sys.platform == "darwin":
        argv = ["caffeinate", "-i", "-w", str(os.getpid())]
    elif sys.platform.startswith("linux"):
        argv = ["systemd-inhibit", "--what=sleep", f"--why={reason}", "--mode=block", "sleep", "infinity"]
    else:
        return None
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def release():
        _terminate(process)

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


def start_serving_hold() -> bool:
    """Hold a process-lifetime sleep assertion so a plugged-in hub stays reachable.

    Separate from ``hold()``'s ref-counted scheduled-run assertion: the two keep
    distinct state and never release each other. Idempotent (a second start while
    one is held returns True without spawning another process), best-effort, and
    never raises — a missing binary or spawn error is logged once and returns
    False. The caller opts in explicitly, so PROTOAGENT_PARENT_PID is not required;
    only an explicit PROTOAGENT_KEEP_AWAKE=0 disables it.
    """
    global _serving_release
    if os.environ.get("PROTOAGENT_KEEP_AWAKE") == "0":
        return False
    if sys.platform == "darwin":
        # -s inhibits system sleep only on AC power (a no-op on battery, which is
        # intentional); -w makes caffeinate exit if this server dies.
        argv = ["caffeinate", "-s", "-w", str(os.getpid())]
    elif sys.platform.startswith("linux"):
        argv = ["systemd-inhibit", "--what=sleep", "--why=protoAgent serving", "--mode=block", "sleep", "infinity"]
    else:
        # SetThreadExecutionState is per-thread, so a lifetime hold needs its own
        # design; out of scope here.
        _log_serving_unsupported()
        return False
    with _lock:
        if _serving_release is not None:
            return True
        try:
            process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            _log_failure(exc)
            return False

        def release():
            _terminate(process)

        _serving_release = release
    return True


def stop_serving_hold() -> None:
    """Release the serving hold if one is held; a no-op otherwise.

    Terminates the process with the same terminate/wait/kill pattern the
    scheduled hold uses, and never raises.
    """
    global _serving_release
    with _lock:
        release, _serving_release = _serving_release, None
    if release is not None:
        try:
            release()
        except Exception as exc:
            _log_failure(exc)
