"""Tests for tools.shell.run_command."""

import ctypes
import os
import sys
import time

import pytest

from tools.shell import run_command


@pytest.mark.asyncio
async def test_success_and_stdout():
    res = await run_command([sys.executable, "-c", "print('hello world')"])
    assert res.ok
    assert res.stdout == "hello world"
    assert res.returncode == 0


@pytest.mark.asyncio
async def test_nonzero_exit_not_ok():
    res = await run_command([sys.executable, "-c", "import sys; print('oops', file=sys.stderr); sys.exit(3)"])
    assert not res.ok
    assert res.returncode == 3
    assert "oops" in res.stderr
    assert res.error is None  # it ran, just failed


@pytest.mark.asyncio
async def test_missing_binary_structured_error():
    res = await run_command(["definitely-not-a-real-binary-xyz"])
    assert not res.ok
    assert res.error is not None and "not installed" in res.error  # no raise


@pytest.mark.asyncio
async def test_timeout_kills_process():
    res = await run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert res.timed_out is True
    assert not res.ok
    assert "timed out" in (res.error or "")


@pytest.mark.asyncio
async def test_stdin_and_env_merge(monkeypatch):
    res = await run_command([sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin="piped input")
    assert res.stdout == "piped input"
    res2 = await run_command([sys.executable, "-c", "import os; print(os.environ['MY_VAR'])"], env={"MY_VAR": "merged"})
    assert res2.stdout == "merged"


def _windows_pid_is_running(pid: int) -> bool:
    synchronize = 0x00100000
    wait_timeout = 0x00000102
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill process-tree regression")
@pytest.mark.asyncio
async def test_timeout_kills_windows_child_process(tmp_path):
    pid_file = tmp_path / "child.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    res = await run_command([sys.executable, "-c", parent_code, str(pid_file)], timeout=1.0)

    assert res.timed_out is True
    assert pid_file.exists(), "parent did not spawn its child before the timeout"
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while _windows_pid_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _windows_pid_is_running(child_pid), "timed-out command left its child running"
