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
import types

import pytest

from observability.telemetry_store import TelemetryStore
from plugins.coding_agent.acp_client import AcpClient, AcpError
from plugins.delegates.adapters import _INCOMPLETE_STOP_REASONS, _mark_incomplete


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


def _stop_body(reason: str) -> str:
    """A turn that streams its (possibly half-written) text and then reports ``reason``.

    Every outcome these tests assert is read off that one wire field, so varying it is
    how a refusal, a truncation and a clean finish are told apart.
    """
    return f'{_CHUNK}\n        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "{reason}"}}}})'


_REFUSAL_BODY = _stop_body("refusal")
_ERROR_BODY = 'send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "the coder blew up"}})'
# Streams one chunk and then never answers — so a test can cancel a turn that is
# genuinely in flight instead of racing a sleep against the handshake.
_HANG_BODY = f"{_CHUNK}\n        continue"


def _tool_call(n: int) -> str:
    """One ACP ``tool_call`` update — the wire event a coder emits per tool it runs."""
    return (
        'send({"jsonrpc": "2.0", "method": "session/update", "params": {'
        '"sessionId": "__SESSION__", "update": {"sessionUpdate": "tool_call",'
        f' "toolCallId": "t{n}", "title": "Read(file{n}.py)", "kind": "read"}}}}}})'
    )


# Three tool calls, then a clean finish.
_TOOLS_BODY = "\n        ".join([_tool_call(1), _tool_call(2), _tool_call(3), _OK_BODY])


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
    follows the wire stop reason rather than "did it return"."""
    client = _client(tmp_path, "coder", _REFUSAL_BODY)
    try:
        await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0


@pytest.mark.parametrize("stop_reason", sorted(_INCOMPLETE_STOP_REASONS))
async def test_a_reply_the_delegate_surface_calls_incomplete_is_recorded_as_failed(wired, tmp_path, stop_reason):
    """Parametrized off the delegate surface's OWN list of cut-off stop reasons, so the
    two readings of the same wire field cannot drift apart.

    They did drift: the row was classified with `dead_end()`, the retry-worthiness test,
    which deliberately excludes `max_tokens`. So a run the delegate surface stamped
    "do not treat it as complete" — and the orchestrator re-dispatched — booked a
    `completed`, `success=1` row, undercounting the truncation failure mode in the very
    number the issue asks for (#3015)."""
    client = _client(tmp_path, "coder", _stop_body(stop_reason))
    try:
        reply = await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    # What the orchestrator is told about this same reply, from the same wire field.
    marked = _mark_incomplete(reply, client.last_stop_reason)
    assert "incomplete reply" in marked

    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0


async def test_a_truncated_run_is_a_failed_row_and_still_worth_retrying(wired, tmp_path):
    """The two classifications answer different questions and must be free to disagree.

    `max_tokens` is NOT a dead end (#2279 — hitting the limit is exactly when escalating
    a tier or splitting the work is the right move), and it IS a failed run: the reply is
    cut off mid-generation. One classifier cannot serve both, which is why the outcome
    reads `unfinished_reason()`."""
    client = _client(tmp_path, "claude-code", _stop_body("max_tokens"))
    try:
        await client.prompt("ship it", timeout=30.0)
    finally:
        await client.close()

    assert client.dead_end() is None  # still retryable
    assert client.unfinished_reason() == "max_tokens"  # still not a success

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


async def test_the_coders_tool_calls_are_counted(wired, tmp_path):
    """Zero tokens and zero cost are honest but say nothing about effort, and the
    issue's evidence table is `turns | tool_calls | cost`. The count comes off the wire,
    not off `tool_callback` — the delegates adapter wires no callback at all, so a
    callback-derived count would have recorded zero for the commonest dispatch path."""
    client = _client(tmp_path, "claude-code", _TOOLS_BODY)
    try:
        await client.prompt("ship it", timeout=30.0)  # no tool_callback, deliberately
    finally:
        await client.close()

    row = wired.recent()[0]
    assert row["tool_calls"] == 3
    assert row["cost_usd"] == 0.0  # still no invented spend


async def test_a_second_run_does_not_inherit_the_first_runs_tool_count(wired, tmp_path):
    """The counter is per-turn state on a client that is POOLED and reused across runs.
    Without a reset the second dispatch would report the first one's work as its own."""
    client = _client(tmp_path, "claude-code", _TOOLS_BODY)
    try:
        await client.prompt("first", timeout=30.0)
        await client.prompt("second", timeout=30.0)
    finally:
        await client.close()

    rows = wired.recent()
    assert len(rows) == 2
    assert [r["tool_calls"] for r in rows] == [3, 3]


# ── the runs that are NOT coder dispatches ────────────────────────────────────
# `AcpClient.prompt` catches every caller, which is the point — and two of those
# callers are not coder dispatches at all. Both live in `runtime/acp_runtime.py`,
# and both would corrupt the very rollup this issue exists to make trustworthy.


async def test_the_agents_own_acp_runtime_turn_writes_no_coder_row(wired, tmp_path, monkeypatch):
    """Under `agent_runtime: acp:<agent>` the coding agent IS the brain, and
    `server.chat._acp_drive_turn` already books that turn under the same `acp:<agent>`
    label. A row from here too would double every ACP-runtime turn — and misfile a chat
    turn as a coder run. Drives the REAL client factory against a real fake agent, so
    this fails if the production wiring stops opting out."""
    import runtime.acp_runtime as rt_mod
    from runtime.context import AssembledContext

    monkeypatch.setattr(rt_mod, "persona_doc", lambda config: "")  # no SOUL.md read

    class _Ctx:
        def assemble(self, *, query=""):
            return AssembledContext(stable_prefix="", volatile_delta="", sources=[])

        def after_turn(self, *, user="", response=""): ...

    cfg = types.SimpleNamespace(
        agent_runtime="acp:codex",
        operator_mcp_tools=[],
        acp_agents={"codex": {"command": sys.executable, "args": _agent(tmp_path, "brain", _OK_BODY)}},
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = rt_mod.AcpRuntime(cfg, cwd=str(workspace), context=_Ctx())
    try:
        assert await rt.run_turn("hello") == "wrote the patch"
    finally:
        await rt.close()

    assert wired.recent() == [], "the chat turn's own recorder owns this row, not the client"


async def test_the_acp_aux_model_writes_no_coder_row(wired, tmp_path, monkeypatch):
    """The aux slots (compaction, goal verification, fact extraction) run on the same
    ACP client when there is no gateway. They are internal housekeeping, not coder work:
    counting them as coder runs would inflate the one number the issue asks for."""
    import runtime.acp_runtime as rt_mod

    monkeypatch.setattr(rt_mod, "_AUX_CLIENTS", {})
    cfg = types.SimpleNamespace(
        acp_agents={"codex": {"command": sys.executable, "args": _agent(tmp_path, "aux", _OK_BODY)}},
    )
    try:
        assert await rt_mod._aux_prompt("codex", cfg, "summarise this") == "wrote the patch"
    finally:
        for c in list(rt_mod._AUX_CLIENTS.values()):
            await c.close()

    assert wired.recent() == []


async def test_the_read_surface_answers_the_acceptance_question(wired, tmp_path):
    """The acceptance criterion, asserted through the HTTP surface an operator actually
    queries — not through the store the writer just wrote to. Two dispatches on one
    delegate, one of which the coder refused: `/api/telemetry/summary?since=` must show
    two coder runs at zero cost, and `/api/telemetry/recent` must show which one failed."""
    from datetime import datetime, timedelta, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.telemetry_routes import register_telemetry_routes

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    # One delegate, two runs — different scripts, same client name, exactly as the
    # pooled client would look across two dispatches.
    for script, body in (("ok", _OK_BODY), ("refused", _REFUSAL_BODY)):
        client = AcpClient(sys.executable, _agent(tmp_path, script, body), cwd=str(tmp_path), name="claude-code")
        try:
            await client.prompt("ship it", timeout=30.0)
        finally:
            await client.close()

    app = FastAPI()
    register_telemetry_routes(app)
    http = TestClient(app)

    summary = http.get("/api/telemetry/summary", params={"since": since}).json()["summary"]
    by_model = {m["model"]: m for m in summary["by_model"]}
    # "How many coder runs in the last 24h" — answerable today, no route changes.
    assert by_model["acp:claude-code"]["turns"] == 2
    assert by_model["acp:claude-code"]["cost_usd"] == 0.0  # zero recorded as zero

    turns = http.get("/api/telemetry/recent", params={"limit": 50}).json()["turns"]
    coder_runs = [t for t in turns if t["task_id"].startswith("coder:claude-code:")]
    assert len(coder_runs) == 2
    # …"and how many failed" — the refusal, and only the refusal.
    assert sum(1 for t in coder_runs if t["state"] == "failed") == 1
