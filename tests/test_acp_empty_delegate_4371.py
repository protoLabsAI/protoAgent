"""An ACP delegate's empty reply is a FAILED delegation, not a silent success (#4371).

A ``delegate_to(..., background=True)`` to an ACP coder can finish with no text and a
NORMAL stop reason (``end_turn``, or none at all). That used to book the background job
``completed`` and deliver a successful ``(replied with nothing)`` — the lead could not
tell a coder that did its work and said nothing from one that never started (a cwd it
can't write, a sandbox denial, a crashed preflight). Both looked like success.

What these pin (all over a real ``AcpClient`` + a scripted ACP agent subprocess — no live
model), mirroring the wire harness in ``test_coder_telemetry.py``:

* ``AcpClient.empty_reply_failure`` fires only for a blank-but-normal-ended turn, names
  the stop reason (``none`` when absent), the turn's tool-call count, and the stderr tail,
  and stays out of the way for ``max_tokens``/``refusal``/``cancelled`` and any real reply.
* ``AcpAdapter.dispatch`` raises a ``DelegateError`` with that diagnostic for an empty
  ``end_turn`` (and an absent stop reason).
* A background delegation with that fake drains ``ok: False`` with a ``(failed)`` note and
  NEVER a successful ``(replied with nothing)``.
* The run books ``failed`` (not ``completed``/``success``) in telemetry and in its trace.
* A ``max_tokens`` empty reply keeps its existing ``[no reply — …]`` marker.
* ``TappedResult`` carries the empty-reply signal while ``reply`` stays the raw string, so
  the board's same-tier empty-reply retry (#2991) still works.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections import deque
from unittest.mock import MagicMock

import pytest

from observability import tracing
from observability.telemetry_store import TelemetryStore
from plugins.coding_agent.acp_client import _EMPTY_REPLY_SIGNAL, AcpClient
from plugins.delegates.acp_adapter import AcpAdapter
from plugins.delegates.base import DelegateError

# ── a scripted ACP agent over stdio (same shape as test_coder_telemetry.py) ──────────

_AGENT_TMPL = r'''
import json, sys

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

# Ends the turn with no agent_message_chunk at all — the #4371 failure.
_EMPTY_END_TURN = 'send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})'
# Ends the turn with NO stopReason whatsoever (the "absent stop reason" case).
_EMPTY_NO_REASON = 'send({"jsonrpc": "2.0", "id": mid, "result": {}})'
# Empty, but cut off at the output-token limit — keeps its own marker, must NOT become
# the empty_reply failure.
_EMPTY_MAX_TOKENS = 'send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "max_tokens"}})'
# A real reply (control): streams text, then ends normally.
_CHUNK = (
    'send({"jsonrpc": "2.0", "method": "session/update", "params": {'
    '"sessionId": "__SESSION__", "update": {"sessionUpdate": "agent_message_chunk",'
    ' "content": {"type": "text", "text": "wrote the patch"}}}})'
)
_REAL_REPLY = f'{_CHUNK}\n        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})'


def _tool_call(n: int) -> str:
    return (
        'send({"jsonrpc": "2.0", "method": "session/update", "params": {'
        '"sessionId": "__SESSION__", "update": {"sessionUpdate": "tool_call",'
        f' "toolCallId": "t{n}", "title": "Read(file{n}.py)", "kind": "read"}}}}}})'
    )


# Two tool calls (work happened) and then a silent, normal end — the "did work but said
# nothing" shape the tool-call count exists to distinguish from "never started".
_EMPTY_END_TURN_AFTER_TOOLS = "\n        ".join([_tool_call(1), _tool_call(2), _EMPTY_END_TURN])


def _agent(tmp_path, name: str, body: str, session: str = "sess-acp-1") -> list[str]:
    script = tmp_path / f"acp_agent_{name}.py"
    script.write_text(_AGENT_TMPL.replace("__BODY__", body).replace("__SESSION__", session), encoding="utf-8")
    return [str(script)]


def _client(tmp_path, name: str, body: str) -> AcpClient:
    # record_runs=False by default here; the telemetry test flips it back on.
    return AcpClient(sys.executable, _agent(tmp_path, name, body), cwd=str(tmp_path), name=name, record_runs=False)


def _delegate(tmp_path, body: str, name: str = "coder"):
    """A parsed ACP ``Delegate`` whose command is the scripted agent."""
    return AcpAdapter().parse(
        {"name": name, "type": "acp", "command": sys.executable, "args": _agent(tmp_path, name, body),
         "workdir": str(tmp_path)}
    )


# ── unit matrix: empty_reply_failure classifies the wire field correctly ─────────────


def _bare(stop_reason, *, tool_calls: int = 0, stderr=()) -> AcpClient:
    """A field-only client (no subprocess) — exactly what empty_reply_failure reads."""
    c = AcpClient.__new__(AcpClient)
    c.name = "t"
    c.last_stop_reason = stop_reason
    c._turn_tool_calls = tool_calls
    c._stderr_tail = deque(stderr, maxlen=50)
    return c


def test_empty_end_turn_is_an_empty_reply_failure_naming_reason_and_count():
    note = _bare("end_turn", tool_calls=2).empty_reply_failure("")
    assert note is not None
    assert _EMPTY_REPLY_SIGNAL in note
    assert "end_turn" in note
    assert "2 tool call(s)" in note
    assert "no stderr output" in note


def test_absent_stop_reason_is_covered_and_reads_as_none():
    note = _bare(None).empty_reply_failure("")
    assert note is not None and "stop reason none" in note
    assert "0 tool call(s)" in note


def test_stderr_tail_rides_the_diagnostic_when_present():
    note = _bare("end_turn", stderr=["could not open /tmp/out", "preflight aborted"]).empty_reply_failure("")
    assert "last stderr:" in note and "preflight aborted" in note


@pytest.mark.parametrize("reason", ["max_tokens", "refusal", "cancelled"])
def test_self_classifying_reasons_are_not_empty_reply_failures(reason):
    # These already book failed via unfinished_reason() and carry their own markers.
    assert _bare(reason).empty_reply_failure("") is None


def test_a_real_reply_is_never_an_empty_reply_failure():
    assert _bare("end_turn").empty_reply_failure("wrote the patch") is None
    assert _bare(None).empty_reply_failure("  text  ") is None


# ── the adapter boundary raises so the background drain sees ok:false ────────────────


def _wire_pool(monkeypatch, client: AcpClient) -> None:
    """Hand the adapter our scripted client instead of the real pooled one."""
    monkeypatch.setattr("plugins.coding_agent._client_for", lambda spec: client)
    monkeypatch.setattr("plugins.coding_agent._drop_client", lambda spec: None)


async def test_dispatch_fails_on_empty_end_turn_with_a_named_diagnostic(tmp_path, monkeypatch):
    client = _client(tmp_path, "coder", _EMPTY_END_TURN_AFTER_TOOLS)
    _wire_pool(monkeypatch, client)
    d = _delegate(tmp_path, _EMPTY_END_TURN_AFTER_TOOLS)
    try:
        with pytest.raises(DelegateError) as ei:
            await AcpAdapter().dispatch(d, "do the thing")
    finally:
        await client.close()
    msg = str(ei.value)
    assert "coder" in msg and "no reply" in msg
    assert _EMPTY_REPLY_SIGNAL in msg and "end_turn" in msg
    assert "2 tool call(s)" in msg  # names the current-turn tool-call count
    assert "stderr" in msg  # the stderr-tail label is present even when the tail is empty


async def test_dispatch_fails_when_the_stop_reason_is_absent(tmp_path, monkeypatch):
    client = _client(tmp_path, "coder", _EMPTY_NO_REASON)
    _wire_pool(monkeypatch, client)
    d = _delegate(tmp_path, _EMPTY_NO_REASON)
    try:
        with pytest.raises(DelegateError) as ei:
            await AcpAdapter().dispatch(d, "do the thing")
    finally:
        await client.close()
    assert "stop reason none" in str(ei.value)


async def test_a_real_reply_still_dispatches_cleanly(tmp_path, monkeypatch):
    client = _client(tmp_path, "coder", _REAL_REPLY)
    _wire_pool(monkeypatch, client)
    d = _delegate(tmp_path, _REAL_REPLY)
    try:
        reply = await AcpAdapter().dispatch(d, "do the thing")
    finally:
        await client.close()
    assert reply == "wrote the patch"


async def test_max_tokens_empty_reply_keeps_its_incomplete_marker(tmp_path, monkeypatch):
    client = _client(tmp_path, "coder", _EMPTY_MAX_TOKENS)
    _wire_pool(monkeypatch, client)
    d = _delegate(tmp_path, _EMPTY_MAX_TOKENS)
    try:
        reply = await AcpAdapter().dispatch(d, "do the thing")  # must NOT raise
    finally:
        await client.close()
    assert reply.startswith("[no reply —")
    assert "output-token limit" in reply
    assert _EMPTY_REPLY_SIGNAL not in reply


# ── the background path: drains ok:false, never "(replied with nothing)" ─────────────


async def _settle(store, job_id, tries: int = 400):
    for _ in range(tries):
        j = store.get(job_id)
        if j is not None and j.status != "running":
            return j
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never settled")


async def test_background_delegation_drains_ok_false_not_replied_with_nothing(tmp_path, monkeypatch):
    from background.manager import BackgroundManager
    from background.store import BackgroundStore
    import runtime.state as rs

    chat = importlib.import_module("server.chat")

    client = _client(tmp_path, "coder", _EMPTY_END_TURN)
    _wire_pool(monkeypatch, client)
    d = _delegate(tmp_path, _EMPTY_END_TURN)

    mgr = BackgroundManager(agent_name="a", invoke_url="http://127.0.0.1:0", store=BackgroundStore(str(tmp_path / "bg.db")))
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    session = "sess-4371"

    async def _work() -> str:
        # Mirrors _spawn_background_delegation's work=registry.dispatch wrapping.
        return await AcpAdapter().dispatch(d, "write the proof under /tmp")

    try:
        job_id = await mgr.spawn_work(
            origin_session=session, kind="delegate", description="delegate → coder", work=_work, result_author="coder"
        )
        job = await _settle(mgr.store, job_id)
        assert job.status == "failed", "an empty normal-ended reply must fail the background job"

        _msgs, room_replies = chat._drain_background(session)
    finally:
        await client.close()

    assert len(room_replies) == 1
    reply = room_replies[0]
    assert reply["ok"] is False
    text = reply["text"]
    assert "(replied with nothing)" not in text
    assert "(failed" in text  # the drain's failure note
    assert _EMPTY_REPLY_SIGNAL in text and "end_turn" in text  # the diagnostic rode through


# ── telemetry + trace classify the run as failed (the #3015 lesson) ──────────────────


@pytest.fixture
def wired(tmp_path, monkeypatch):
    import runtime.state as rs

    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    return store


async def test_empty_reply_books_a_failed_telemetry_row(wired, tmp_path):
    client = AcpClient(sys.executable, _agent(tmp_path, "coder", _EMPTY_END_TURN), cwd=str(tmp_path), name="coder")
    try:
        assert await client.prompt("do the thing", timeout=30.0) == ""  # raw reply unchanged
    finally:
        await client.close()
    row = wired.recent()[0]
    assert row["state"] == "failed"
    assert row["success"] == 0


@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = MagicMock()
    span = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    span.children = []
    span.start_observation.side_effect = lambda **kw: MagicMock()
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return fake, span


async def test_empty_reply_marks_the_trace_span_as_an_error(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    client = AcpClient(
        sys.executable, _agent(tmp_path, "coder", _EMPTY_END_TURN), cwd=str(tmp_path), name="coder", record_runs=False
    )
    try:
        await client.prompt("do the thing", timeout=30.0)
    finally:
        await client.close()
    outcome = span.update.call_args.kwargs
    assert outcome["level"] == "ERROR"
    assert outcome["metadata"]["state"] == "failed"
    assert outcome["metadata"]["stop_reason"] == "end_turn"
    assert _EMPTY_REPLY_SIGNAL in outcome["output"]  # the trace says WHY it failed


# ── the board reads TappedResult: raw reply preserved, signal exposed (#2991) ────────


async def test_tapped_result_exposes_the_empty_signal_but_keeps_the_raw_reply(tmp_path):
    client = _client(tmp_path, "coder", _EMPTY_END_TURN)
    try:
        reply = await client.prompt("do the thing", timeout=30.0)
        tapped = client.tapped_result(reply)
    finally:
        await client.close()
    assert tapped.reply == ""  # raw contract unchanged — the board's empty-reply retry still fires
    assert tapped.stop_reason == "end_turn"
    assert tapped.empty_reply is not None and _EMPTY_REPLY_SIGNAL in tapped.empty_reply


async def test_tapped_result_leaves_max_tokens_to_its_own_marker(tmp_path):
    client = _client(tmp_path, "coder", _EMPTY_MAX_TOKENS)
    try:
        reply = await client.prompt("do the thing", timeout=30.0)
        tapped = client.tapped_result(reply)
    finally:
        await client.close()
    assert tapped.empty_reply is None  # max_tokens is not the empty_reply failure
    assert tapped.stop_reason == "max_tokens"
