"""pid_alive (#1678) — the cross-platform process-liveness probe.

`os.kill(pid, 0)` is a POSIX idiom that is NOT a liveness probe on Windows: signal 0
is CTRL_C_EVENT, which fails with OSError against any non-console-group target — the
desktop's parent-death watchdog read its healthy GUI launcher as gone and self-killed
the sidecar ~2s after every Windows boot. These pin the corrected semantics on both
platforms (the Windows branch via an injected fake kernel32 — CI runs POSIX).
"""

from __future__ import annotations

import ctypes
import os

from infra import paths


# ── POSIX semantics ───────────────────────────────────────────────────────────────
def test_posix_running_process_is_alive():
    assert paths.pid_alive(os.getpid()) is True


def test_posix_only_process_lookup_error_means_gone(monkeypatch):
    def _kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _kill)
    assert paths.pid_alive(12345) is False


def test_posix_permission_error_means_alive(monkeypatch):
    """EPERM = the process EXISTS but isn't ours to signal — the old watchdog's bare
    `except OSError` misread this as dead."""

    def _kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", _kill)
    assert paths.pid_alive(12345) is True


def test_nonpositive_pids_are_dead():
    assert paths.pid_alive(0) is False
    assert paths.pid_alive(-1) is False


# ── Windows branch (fake kernel32) ────────────────────────────────────────────────
class _FakeKernel32:
    """OpenProcess/GetExitCodeProcess/GetLastError/CloseHandle, scriptable."""

    def __init__(self, *, handle=1, exit_code=259, exit_ok=True, last_error=0):
        self._handle = handle
        self._exit_code = exit_code
        self._exit_ok = exit_ok
        self._last_error = last_error
        self.closed = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 — win32 casing
        return self._handle

    def GetLastError(self):  # noqa: N802
        return self._last_error

    def GetExitCodeProcess(self, handle, code_ref):  # noqa: N802
        if not self._exit_ok:
            return 0
        # ctypes.byref proxies the c_ulong via _obj.
        code_ref._obj.value = self._exit_code
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return 1


def test_windows_still_active_process_is_alive():
    k32 = _FakeKernel32(exit_code=259)  # STILL_ACTIVE
    assert paths._pid_alive_windows(4242, kernel32=k32) is True
    assert k32.closed == [1]  # handle released


def test_windows_exited_process_is_dead():
    assert paths._pid_alive_windows(4242, kernel32=_FakeKernel32(exit_code=0)) is False


def test_windows_access_denied_means_alive():
    """OpenProcess failing with ERROR_ACCESS_DENIED means the process EXISTS."""
    k32 = _FakeKernel32(handle=0, last_error=5)
    assert paths._pid_alive_windows(4242, kernel32=k32) is True


def test_windows_invalid_pid_is_dead():
    k32 = _FakeKernel32(handle=0, last_error=87)  # ERROR_INVALID_PARAMETER
    assert paths._pid_alive_windows(4242, kernel32=k32) is False


def test_windows_unreadable_exit_code_leans_alive():
    """A queryable handle whose exit code can't be read must NOT read as dead —
    false 'dead' is the self-kill direction (#1678)."""
    assert paths._pid_alive_windows(4242, kernel32=_FakeKernel32(exit_ok=False)) is True


def test_byref_proxy_matches_ctypes_contract():
    """The fake's `_obj` poke mirrors real ctypes.byref — pin that assumption."""
    c = ctypes.c_ulong()
    ref = ctypes.byref(c)
    ref._obj.value = 259
    assert c.value == 259
