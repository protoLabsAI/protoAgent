"""Tests for tools.shell.run_command."""

import pytest

from tools.shell import run_command


@pytest.mark.asyncio
async def test_success_and_stdout():
    res = await run_command(["echo", "hello world"])
    assert res.ok
    assert res.stdout == "hello world"
    assert res.returncode == 0


@pytest.mark.asyncio
async def test_nonzero_exit_not_ok():
    res = await run_command(["sh", "-c", "echo oops >&2; exit 3"])
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
    res = await run_command(["sleep", "5"], timeout=0.2)
    assert res.timed_out is True
    assert not res.ok
    assert "timed out" in (res.error or "")


@pytest.mark.asyncio
async def test_stdin_and_env_merge(monkeypatch):
    res = await run_command(["cat"], stdin="piped input")
    assert res.stdout == "piped input"
    res2 = await run_command(["sh", "-c", "echo $MY_VAR"], env={"MY_VAR": "merged"})
    assert res2.stdout == "merged"
