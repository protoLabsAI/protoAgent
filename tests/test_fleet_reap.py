"""A crashed (SIGKILLed) child member must be detected as DOWN.

Regression for the zombie-liveness bug: a member spawned by this hub is our child;
when it's SIGKILLed it lingers as a zombie until reaped, and ``os.kill(pid, 0)``
reports a zombie as *alive*. That masked the crash from ``status()``/``is_running()``
and made ``start()`` no-op on the dead pid. ``_alive`` now reaps the zombie first.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from graph.fleet import supervisor


def _spawn_sleeper() -> subprocess.Popen:
    # A real child of THIS (pytest) process, so it becomes a zombie when killed until reaped.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_alive_true_for_running_child():
    p = _spawn_sleeper()
    try:
        assert supervisor._alive(p.pid) is True
    finally:
        p.kill()
        p.wait()


def test_alive_false_for_sigkilled_zombie_child():
    p = _spawn_sleeper()
    pid = p.pid
    os.kill(pid, signal.SIGKILL)

    # The kernel needs a beat to transition the process to a zombie. Until the parent
    # reaps it, a NAIVE os.kill(pid, 0) probe still succeeds — that's the bug.
    for _ in range(50):
        try:
            os.kill(pid, 0)  # still "alive" (zombie not yet reaped)
            time.sleep(0.02)
        except ProcessLookupError:
            break

    # _alive reaps the zombie and then reports it gone — the crash IS detected.
    assert supervisor._alive(pid) is False
    # Idempotent: a second call (pid already reaped / unknown) is still False, no raise.
    assert supervisor._alive(pid) is False


def test_reap_is_noop_for_non_child_pid():
    # PID 1 (init) is never our child → waitpid raises ECHILD, swallowed; no exception.
    supervisor._reap(1)


def test_alive_false_for_none_and_zero():
    assert supervisor._alive(None) is False
    assert supervisor._alive(0) is False


@pytest.mark.skipif(not hasattr(os, "waitpid"), reason="POSIX waitpid only")
def test_reap_does_not_steal_other_childs_status():
    """Targeted reap must not consume a DIFFERENT child's exit status (the SIGCHLD
    reaper footgun) — a concurrent child we still want to wait() on stays wait()-able."""
    other = _spawn_sleeper()
    victim = _spawn_sleeper()
    try:
        os.kill(victim.pid, signal.SIGKILL)
        supervisor._reap(victim.pid)  # reap ONLY the victim
        # `other` is untouched and still reapable by its own Popen handle.
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()
        try:
            victim.wait(timeout=2)
        except Exception:
            pass
