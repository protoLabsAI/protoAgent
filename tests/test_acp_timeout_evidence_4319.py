"""Bounded current-turn evidence survives ACP timeouts and transport abandonment."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from plugins.coding_agent.acp_client import AcpClient, AcpError


@pytest.fixture
def client(monkeypatch, tmp_path):
    client = AcpClient("fake", [], cwd=str(tmp_path), record_runs=False)
    client._session_id = "session-4319"
    monkeypatch.setattr(client, "_ensure_started", AsyncMock())
    monkeypatch.setattr(client, "_cancel_session", AsyncMock())
    return client


async def run(client):
    return await client._prompt_locked(
        "work", progress_callback=None, tool_callback=None, text_callback=None,
        thought_callback=None, timeout=0.01,
    )


async def update(client, **fields):
    await client._handle_update({"sessionId": "session-4319", "update": fields})


async def test_timeout_has_bounded_redacted_evidence(client, monkeypatch):
    client._note_transcript({"_meta": {"transcriptPath": "/logs/session-4319.jsonl"}})

    async def send(_message):
        await update(client, sessionUpdate="tool_call", toolCallId="edit", title="Edit",
                     kind="edit", rawInput={"content": "NEVER DUMP FILE CONTENT"})
        await update(client, sessionUpdate="tool_call_update", toolCallId="edit",
                     locations=[{"path": f"src/file-{i}.py"} for i in range(20)],
                     content=[{"type": "diff", "path": "other.py", "newText": "PRIVATE CONTENT"}])
        client._stderr_tail.extend(["No onPostToolUseHook found"] * 50)
        client._stderr_tail.append("\x1b[31mAPI_KEY=secret-value\x1b[0m")
        client._stderr_tail.append("noise" * 1000)

    monkeypatch.setattr(client, "_send", send)
    with pytest.raises(AcpError) as caught:
        await run(client)
    error = str(caught.value)
    assert "session/prompt timed out" in error
    assert "session_id=session-4319" in error
    assert "transcript=/logs/session-4319.jsonl" in error
    assert "last_tool=Edit (age " in error
    assert "pending_permission=no" in error
    assert "observed_edit_paths (unverified)=" in error
    assert "src/file-0.py" in error and "src/file-7.py" in error
    assert "src/file-8.py" not in error
    assert "additional paths omitted" in error
    assert "secret-value" not in error and "\x1b" not in error
    assert "PRIVATE CONTENT" not in error and "NEVER DUMP" not in error
    assert len(error) < 5000
    assert not caught.value.reported_by_agent
    assert client._stream_fenced
    client._cancel_session.assert_awaited_once()


async def test_evidence_resets_and_read_paths_are_not_edits(client, monkeypatch):
    calls = 0

    async def send(_message):
        nonlocal calls
        calls += 1
        if calls == 1:
            await update(client, sessionUpdate="tool_call", toolCallId="old", title="Write",
                         kind="edit", locations=[{"path": "previous.py"}])
        else:
            await update(client, sessionUpdate="tool_call", toolCallId="read", title="Read",
                         kind="read", locations=[{"path": "only-read.py"}])

    monkeypatch.setattr(client, "_send", send)
    for _ in range(2):
        with pytest.raises(AcpError) as caught:
            await run(client)
    error = str(caught.value)
    assert "previous.py" not in error and "only-read.py" not in error
    assert "observed_edit_paths (unverified)=[]" in error
    assert "last_tool=Read" in error
    assert "transcript=unavailable" in error


async def test_pending_permission_until_response_sent(client, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(client, "_respond", respond)
    request = asyncio.create_task(client._handle_request({
        "id": 1, "method": "session/request_permission",
        "params": {"options": [{"kind": "allow_once", "optionId": "yes"}]},
    }))
    try:
        await entered.wait()
        assert "pending_permission=yes" in client._timeout_evidence()
        release.set()
        await request
        assert "pending_permission=no" in client._timeout_evidence()
    finally:
        request.cancel()


@pytest.mark.parametrize("reported", [False, True])
async def test_abandon_preserves_exception_contract(client, monkeypatch, reported):
    original = AcpError("transport gone", code=42, data={"reason": "gone"}, reported_by_agent=reported)

    async def request(*args, **kwargs):
        await update(client, sessionUpdate="tool_call", toolCallId="bash",
                     title='Bash: echo SECRET_ARGUMENT', rawInput={"command": "SECRET_ARGUMENT"})
        raise original

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(AcpError) as caught:
        await run(client)
    assert caught.value is original
    assert caught.value.code == 42 and caught.value.data == {"reason": "gone"}
    if reported:
        assert str(original) == "transport gone"
        assert not client._stream_fenced
    else:
        assert "session_id=session-4319" in str(original)
        assert "last_tool=Bash" in str(original)
        assert "SECRET_ARGUMENT" not in str(original)
        assert client._stream_fenced


async def test_unknown_tool_and_external_cancellation_contract(client, monkeypatch):
    monkeypatch.setattr(client, "_send", AsyncMock())
    with pytest.raises(AcpError) as caught:
        await run(client)
    assert "last_tool=unknown (age unknown)" in str(caught.value)
    assert "transcript=unavailable" in str(caught.value)
    monkeypatch.setattr(client, "_request", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await run(client)


async def test_replayed_tools_ignored_and_transcript_cleared(client):
    client._loading = True
    await update(client, sessionUpdate="tool_call", title="Edit", kind="edit",
                 locations=[{"path": "history.py"}])
    assert client._last_tool is None
    assert client._observed_edit_paths == []
    client._note_transcript({"_meta": {"transcriptPath": "/logs/old.jsonl"}})
    client._note_transcript(None)
    assert "transcript=unavailable" in client._timeout_evidence()


async def test_timeout_snapshots_pending_permission_and_tool_age(client, monkeypatch):
    import time

    entered = asyncio.Event()
    release = asyncio.Event()
    permission_task = None

    async def respond(*args):
        entered.set()
        await release.wait()

    async def send(_message):
        nonlocal permission_task
        await update(client, sessionUpdate="tool_call", toolCallId="write",
                     title="Write", kind="edit", locations=[{"path": "attempt.py"}],
                     status="failed")
        client._last_tool = ("Write", time.monotonic() - 12)
        permission_task = asyncio.create_task(client._handle_request({
            "id": 9, "method": "session/request_permission", "params": {},
        }))
        await entered.wait()

    monkeypatch.setattr(client, "_respond", respond)
    monkeypatch.setattr(client, "_send", send)
    try:
        with pytest.raises(AcpError) as caught:
            await run(client)
        error = str(caught.value)
        assert "pending_permission=yes" in error
        assert "last_tool=Write (age 12." in error
        assert 'observed_edit_paths (unverified)=["attempt.py"]' in error
    finally:
        release.set()
        if permission_task is not None:
            await permission_task


@pytest.mark.parametrize("load", [False, True])
async def test_transcript_metadata_bound_to_new_or_loaded_session(client, monkeypatch, load):
    monkeypatch.setattr(client, "_request", AsyncMock(return_value={
        "sessionId": "new-session", "_meta": {"transcriptPath": "/agent/reported.jsonl"},
    }))
    monkeypatch.setattr(client, "_persist_session_id", lambda _: None)
    if load:
        await client._load_session("resumed-session")
    else:
        await client._new_session()
    assert client._transcript_path == "/agent/reported.jsonl"
    monkeypatch.setattr(client, "_request", AsyncMock(return_value={"sessionId": "next"}))
    await client._new_session()
    assert client._transcript_path is None


def test_sanitizes_before_cutting_and_keeps_stderr_tail(client):
    client._stderr_tail.append("noise" * 2000 + " \x1b[31mAPI_KEY=secret-value\x1b[0m")
    evidence = client._timeout_evidence()
    assert "secret-value" not in evidence
    assert "API_KEY=[REDACTED]" in evidence
    assert len(evidence) < 2500
    assert client._evidence_text("API_KEY=" + "a" * 4000, 50) == "API_KEY=[REDACTED]"


async def test_start_failure_does_not_report_previous_turn_transcript(client, monkeypatch):
    client._turn_session_id = "previous"
    client._note_transcript({"_meta": {"transcriptPath": "/logs/previous.jsonl"}})
    client._last_tool = ("PreviousTool", 1.0)

    async def start(**kwargs):
        raise AcpError("start failed" + client._timeout_evidence())

    monkeypatch.setattr(client, "_ensure_started", start)
    with pytest.raises(AcpError) as caught:
        await run(client)
    assert "session_id=unknown" in str(caught.value)
    assert "transcript=unavailable" in str(caught.value)
    assert "previous" not in str(caught.value)
    assert "last_tool=unknown" in str(caught.value)
