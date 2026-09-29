"""Tests for tools.shell.run_command."""

import ctypes
import os
import sys
import time

import pytest

from tools.shell import run_command

pytestmark = pytest.mark.platform_sensitive


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


@pytest.mark.asyncio
async def test_raw_command_line_is_windows_only():
    """A ``str`` argv is a Windows command line (#3802); elsewhere it is refused, not run."""
    if os.name == "nt":
        pytest.skip("POSIX-only guard")
    res = await run_command("echo hi")
    assert not res.ok and "Windows-only" in (res.error or "")


@pytest.mark.asyncio
async def test_spawn_command_line_yields_a_normal_process():
    """The raw-command-line spawn builds the same transport + ``Process`` as
    ``create_subprocess_exec``. A bare program path is a valid command line on every
    platform, so the plumbing (pipes, communicate, returncode) is exercised off Windows too."""
    import asyncio

    from tools.shell import _spawn_command_line

    proc = await _spawn_command_line(
        sys.executable,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate(b"print(6 * 7)\n")
    assert proc.returncode == 0
    assert out.decode().strip() == "42"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="cmd.exe quoting is Windows-only")
async def test_cmd_runs_a_quoted_interpreter_path(tmp_path):
    """#3802: `"C:\\...\\py.exe" -c "print(42)"` under cmd.exe. The spaced directory forces
    the quotes to matter; before the fix cmd saw `\\"C:\\...\\"` and failed to find it."""
    import shutil

    from tools.shell import cmd_command_line

    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    exe = spaced / os.path.basename(sys.executable)
    shutil.copy2(sys.executable, exe)
    # Make the copy runnable: its DLLs beside it, and either the venv's pyvenv.cfg (a venv
    # python.exe is a launcher that finds its base via that file) or PYTHONHOME (a base
    # install's python.exe locates the stdlib relative to itself, which the copy can't).
    base = os.path.dirname(sys.executable)
    for name in os.listdir(base):
        if name.lower().endswith(".dll"):
            shutil.copy2(os.path.join(base, name), spaced / name)
    env = {}
    cfg = os.path.join(sys.prefix, "pyvenv.cfg")
    if sys.prefix != sys.base_prefix and os.path.exists(cfg):
        shutil.copy2(cfg, spaced / "pyvenv.cfg")
    else:
        env["PYTHONHOME"] = sys.base_prefix
    res = await run_command(cmd_command_line(f'"{exe}" -c "print(42)"'), timeout=60, env=env)
    assert res.ok, (res.returncode, res.stdout, res.stderr, res.error)
    assert res.stdout == "42"


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
