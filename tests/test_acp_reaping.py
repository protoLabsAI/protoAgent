"""Regression tests for ACP subprocess reaping — the orphan leak.

`delegate_to` / the health prober spawn CLI coding agents (codex-acp, claude-agent-acp)
over ACP. Two bugs leaked their processes until ~20 GB of `ppid 1` orphans piled up:

  1. teardown signalled only the DIRECT child, so the backend the adapter spawned
     reparented to init and survived (no process-group kill);
  2. `dispatch` awaited a POOLED client and never reaped it on cancel, so stopping the
     turn left the agent running ("I stopped the main thread and the delegate didn't
     stop").

These tests pin the fixes: a spawned grandchild dies with the group, the cancel path
hard-kills synchronously, and the shutdown hook drains the whole pool.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

import pytest

from infra.proc import group_kwargs, pid_alive
from plugins.coding_agent.acp_client import AcpClient


def _alive(pid: int) -> bool:
    """True if ``pid`` is a live (non-zombie) process — portable (ADR 0098)."""
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        return True  # no zombies on Windows — alive means alive
    # POSIX: exists, but a zombie/defunct is effectively dead — distinguish via ps.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


async def _wait_dead(*pids: int, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.1)):
        if not any(_alive(p) for p in pids):
            return
        await asyncio.sleep(0.1)


async def _spawn_group_with_grandchild() -> tuple[asyncio.subprocess.Process, int]:
    """A parent in its OWN process tree that spawns a grandchild sleeper (the
    stand-in for the adapter's backend), prints the grandchild pid, then waits."""
    code = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        "print(child.pid, flush=True); "
        "child.wait()"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **group_kwargs(),  # same anchoring _start() now uses (ADR 0098)
    )
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    return proc, int(line.decode().strip())


def _client_holding(proc: asyncio.subprocess.Process) -> AcpClient:
    client = AcpClient(sys.executable, ["-c", "pass"], cwd=tempfile.gettempdir(), name="reap-test")
    client._proc = proc  # bypass the ACP handshake — we only exercise teardown
    return client


@pytest.mark.asyncio
async def test_close_reaps_whole_process_group():
    """close() must kill the backend the agent spawned, not just the agent."""
    proc, grandchild = await _spawn_group_with_grandchild()
    assert _alive(proc.pid) and _alive(grandchild)

    await _client_holding(proc).close()

    await _wait_dead(proc.pid, grandchild)
    assert not _alive(grandchild), "grandchild leaked — process-group kill regressed"
    assert not _alive(proc.pid)


@pytest.mark.asyncio
async def test_kill_now_is_synchronous_and_reaps_group():
    """kill_now() is the cancel-path hard stop: no awaits, whole group dies."""
    proc, grandchild = await _spawn_group_with_grandchild()
    assert _alive(proc.pid) and _alive(grandchild)

    _client_holding(proc).kill_now()  # synchronous — no await

    await _wait_dead(proc.pid, grandchild)
    assert not _alive(grandchild), "grandchild survived kill_now — group SIGKILL regressed"
    assert not _alive(proc.pid)


@pytest.mark.asyncio
async def test_dispatch_hard_reaps_on_cancel(monkeypatch):
    """A cancelled dispatch must drop the pooled client AND kill its agent tree —
    otherwise the subprocess keeps running detached after the turn is stopped."""
    import plugins.coding_agent as ca
    from plugins.delegates.adapters import ADAPTERS

    killed = {"now": False}
    dropped: list = []

    class _FakeClient:
        _permission = None

        async def prompt(self, *a, **k):
            raise asyncio.CancelledError()

        def kill_now(self):
            killed["now"] = True

    fake = _FakeClient()
    monkeypatch.setattr(ca, "_client_for", lambda spec: fake)
    monkeypatch.setattr(ca, "_make_permission", lambda spec: None)
    monkeypatch.setattr(ca, "_drop_client", lambda spec: dropped.append(spec))

    d = ADAPTERS["acp"].parse({"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"})
    with pytest.raises(asyncio.CancelledError):
        await ADAPTERS["acp"].dispatch(d, "do a thing")

    assert killed["now"], "dispatch did not hard-kill the agent tree on cancel"
    assert dropped, "dispatch did not drop the dead client from the pool on cancel"


@pytest.mark.asyncio
async def test_close_all_drains_the_pool():
    """The shutdown hook reaps every pooled client and clears the cache."""
    import plugins.coding_agent as ca

    closed: list[str] = []

    class _FakeClient:
        def __init__(self, name):
            self.name = name

        async def close(self):
            closed.append(self.name)

    ca._CLIENTS.clear()
    ca._CLIENTS[("a",)] = _FakeClient("a")
    ca._CLIENTS[("b",)] = _FakeClient("b")

    assert await ca.close_all() is True
    assert sorted(closed) == ["a", "b"]
    assert ca._CLIENTS == {}
    assert await ca.close_all() is False  # idempotent
