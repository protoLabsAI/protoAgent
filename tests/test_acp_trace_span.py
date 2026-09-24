"""Fleet tracing for ACP coders: `AcpClient.prompt` wraps every coder run in an
`acp:<name>` agent observation, and each finished coder tool call lands as an
EXPLICIT child of it — the reader task that sees the tool events does not carry
the span's context, so ambient nesting would orphan them into stray traces.

The project board dispatches coders from a background loop, outside any turn, so
before this a coder run never reached Langfuse at all."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from observability import tracing
from plugins.coding_agent.acp_client import AcpClient

# Two tool calls: t1 start → completed update; t2 arrives ALREADY completed with only
# ``rawOutput`` (ACP allows both; the follow-up update is only recommended).
_FAKE_AGENT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": u}})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read app.py",
                "rawInput": {"path": "app.py", "api_key": "hunter2"}})
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": "Read app.py",
                "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}]})
        update({"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "Run tests",
                "status": "completed", "rawOutput": {"exit": 0}})
        # ...and the recommended follow-up update for it anyway: must not end it twice.
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed"})
        update({"sessionUpdate": "tool_call", "toolCallId": "t3", "title": "Terminal env",
                "status": "completed", "rawOutput": "OPENAI_API_KEY=sk-" + "B" * 40})
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


@pytest.fixture
def fake_agent(tmp_path):
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    return script


@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = MagicMock()
    span = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return fake, span


async def _run(fake_agent, tmp_path) -> str:
    client = AcpClient(sys.executable, [str(fake_agent)], cwd=str(tmp_path), name="codex", record_runs=False)
    try:
        return await client.prompt("fix it", timeout=30.0)
    finally:
        await client.close()


async def test_coder_run_is_an_acp_agent_span_with_its_outcome(fake_agent, tmp_path, fake_langfuse):
    fake, span = fake_langfuse
    assert await _run(fake_agent, tmp_path) == "done"

    fake.start_as_current_observation.assert_called_once()
    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "acp:codex"
    assert kwargs["as_type"] == "agent"
    assert kwargs["metadata"]["cwd"] == str(tmp_path)

    outcome = span.update.call_args.kwargs
    assert outcome["output"] == "done"
    assert outcome["level"] == "DEFAULT"
    assert outcome["metadata"]["state"] == "completed"
    assert outcome["metadata"]["stop_reason"] == "end_turn"
    assert outcome["metadata"]["tool_calls"] == 3


async def test_coder_tool_call_is_parented_explicitly_on_the_run_span(fake_agent, tmp_path, fake_langfuse):
    fake, span = fake_langfuse
    await _run(fake_agent, tmp_path)

    # On the run span — never the client, which would start a fresh root trace.
    fake.start_observation.assert_not_called()
    # t2's follow-up update does NOT end it a second time (no phantom `tool:tool` span).
    assert span.start_observation.call_count == 3
    tool, terminal, env = (c.kwargs for c in span.start_observation.call_args_list)
    assert tool["name"] == "tool:Read app.py"
    assert tool["as_type"] == "tool"
    # Redacted as DATA: a key-based rule catches `api_key` even though "hunter2"
    # matches no secret pattern (the event's JSON text would have hidden the key).
    assert tool["input"] == {"input": '{"path": "app.py", "api_key": "[REDACTED]"}'}
    assert tool["output"] == "ok"
    assert tool["level"] == "DEFAULT"
    # Arrived terminal on the initial tool_call: still recorded, its rawOutput kept.
    assert terminal["name"] == "tool:Run tests"
    assert terminal["output"] == '{"exit": 0}'
    # Redacted like every other tool span: a coder running `env` doesn't ship the key.
    assert "B" * 40 not in env["output"]


async def test_failed_run_marks_the_span_as_an_error(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    script = tmp_path / "dies.py"
    script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="codex", record_runs=False)
    with pytest.raises(Exception):
        await client.prompt("fix it", timeout=10.0)
    await client.close()

    outcome = span.update.call_args.kwargs
    assert outcome["level"] == "ERROR"
    assert outcome["metadata"]["state"] == "failed"
    assert outcome["metadata"]["stop_reason"] is None


async def test_tracing_disabled_is_a_no_op(fake_agent, tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_langfuse", None)
    assert await _run(fake_agent, tmp_path) == "done"


async def test_tool_ends_once_for_the_ui_too(fake_agent, tmp_path, fake_langfuse):
    events: list[dict] = []

    async def on_tool(event: dict) -> None:
        events.append(event)

    client = AcpClient(sys.executable, [str(fake_agent)], cwd=str(tmp_path), name="codex", record_runs=False)
    try:
        await client.prompt("fix it", tool_callback=on_tool, timeout=30.0)
    finally:
        await client.close()

    ends = [e["id"] for e in events if e["phase"] == "end"]
    assert sorted(ends) == ["t1", "t2", "t3"]


async def test_incognito_coder_run_records_no_content(fake_agent, tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    token = tracing._io_suppressed_ctx.set(True)
    try:
        await _run(fake_agent, tmp_path)
    finally:
        tracing._io_suppressed_ctx.reset(token)

    assert span.update.call_args.kwargs["output"] == ""
    for call in span.start_observation.call_args_list:
        assert call.kwargs["input"] == {"input": ""} and call.kwargs["output"] == ""
