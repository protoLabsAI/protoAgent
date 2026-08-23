"""A CLI coding-agent run leaves a durable telemetry row (#3015).

Per-turn telemetry is written from a *turn's* terminal hook, and the project-board
loop dispatches coders from a background loop rather than from a turn — so the
expensive half of the work recorded nothing at all. The live PM booked $2,809 over
331 turns, every dollar of it its own reasoning, while the coders that wrote the PRs
contributed zero rows.

``AcpClient.prompt`` is the seam every dispatch path funnels through (the delegates
adapter, and the project board's tapped seam that deliberately bypasses that adapter),
so these tests drive a real fake ACP agent over the wire and assert the row that lands
in the store — not that a writer function was called.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from observability.telemetry_store import TelemetryStore
from plugins.coding_agent.acp_client import AcpClient, AcpError


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point STATE's telemetry holder at a throwaway store for the test."""
    import runtime.state as rs

    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


# A minimal ACP agent over stdio. `__SESSION__` is the session id it hands back and
# `__BODY__` is how it answers `session/prompt` — the only two things these tests vary.
_AGENT_TMPL = r'''
import json, sys, time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "__SESSION__"}})
    elif method == "session/prompt":
        __BODY__
'''

_CHUNK = (
    'send({"jsonrpc": "2.0", "method": "session/update", "params": {'
    '"sessionId": "__SESSION__", "update": {"sessionUpdate": "agent_message_chunk",'
    ' "content": {"type": "text", "text": "wrote the patch"}}}})'
)

# A slow-but-fine turn: the sleep is what makes `duration_ms` a measurement rather
# than a constant zero the assertions could not tell apart from a broken clock.
_OK_BODY = f'time.sleep(0.08)\n        {_CHUNK}\n        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})'
_REFUSAL_BODY = f'{_CHUNK}\n        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "refusal"}}}})'
_ERROR_BODY = 'send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "the coder blew up"}})'
# Streams one chunk and then never answers — so a test can cancel a turn that is
# genuinely in flight instead of racing a sleep against the handshake.
_HANG_BODY = f"{_CHUNK}\n        continue"


def _agent(tmp_path, name: str, body: str, session: str = "sess-acp-1") -> list[str]:
    script = tmp_path / f"acp_agent_{name}.py"
    script.write_text(_AGENT_TMPL.replace("__BODY__", body).replace("__SESSION__", session), encoding="utf-8")
    return [str(script)]


def _client(tmp_path, name: str, body: str, session: str = "sess-acp-1") -> AcpClient:
    return AcpClient(sys.executable, _agent(tmp_path, name, body, session), cwd=str(tmp_path), name=name)


async def test_a_successful_coder_run_writes_one_row(wired, tmp_path):
    """The acceptance case: a dispatch produces a durable record naming the delegate,
    its duration, and its outcome."""
    client = _client(tmp_path, "claude-code", _OK_BODY)
    try:
        assert await client.prompt("ship it", timeout=30.0) == "wrote the patch"
    finally:
        await client.close()

    rows = wired.recent()
    assert len(rows) == 1
    row = rows[0]
    # Origin-prefixed key (#3000's convention) — a coder run is identifiable as one
    # from the key alone, without joining against anything.
    assert row["task_id"].startswith("coder:claude-code:")
    assert row["session_id"] == "sess-acp-1"
    assert row["state"] == "completed"
    assert row["success"] == 1
    # The honest "not gateway-metered" label, the same one _acp_drive_turn uses.
    assert row["model"] == "acp:claude-code"
    assert row["models"] == "acp:claude-code"
    # Real wall clock, not a placeholder: the fake agent slept 80ms before answering.
    assert row["duration_ms"] >= 50


async def test_zero_cost_is_recorded_as_zero(wired, tmp_path):
    """The coder bills its own subscription and protoAgent never observes those
    numbers. Recording an invented figure would be worse than recording none (#3006)."""
    client = _client(tmp_path, "coder", _OK_BODY)
    try:
        await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["total_tokens"] == 0
    assert row["cache_read_input_tokens"] == 0
    assert row["cache_creation_input_tokens"] == 0
    assert row["llm_calls"] == 0
    assert row["tool_calls"] == 0


async def test_a_failed_coder_run_is_recorded_as_failed(wired, tmp_path):
    """A transport/protocol failure still raises to the caller AND lands a row —
    "how many coder runs failed" is half the question the issue asks."""
    client = _client(tmp_path, "coder", _ERROR_BODY)
    try:
        with pytest.raises(AcpError, match="the coder blew up"):
            await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0
    assert row["model"] == "acp:coder"


async def test_a_refusal_is_recorded_as_failed(wired, tmp_path):
    """`prompt()` is typed -> str, so a refusal returns normally. The recorded outcome
    follows the wire stop reason (`dead_end()`, #2279) rather than "did it return"."""
    client = _client(tmp_path, "coder", _REFUSAL_BODY)
    try:
        await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0


async def test_a_cancelled_coder_run_records_and_still_raises(wired, tmp_path):
    """The expensive failure class: an operator stop or an orchestrator watchdog kills
    a coder that has already burned minutes. It must record, and the cancellation must
    still propagate — swallowing it would leave the caller thinking the turn finished."""
    streamed = asyncio.Event()

    async def _on_text(_chunk: str) -> None:
        streamed.set()

    client = _client(tmp_path, "coder", _HANG_BODY)
    try:
        task = asyncio.create_task(client.prompt("ship it", text_callback=_on_text, timeout=30.0))
        await asyncio.wait_for(streamed.wait(), timeout=30.0)  # the turn is genuinely in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0
    assert row["model"] == "acp:coder"


async def test_a_telemetry_failure_cannot_break_the_coder_run(wired, tmp_path, monkeypatch):
    """Telemetry is best-effort by contract. A store that explodes must cost the
    operator a row, never the coder's work."""
    import server.turn_telemetry as tt

    reached: list[str] = []

    def _boom(**kw):
        reached.append(kw.get("task_id", ""))
        raise RuntimeError("telemetry is on fire")

    monkeypatch.setattr(tt, "record_turn", _boom)

    client = _client(tmp_path, "coder", _OK_BODY)
    try:
        assert await client.prompt("ship it", timeout=30.0) == "wrote the patch"
    finally:
        await client.close()

    # The writer really was reached and really did raise — otherwise this test would
    # pass just as well against an uninstrumented client.
    assert reached and reached[0].startswith("coder:coder:")
    assert wired.recent() == []


async def test_a_coder_run_publishes_no_turn_usage_event(wired, tmp_path, monkeypatch):
    """The console's fleet roster counts a member running with +1 `turn.started` and
    -1 terminal `turn.usage`. A coder run emits no `turn.started`, so emitting the -1
    half would drive that count negative — the row is durable, the bus event is not."""
    import server

    published: list[str] = []

    class _RecordingBus:
        def publish(self, topic, payload=None, **_kw):
            published.append(topic)

    monkeypatch.setattr(server, "_event_bus", _RecordingBus())

    client = _client(tmp_path, "coder", _OK_BODY)
    try:
        await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    assert wired.recent(), "the durable row is the point — it must still be written"
    assert "turn.usage" not in published
