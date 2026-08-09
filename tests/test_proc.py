"""Tests for infra.proc — the cross-platform process-tree contract (ADR 0098).

Portable by design: these run on the Windows CI gate too, and are the
acceptance tests for the #2412 phase-2 burndown (each subsystem migrated onto
infra.proc gets its POSIX-only lifecycle tests rewritten against this module).
"""

import asyncio
import os
import subprocess
import sys
import time

import pytest

from infra.proc import (
    akill_tree,
    detached_kwargs,
    group_kwargs,
    kill_tree,
    pid_alive,
    terminate_tree,
)

_IS_WINDOWS = os.name == "nt"

# A child that spawns a grandchild, records its PID, then sleeps — the shape
# every tree-kill bug involves (kill the child, orphan the grandchild).
_PARENT_CODE = (
    "import pathlib, subprocess, sys, time; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
    "time.sleep(30)"
)


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def _spawn_tree(tmp_path, kwargs):
    pid_file = tmp_path / "child.pid"
    proc = subprocess.Popen(
        [sys.executable, "-c", _PARENT_CODE, str(pid_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "parent never spawned its child"
    return proc, int(pid_file.read_text())


# ── spawn-kwargs shape ───────────────────────────────────────────────────────


def test_group_kwargs_shape():
    kw = group_kwargs()
    if _IS_WINDOWS:
        assert kw == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kw == {"start_new_session": True}


def test_detached_kwargs_shape():
    kw = detached_kwargs()
    if _IS_WINDOWS:
        assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert kw == {"start_new_session": True}


# ── liveness ─────────────────────────────────────────────────────────────────


def test_pid_alive_on_live_and_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert _wait_gone(proc.pid)
    assert not pid_alive(-1)
    assert not pid_alive(0)


# ── sync tree kill ───────────────────────────────────────────────────────────


def test_kill_tree_takes_down_grandchild(tmp_path):
    proc, grandchild = _spawn_tree(tmp_path, group_kwargs())
    kill_tree(proc.pid)
    proc.wait(timeout=10)
    assert _wait_gone(grandchild), "tree kill left the grandchild running"


def test_terminate_tree_graceful_then_gone(tmp_path):
    proc, grandchild = _spawn_tree(tmp_path, group_kwargs())
    assert terminate_tree(proc.pid, grace=3.0)
    proc.wait(timeout=10)
    assert _wait_gone(grandchild), "terminate_tree left the grandchild running"


def test_kill_tree_on_dead_pid_never_raises():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    kill_tree(proc.pid)  # must be a no-op, not an exception
    assert terminate_tree(proc.pid, grace=0.2)


# ── async tree kill (the shell-tool path) ────────────────────────────────────


@pytest.mark.asyncio
async def test_akill_tree_takes_down_grandchild(tmp_path):
    pid_file = tmp_path / "child.pid"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PARENT_CODE,
        str(pid_file),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **group_kwargs(),
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "parent never spawned its child"
    grandchild = int(pid_file.read_text())

    await akill_tree(proc)
    await proc.wait()
    assert _wait_gone(grandchild), "akill_tree left the grandchild running"
