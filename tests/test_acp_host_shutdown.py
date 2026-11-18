"""Our own shutdown is not a coder crash, and a working coder is not idle (#5267)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from infra import proc
from operator_api.runtime_routes import restart_readiness
from plugins.coding_agent import acp_client
from plugins.coding_agent.acp_client import AcpClient, AcpError

# Drives infra.proc's tree teardown, which branches on the OS: run it on the Windows lane.
pytestmark = pytest.mark.platform_sensitive


class _Proc:
    """An agent process whose stdout the test ends by hand; it has already exited 0."""

    pid = 2**22 + 5267
    returncode = 0
    stdin = None

    def __init__(self):
        self.stdout = asyncio.StreamReader()

    async def wait(self):
        return self.returncode


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(proc, "_TRACKED", {})  # teardown below must signal nothing real
    client = AcpClient("fake", [], cwd=str(tmp_path), name="sonnet", record_runs=False)
    client._session_id = "session-5267"
    client._stderr_tail.append("No onPostToolUseHook found")
    monkeypatch.setattr(client, "_send", AsyncMock())
    monkeypatch.setattr(client, "_cancel_session", AsyncMock())
    monkeypatch.setattr(client, "_close_session", AsyncMock())
    return client


async def _prompt_in_flight(client):
    client._proc = _Proc()
    client._reader_task = asyncio.ensure_future(client._read_loop(client._stream_gen))
    prompt = asyncio.ensure_future(client._prompt_request("work", timeout=30))
    while not client._pending:
        await asyncio.sleep(0)
    return prompt


async def _exit(client):
    client._proc.stdout.feed_eof()


async def _host_teardown(client):
    proc.begin_tree_teardown()
    client._proc.stdout.feed_eof()


@pytest.mark.parametrize(
    ("stop", "host_shutdown", "message"),
    [
        (_exit, False, "sonnet agent exited (exit code 0); last stderr: No onPostToolUseHook found"),
        (_host_teardown, True, "sonnet agent stopped: host shutting down"),
        (AcpClient.close, True, "sonnet agent stopped: host shutting down"),
    ],
)
async def test_pending_prompt_names_who_stopped_the_agent(client, stop, host_shutdown, message):
    prompt = await _prompt_in_flight(client)
    await stop(client)
    with pytest.raises(AcpError) as caught:
        await prompt
    assert caught.value.host_shutdown is host_shutdown
    assert caught.value.reported_by_agent is False
    if host_shutdown:
        assert str(caught.value) == message  # no exit code, no stderr noise, no turn evidence
    else:
        assert str(caught.value).startswith(message)
    assert acp_client.live_dispatch_count() == 0


async def test_readiness_is_busy_while_a_coder_dispatch_runs(client, monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "background_mgr", None)
    assert restart_readiness()["busy"] == []
    prompt = await _prompt_in_flight(client)
    readiness = restart_readiness()
    assert readiness["idle"] is False
    assert readiness["busy"] == ["1 coding-agent dispatch running"]
    prompt.cancel()
    await asyncio.gather(prompt, return_exceptions=True)
    assert restart_readiness()["idle"] is True

    # A registry that cannot be read is busy, never idle.
    monkeypatch.setattr(acp_client, "live_dispatch_count", lambda: 1 / 0)
    readiness = restart_readiness()
    assert readiness["idle"] is False
    assert readiness["busy"] == ["coding-agent dispatches could not be read"]
    await client.close()
