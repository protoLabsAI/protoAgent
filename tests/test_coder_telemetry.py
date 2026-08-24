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
import shutil
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


# ── run attribution: no run books another run's effort (#3040) ────────────────
# The counter and the session id above live on a client that is POOLED across
# dispatches, and both are per-turn state reset on the way in. Two mechanisms let
# that reset miss. A run that dies inside `_ensure_started` — workdir gone, binary
# gone, handshake timeout — never reaches it. And the stdout reader that increments
# the counter is not bound by `_turn_lock`, so a turn abandoned on timeout goes on
# feeding the tally of whichever run holds the client next.
#
# Every case here runs against a store that already holds a month of this instance's
# real traffic — PM chat turns, this same delegate's earlier runs, a peer delegation —
# plus a second delegate dispatched concurrently. Be precise about what that buys: the
# per-row assertions below identify their rows by `row_id` diff, so the seeded rows do
# not themselves make a red test red. What they do is put the assertions in the
# population the number is actually read in — `recent()` full of same-prefix history
# where position means nothing, and the instance-wide `tool_calls` aggregate the
# operator surface reports, which
# `test_a_dead_agents_orphaned_backend_neither_charges_nor_kills_the_next_run` asserts
# with the seeded rows INSIDE the sum rather than filtered out of it.


def _seed_a_month_of_traffic(store) -> None:
    """Thirty days of the traffic a live instance actually accumulates.

    Every seeded row carries a non-zero ``tool_calls`` and a real session id, including
    rows for the *same* delegate the tests below dispatch — the population an inherited
    count would be invisible in, and the one every aggregate these tests read is
    computed over.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    for day in range(1, 31):
        ended = now - timedelta(days=day)
        started = ended - timedelta(seconds=45)
        for task_id, session_id, model, tools, cost in (
            (f"a2a:pm-turn-{day}", f"ctx-pm-{day}", "anthropic:claude-opus-4", 4, 0.31),
            (f"coder:claude-code:hist{day:02d}", f"sess-hist-{day}", "acp:claude-code", 7, 0.0),
            (f"coder:codex:hist{day:02d}", f"sess-codex-{day}", "acp:codex", 12, 0.0),
        ):
            store.record(
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "state": "completed",
                    "success": 1,
                    "model": model,
                    "models": model,
                    "input_tokens": 4200,
                    "output_tokens": 900,
                    "total_tokens": 5100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cost_usd": cost,
                    "duration_ms": 45_000,
                    "llm_calls": 3,
                    "tool_calls": tools,
                    "created_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "soul_rev": "abc1234",
                    "trace_id": "",
                    "tool_durations": "{}",
                    "context_tokens": 4200,
                }
            )


def _rows_written_by(store, before: set) -> list[dict]:
    """The rows this test produced, told apart from the seeded history by row id — not
    by position, which a month of same-prefix history makes meaningless."""
    return [r for r in store.recent(500) if r["row_id"] not in before]


async def test_a_run_that_dies_before_it_starts_books_none_of_the_previous_runs_effort(wired, tmp_path):
    """The reported case, end to end: run A does three tool calls in session
    ``sess-run-a``; run B's worktree is gone before it starts, so it never gets past
    ``_ensure_started``. B must record its own nothing — 0 tool calls, no session — not
    A's three and A's session id.

    The workdir being gone is routine rather than exotic: managed-git dispatch (ADR
    0076) creates a disposable per-call worktree and the client that outlives it is
    pooled. The agent dying between dispatches is what makes ``_ensure_started``
    reachable on a warm client at all.
    """
    _seed_a_month_of_traffic(wired)
    before = {r["row_id"] for r in wired.recent(500)}

    worktree = tmp_path / "wt-feature-1"
    worktree.mkdir()
    client = AcpClient(
        sys.executable,
        _agent(tmp_path, "pooled", _TOOLS_BODY, session="sess-run-a"),
        cwd=str(worktree),
        name="claude-code",
    )
    try:
        assert await client.prompt("first feature", timeout=30.0) == "wrote the patch"

        # The pooled client's agent died between dispatches, and managed-git already
        # threw the worktree away. Next dispatch, same client.
        await client.close()
        shutil.rmtree(worktree)

        with pytest.raises(AcpError, match="workdir does not exist"):
            await client.prompt("second feature", timeout=30.0)
    finally:
        await client.close()

    fresh = _rows_written_by(wired, before)
    assert len(fresh) == 2, "both dispatches record — a dispatch that never ran is a failed dispatch"
    ran = [r for r in fresh if r["state"] == "completed"][0]
    died = [r for r in fresh if r["state"] == "failed"][0]

    assert ran["tool_calls"] == 3
    assert ran["session_id"] == "sess-run-a"

    # The run that never started did no work and opened no session of its own.
    assert died["tool_calls"] == 0
    assert died["session_id"] == ""
    assert died["session_id"] != ran["session_id"]


# An agent that keeps working after the client gives up on the turn. The flood runs on
# its own thread precisely because the real failure needs it: a coder that blew its
# timeout is not paused waiting to be collected, it is still running tools and still
# writing `session/update` notifications down the same stdout the next run reads.
_FLOODING_AGENT = r'''
import json, sys, threading, time

_out = threading.Lock()


def send(obj):
    with _out:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def tool(tag):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "__SESSION__",
        "update": {"sessionUpdate": "tool_call", "toolCallId": "t-%s" % tag,
                   "title": "Edit(%s.py)" % tag, "kind": "edit"}}})


def chunk():
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "__SESSION__",
        "update": {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": "wrote the patch"}}}})


def flood():
    for n in range(2000):
        tool("late%d" % n)
        time.sleep(0.01)


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
        blocks = (msg.get("params") or {}).get("prompt") or []
        text = "".join(b.get("text", "") for b in blocks)
        if "runaway" in text:
            # Never answers. The client's own timeout abandons the turn while this
            # thread carries on emitting.
            threading.Thread(target=flood, daemon=True).start()
        else:
            tool("own")
            time.sleep(0.25)
            chunk()
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
'''

# A healthy peer coder working the whole time the runaway turn is being abandoned —
# the board dispatches several coders at once, so attribution has to hold across
# clients as well as across turns on one client.
_PEER_TOOLS_BODY = "\n        ".join(
    [_tool_call(1), "time.sleep(0.2)", _tool_call(2), "time.sleep(0.2)", _tool_call(3), _OK_BODY]
)


def _flooding_agent(tmp_path, session: str = "sess-runaway") -> list[str]:
    script = tmp_path / "acp_agent_runaway.py"
    script.write_text(_FLOODING_AGENT.replace("__SESSION__", session), encoding="utf-8")
    return [str(script)]


async def test_a_timed_out_turn_cannot_spend_the_next_runs_tool_budget(wired, tmp_path):
    """A dispatch that blows its timeout leaves the coder running. The counter is
    incremented by the stdout reader, which ``_turn_lock`` does not bind — so those
    late ``session/update`` notifications land wherever the pooled client goes next.

    Run A is abandoned mid-flood; run B does exactly one tool call. B's row must say
    one. Asserted with a second delegate dispatched concurrently and a month of history
    already in the store, because that is the shape the number is read in.
    """
    _seed_a_month_of_traffic(wired)
    before = {r["row_id"] for r in wired.recent(500)}

    peer = _client(tmp_path, "codex", _PEER_TOOLS_BODY, session="sess-peer-live")
    client = AcpClient(sys.executable, _flooding_agent(tmp_path), cwd=str(tmp_path), name="claude-code")
    try:
        peer_task = asyncio.create_task(peer.prompt("peer feature", timeout=30.0))
        with pytest.raises(AcpError, match="session/prompt timed out"):
            await client.prompt("runaway feature", timeout=0.8)
        assert await peer_task == "wrote the patch"

        # The stream is disowned the moment we give up, not merely when the next
        # dispatch respawns the agent — between those two points the flood is still
        # arriving and the reader that counts it is still alive, so a run B that clears
        # the counter and *then* awaits its respawn would already be accruing. Pinned
        # here rather than through a row because there is no row until run B ends, by
        # which point the respawn would hide which half of the fence did the work.
        settled = client._turn_tool_calls
        await asyncio.sleep(0.3)
        assert client._turn_tool_calls == settled

        assert await client.prompt("second feature", timeout=30.0) == "wrote the patch"
    finally:
        await client.close()
        await peer.close()

    fresh = _rows_written_by(wired, before)
    mine = [r for r in fresh if r["model"] == "acp:claude-code"]
    peer_rows = [r for r in fresh if r["model"] == "acp:codex"]
    assert len(mine) == 2 and len(peer_rows) == 1

    abandoned = [r for r in mine if r["state"] == "failed"][0]
    following = [r for r in mine if r["state"] == "completed"][0]

    # The agent really was mid-work when we walked away — otherwise this test would
    # pass just as well against a client that never counted anything at all.
    assert abandoned["tool_calls"] >= 1
    # …and none of that work is the next run's.
    assert following["tool_calls"] == 1
    # The concurrent peer's own count is untouched by either.
    assert peer_rows[0]["tool_calls"] == 3
    assert peer_rows[0]["session_id"] == "sess-peer-live"


# ── the stream of an agent that died between dispatches (#3040) ──────────────
# Not every respawn is a fenced one. A pooled client whose agent died between
# dispatches goes straight from `_ensure_started` to `_start` — no close(), nothing
# cancelled — and the reader that served the dead process is still alive, because the
# backend that process spawned holds the same stdout open and keeps writing into it.
# (That orphan pile is the reason `close()` kills the whole process GROUP.) Until the
# generation advances on every start, that reader is indistinguishable from the live
# one: its `tool_call` updates are charged to the next run, and when its pipe finally
# does close, its `finally` fails that run's in-flight `session/prompt` with the
# previous process's obituary.

_ORPHANED_AGENT = r'''
import json, os, subprocess, sys, time

SESSION = "__SESSION__"
SENTINEL = sys.argv[1]

# The backend this adapter spawned, and which outlives it — the whole point: it holds
# the stdout the client is reading and keeps emitting `tool_call` updates into it long
# after the process the client thinks it is talking to is gone. It stops when the test
# creates the sentinel, so the EOF lands at a moment the test chooses rather than a
# moment it races.
_FLOOD = """
import json, os, sys, time
sess, sentinel = sys.argv[1], sys.argv[2]
for n in range(600):
    if os.path.exists(sentinel):
        break
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sess,
        "update": {"sessionUpdate": "tool_call", "toolCallId": "orphan-%d" % n,
                   "title": "Edit(orphan.py)", "kind": "edit"}}}) + "\\n")
    sys.stdout.flush()
    time.sleep(0.01)
"""


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def tool(tag):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": SESSION,
        "update": {"sessionUpdate": "tool_call", "toolCallId": "t-%s" % tag,
                   "title": "Edit(%s.py)" % tag, "kind": "edit"}}})


def chunk():
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": SESSION,
        "update": {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": "wrote the patch"}}}})


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
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": SESSION}})
    elif method == "session/prompt":
        blocks = (msg.get("params") or {}).get("prompt") or []
        text = "".join(b.get("text", "") for b in blocks)
        if "first" in text:
            tool("own")
            chunk()
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
            # Answered cleanly — so nothing fences this stream. Only then hand the
            # pipe to the backend and die.
            time.sleep(0.3)
            subprocess.Popen([sys.executable, "-c", _FLOOD, SESSION, SENTINEL],
                             stdout=sys.stdout, stderr=subprocess.DEVNULL)
            time.sleep(0.05)
            os._exit(1)
        else:
            tool("own")
            time.sleep(2.8)
            chunk()
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
'''


def _orphaning_agent(tmp_path, sentinel, session: str = "sess-orphaned") -> list[str]:
    script = tmp_path / "acp_agent_orphaned.py"
    script.write_text(_ORPHANED_AGENT.replace("__SESSION__", session), encoding="utf-8")
    return [str(script), str(sentinel)]


async def _wait_for_exit(client, timeout: float = 5.0) -> None:
    """Block until the agent subprocess has actually been reaped — the precondition
    the case needs, not something it asserts. Without it the next dispatch could find
    a process still marked running and never take the respawn path at all."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if client._proc is not None and client._proc.returncode is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the fake agent never exited")


async def test_a_dead_agents_orphaned_backend_neither_charges_nor_kills_the_next_run(wired, tmp_path):
    """Run A finishes cleanly — ``end_turn``, one tool call — and *then* the agent dies,
    leaving a backend flooding the stdout it inherited. Nothing fenced that stream: the
    turn ended the way turns are supposed to. Run B, on the same pooled client, must
    still record one tool call and complete, even though the orphan is writing
    ``tool_call`` updates throughout it and its pipe closes mid-turn.

    Both symptoms are one root: the reader of a replaced process outliving it. Asserted
    on the durable rows, with a peer coder dispatched concurrently and a month of this
    instance's history already in the store — the population the number is read in.
    """
    _seed_a_month_of_traffic(wired)
    before = {r["row_id"] for r in wired.recent(500)}
    seeded_tool_calls = sum(r["tool_calls"] for r in wired.recent(500))

    sentinel = tmp_path / "stop-flooding"
    peer = _client(tmp_path, "codex", _PEER_TOOLS_BODY, session="sess-peer-live")
    client = AcpClient(
        sys.executable, _orphaning_agent(tmp_path, sentinel), cwd=str(tmp_path), name="claude-code"
    )
    try:
        peer_task = asyncio.create_task(peer.prompt("peer feature", timeout=30.0))
        assert await client.prompt("first feature", timeout=30.0) == "wrote the patch"
        await _wait_for_exit(client)

        # Same pooled client, next dispatch. `_ensure_started` respawns — but by the
        # unfenced route, because run A ended cleanly.
        second = asyncio.create_task(client.prompt("second feature", timeout=30.0))
        await asyncio.sleep(0.4)  # the orphan's updates arrive while run B is generating
        sentinel.write_text("stop", encoding="utf-8")  # …and now its pipe closes, mid-turn
        assert await second == "wrote the patch"
        assert await peer_task == "wrote the patch"
    finally:
        sentinel.write_text("stop", encoding="utf-8")
        await client.close()
        await peer.close()

    fresh = _rows_written_by(wired, before)
    mine = [r for r in fresh if r["model"] == "acp:claude-code"]
    peer_rows = [r for r in fresh if r["model"] == "acp:codex"]
    assert len(mine) == 2 and len(peer_rows) == 1

    # Run B did one tool call and finished. A run charged for the orphan's work reads
    # as a busy run; a run killed by the orphan's EOF reads as a failed one.
    following = max(mine, key=lambda r: r["created_at"])
    assert following["state"] == "completed"
    assert following["tool_calls"] == 1
    assert all(r["tool_calls"] == 1 for r in mine)
    assert peer_rows[0]["tool_calls"] == 3

    # And the same thing said the way an operator reads it: the instance-wide tool-call
    # total is its month of history plus exactly the five calls these three runs made.
    # The seeded rows are inside this number, not filtered out of it.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.telemetry_routes import register_telemetry_routes

    app = FastAPI()
    register_telemetry_routes(app)
    summary = TestClient(app).get("/api/telemetry/summary").json()["summary"]
    assert summary["tool_calls"] == seeded_tool_calls + 5


# ── an error the agent answered with is not an abandoned turn (#3040) ────────

_POOLED_ERROR_AGENT = r'''
import json, os, sys, time

# The session id names the PROCESS, so a respawn is visible in the recorded row: this
# agent advertises no `loadSession`, so a client that reaps it opens a brand-new
# session and the ACP thread is gone.
SESSION = "sess-%d" % os.getpid()


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
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": SESSION}})
    elif method == "session/prompt":
        blocks = (msg.get("params") or {}).get("prompt") or []
        text = "".join(b.get("text", "") for b in blocks)
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": SESSION,
            "update": {"sessionUpdate": "tool_call", "toolCallId": "t-own",
                       "title": "Read(spec.md)", "kind": "read"}}})
        if "boom" in text:
            # A turn the agent ends by telling us it failed. It is idle afterwards.
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32000, "message": "the coder blew up"}})
        else:
            time.sleep(0.05)
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": SESSION,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "wrote the patch"}}}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
'''


async def test_an_error_the_agent_reported_keeps_the_pooled_session(wired, tmp_path):
    """A JSON-RPC error response is the agent saying it stopped. Nothing is left
    generating, so there is nothing to fence — and fencing it anyway would SIGTERM a
    healthy pooled agent on every ordinary agent-side error, then reopen a fresh
    session, dropping the conversation thread that pooling by ``conversation_key``
    exists to keep.

    Read off the durable rows: the run after the error must record the SAME ACP session
    as the run before it. This agent names its session after its pid, so a respawn
    cannot hide.
    """
    _seed_a_month_of_traffic(wired)
    before = {r["row_id"] for r in wired.recent(500)}

    script = tmp_path / "acp_agent_pooled_error.py"
    script.write_text(_POOLED_ERROR_AGENT, encoding="utf-8")
    peer = _client(tmp_path, "codex", _PEER_TOOLS_BODY, session="sess-peer-live")
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="claude-code")
    try:
        peer_task = asyncio.create_task(peer.prompt("peer feature", timeout=30.0))
        assert await client.prompt("first feature", timeout=30.0) == "wrote the patch"
        with pytest.raises(AcpError, match="the coder blew up"):
            await client.prompt("boom feature", timeout=30.0)
        # The conversation carries on in the same session — an agent that errored on one
        # turn is still the agent that has the context of the previous ten.
        assert await client.prompt("third feature", timeout=30.0) == "wrote the patch"
        assert await peer_task == "wrote the patch"
    finally:
        await client.close()
        await peer.close()

    fresh = sorted(
        [r for r in _rows_written_by(wired, before) if r["model"] == "acp:claude-code"],
        key=lambda r: r["created_at"],
    )
    assert len(fresh) == 3
    first, errored, third = fresh

    assert first["state"] == "completed"
    assert errored["state"] == "failed"
    assert third["state"] == "completed"

    # The whole point: one session across all three, including the one that errored —
    # which did run in it, and records it.
    assert first["session_id"].startswith("sess-")
    assert errored["session_id"] == first["session_id"]
    assert third["session_id"] == first["session_id"], "the pooled agent was evicted by an error it reported"

    # The error turn's own tool call is still its own, and the peer is untouched.
    assert errored["tool_calls"] == 1
    assert third["tool_calls"] == 1
    peer_rows = [r for r in _rows_written_by(wired, before) if r["model"] == "acp:codex"]
    assert peer_rows[0]["tool_calls"] == 3


async def test_a_turn_queued_behind_an_abandoned_one_is_charged_only_for_itself(wired, tmp_path):
    """The fan-out shape ``prompt()`` explicitly supports: a second dispatch arrives on
    the same pooled client *while* the first is still in flight, and logs "prompt queued
    behind an in-flight turn". It is the queued turn — not a later, leisurely one — that
    inherits the client the instant the runaway is abandoned, with the flood still
    arriving and the respawn not yet done.

    The queued run must record its own single tool call.
    """
    _seed_a_month_of_traffic(wired)
    before = {r["row_id"] for r in wired.recent(500)}

    peer = _client(tmp_path, "codex", _PEER_TOOLS_BODY, session="sess-peer-live")
    client = AcpClient(
        sys.executable, _flooding_agent(tmp_path, session="sess-queued"), cwd=str(tmp_path), name="claude-code"
    )
    try:
        peer_task = asyncio.create_task(peer.prompt("peer feature", timeout=30.0))
        runaway = asyncio.create_task(client.prompt("runaway feature", timeout=0.8))
        await asyncio.sleep(0.3)  # the second dispatch arrives mid-generation, not after
        assert client._turn_lock.locked()
        queued = asyncio.create_task(client.prompt("second feature", timeout=30.0))
        with pytest.raises(AcpError, match="session/prompt timed out"):
            await runaway
        assert await queued == "wrote the patch"
        assert await peer_task == "wrote the patch"
    finally:
        await client.close()
        await peer.close()

    fresh = _rows_written_by(wired, before)
    mine = [r for r in fresh if r["model"] == "acp:claude-code"]
    abandoned = [r for r in mine if r["state"] == "failed"][0]
    following = [r for r in mine if r["state"] == "completed"][0]

    assert abandoned["tool_calls"] >= 1  # it really was mid-work when we walked away
    assert following["tool_calls"] == 1
    assert [r for r in fresh if r["model"] == "acp:codex"][0]["tool_calls"] == 3
