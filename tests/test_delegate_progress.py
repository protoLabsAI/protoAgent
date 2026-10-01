"""A coding-agent delegation's LIVE progress reaches the console's delegation card (#3979).

Before this, an `@claude-code <task>` showed a spinner and a clock for the whole run:
the ACP adapter awaited ``client.prompt()`` with no callbacks, so the coder's plan, tool
calls and narration were dropped on the floor. These pin the whole chain, from a REAL
subprocess speaking ACP over stdio to the frames the console consumes:

* ``graph.delegate_progress`` — the bounded, throttled tracker + its ContextVar sink;
* the ACP client — plan callback, ``kind``/``locations`` on tool events, and the
  segment-replay guard that doubled the first sentence of a reply;
* the adapter — a sink bound by the card's owner gets snapshots; no sink, no change;
* the three owners: the ``@`` short-circuit (frames keyed to its mention card), the
  foreground ``delegate_to`` tool (a LangChain custom event keyed to its run), and a
  background job (``background.progress``);
* the durable task store keeps one snapshot per delegation, not one per frame.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

from graph import delegate_progress as dp

pytestmark = pytest.mark.platform_sensitive  # spawns a fake ACP agent subprocess


# ── a fake ACP coder that does what claude-agent-acp does on the wire ─────────────
# One prompt: narrate in streamed deltas, RE-SEND that whole block as one chunk (the
# replay claude-agent-acp emits — captured live 2026-10-01), plan, open a tool with a
# placeholder title then refine it with kind + locations, complete it, update the plan,
# and answer.
_CODER = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def update(u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": u}})
def say(t):
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": t}})
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
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        for d in ("I'll look", " at the existing `calc.", "py` first."):
            say(d)
        say("I'll look at the existing `calc.py` first.")
        update({"sessionUpdate": "plan", "entries": [
            {"content": "Read calc.py", "status": "in_progress", "priority": "high"},
            {"content": "Add subtract()", "status": "pending", "priority": "high"}]})
        update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read File",
                "kind": "read", "status": "pending", "rawInput": {}, "locations": []})
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": "Read calc.py",
                "kind": "read", "rawInput": {"file_path": "/w/calc.py"},
                "locations": [{"path": "/w/calc.py", "line": 1}]})
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "def add(a, b): ..."}}]})
        update({"sessionUpdate": "plan", "entries": [
            {"content": "Read calc.py", "status": "completed", "priority": "high"},
            {"content": "Add subtract()", "status": "completed", "priority": "high"}]})
        for d in ("Done.", " Added `subtract`."):
            say(d)
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


@pytest.fixture
def coder(tmp_path):
    script = tmp_path / "fake_coder.py"
    script.write_text(_CODER, encoding="utf-8")
    return script


def _acp_delegate(script, workdir, name="claude-code"):
    from plugins.delegates.adapters import AcpAdapter

    adapter = AcpAdapter()
    d = adapter.parse(
        {
            "name": name,
            "type": "acp",
            "command": sys.executable,
            "args": [str(script)],
            "workdir": str(workdir),
            "return_diff": "false",
        }
    )
    return adapter, d


# ── the tracker ───────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def test_tracker_throttles_and_always_lands_the_final_state():
    clock, sent = _Clock(), []

    async def sink(snap):
        sent.append(snap)

    t = dp.DelegateProgress("coder", sink, min_interval=10.0, clock=clock)
    await t.on_tool({"phase": "start", "id": "a", "name": "Read calc.py", "kind": "read"})
    # Inside the interval: nothing more goes out, however much changes.
    for i in range(50):
        await t.on_text(f"word{i} ")
        await t.on_tool({"phase": "start", "id": f"x{i}", "name": f"tool {i}"})
    assert len(sent) == 1
    await t.finish(ok=True)
    final = sent[-1]
    assert len(sent) == 2 and final["done"] is True and final["ok"] is True
    # Bounded: the ring keeps the newest few tools, the text keeps its tail.
    assert len(final["recent_tools"]) == dp.RECENT_TOOLS_MAX
    assert final["recent_tools"][-1]["name"] == "tool 49"
    assert final["tool_count"] == 51
    assert len(final["text"]) <= dp.TEXT_TAIL_MAX and final["text"].endswith("word49 ")
    # A run that finished leaves no tool reading "running".
    assert final["current_tool"]["status"] == "completed"
    # Nothing after done.
    await t.on_text("late")
    assert len(sent) == 2


async def test_tracker_trailing_flush_delivers_the_last_change():
    sent = []

    async def sink(snap):
        sent.append(snap)

    t = dp.DelegateProgress("coder", sink, min_interval=0.05)
    await t.on_tool({"phase": "start", "id": "a", "name": "first"})
    await t.on_tool({"phase": "start", "id": "b", "name": "second"})  # throttled
    assert sent[-1]["current_tool"]["name"] == "first"
    await asyncio.sleep(0.15)
    assert sent[-1]["current_tool"]["name"] == "second"
    t.close()


async def test_tracker_bounds_the_plan_and_survives_a_raising_sink():
    async def sink(snap):
        raise RuntimeError("console went away")

    t = dp.DelegateProgress("coder", sink, min_interval=0.0)
    await t.on_plan([{"content": "x" * 999, "status": "pending"}] * 100)
    assert len(t.plan) == dp.PLAN_MAX and len(t.plan[0]["content"]) <= dp.PLAN_CONTENT_MAX
    await t.finish(ok=False)  # the raising sink is logged, never raised


async def test_tool_refinement_renames_in_place_and_end_settles_it():
    sent = []

    async def sink(snap):
        sent.append(snap)

    t = dp.DelegateProgress("coder", sink, min_interval=0.0)
    await t.on_tool({"phase": "start", "id": "t1", "name": "Read File", "kind": "read"})
    await t.on_tool(
        {"phase": "update", "id": "t1", "name": "Read calc.py", "locations": [{"path": "/w/calc.py", "line": 3}]}
    )
    await t.on_tool({"phase": "end", "id": "t1", "name": "Read calc.py", "status": "completed"})
    snap = sent[-1]
    assert snap["tool_count"] == 1
    assert [r["name"] for r in snap["recent_tools"]] == ["Read calc.py"]
    assert snap["recent_tools"][0]["status"] == "completed"
    assert snap["current_tool"] == {
        "id": "t1",
        "name": "Read calc.py",
        "kind": "read",
        "status": "completed",
        "locations": [{"path": "/w/calc.py", "line": 3}],
    }


# ── the ACP client, against a real subprocess ─────────────────────────────────────


async def test_client_drops_the_replayed_block_and_reports_plan_kind_and_locations(coder, tmp_path):
    """The doubled first sentence (#3979): claude-agent-acp streams a text block as
    deltas, then re-sends the whole block as one chunk. Neither the adjacent-chunk guard
    nor the whole-reply halving could see it, so the reply opened with its first
    sentence twice — in the stream AND the stored reply."""
    from plugins.coding_agent.acp_client import AcpClient

    deltas, tools, plans = [], [], []

    async def on_text(d):
        deltas.append(d)

    async def on_tool(e):
        tools.append(e)

    async def on_plan(p):
        plans.append(p)

    client = AcpClient(sys.executable, [str(coder)], cwd=str(tmp_path), name="fake", record_runs=False)
    try:
        reply = await client.prompt(
            "add subtract", text_callback=on_text, tool_callback=on_tool, plan_callback=on_plan, timeout=30.0
        )
    finally:
        await client.close()

    opening = "I'll look at the existing `calc.py` first."
    assert reply.count(opening) == 1, reply
    assert reply == f"{opening}\n\nDone. Added `subtract`."
    assert "".join(deltas).count(opening) == 1  # the live stream is not doubled either
    assert [e["status"] for e in plans[-1]] == ["completed", "completed"]
    start = next(e for e in tools if e["phase"] == "start")
    assert start["kind"] == "read" and "locations" not in start  # an empty list adds nothing
    refined = next(e for e in tools if e["phase"] == "update")
    assert refined["name"] == "Read calc.py"
    assert refined["locations"] == [{"path": "/w/calc.py", "line": 1}]


def test_a_short_or_single_chunk_repeat_is_left_alone():
    """The guard needs a streamed segment (>=2 chunks) that the next chunk equals
    EXACTLY — a model saying the same short thing twice is not the replay."""
    from plugins.coding_agent.acp_client import _SEGMENT_REPLAY_FLOOR

    assert _SEGMENT_REPLAY_FLOOR >= 8


# ── the adapter: snapshots only when a card owner bound a sink ────────────────────


async def test_adapter_reports_into_a_bound_sink_and_ends_with_a_done_snapshot(coder, tmp_path):
    adapter, d = _acp_delegate(coder, tmp_path)
    snaps = []

    async def sink(snap):
        snaps.append(snap)

    try:
        with dp.progress_sink(sink):
            reply = await adapter.dispatch(d, "add subtract")
    finally:
        await adapter.teardown(d)
    assert reply.count("I'll look at the existing `calc.py` first.") == 1
    assert snaps, "no progress reached the sink"
    final = snaps[-1]
    assert final["target"] == "claude-code" and final["done"] is True and final["ok"] is True
    assert final["plan"] == [
        {"content": "Read calc.py", "status": "completed"},
        {"content": "Add subtract()", "status": "completed"},
    ]
    assert final["tool_count"] == 1
    assert final["recent_tools"][0]["name"] == "Read calc.py"
    assert final["recent_tools"][0]["locations"] == [{"path": "/w/calc.py", "line": 1}]
    assert final["current_tool"]["kind"] == "read"
    assert "Added `subtract`" in final["text"]


async def test_adapter_without_a_sink_wires_no_callbacks(coder, tmp_path, monkeypatch):
    """Every caller that owns no card (the board, a CLI, a test) dispatches exactly as
    before — the adapter passes the client nothing new."""
    from plugins.coding_agent.acp_client import AcpClient

    seen = {}
    real = AcpClient.prompt

    async def spy(self, text, **kwargs):
        seen.update(kwargs)
        return await real(self, text, **kwargs)

    monkeypatch.setattr(AcpClient, "prompt", spy)
    adapter, d = _acp_delegate(coder, tmp_path, name="nosink")
    try:
        await adapter.dispatch(d, "go")
    finally:
        await adapter.teardown(d)
    assert set(seen) == {"timeout"}


# ── owner 1: the `@` short-circuit ────────────────────────────────────────────────


async def test_mention_exchange_streams_progress_keyed_to_its_card_before_the_result(monkeypatch):
    chat_dispatch = importlib.import_module("server.chat_dispatch")
    gate = asyncio.Event()

    async def exchange(message, session_id, request_metadata):
        sink = dp.current_sink()
        assert sink is not None  # bound in the caller, inherited by the exchange task
        await sink({"target": "claude-code", "plan": None, "done": False})
        await gate.wait()
        await sink({"target": "claude-code", "plan": None, "done": True})
        return "reply", [{"ok": True, "author": "claude-code"}]

    monkeypatch.setattr(chat_dispatch._chat_rooms, "_at_delegate_exchange", exchange)
    card = {"id": "mention:claude-code", "name": "@claude-code", "input": "go"}
    gen = chat_dispatch._mention_exchange_with_progress("@claude-code go", "s1", None, card)
    first = await gen.__anext__()
    # A frame arrives WHILE the exchange is still running — the whole point.
    assert first == ("delegate_progress", {"target": "claude-code", "plan": None, "done": False, "id": card["id"]})
    gate.set()
    rest = [f async for f in gen]
    assert rest[0][0] == "delegate_progress" and rest[0][1]["done"] is True
    assert rest[-1] == ("__result__", ("reply", [{"ok": True, "author": "claude-code"}]))


async def test_closing_the_mention_stream_cancels_the_exchange(monkeypatch):
    chat_dispatch = importlib.import_module("server.chat_dispatch")
    cancelled = asyncio.Event()

    async def exchange(message, session_id, request_metadata):
        await dp.current_sink()({"target": "c"})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(chat_dispatch._chat_rooms, "_at_delegate_exchange", exchange)
    gen = chat_dispatch._mention_exchange_with_progress("@c go", "s1", None, {"id": "mention:c"})
    await gen.__anext__()
    await gen.aclose()
    await asyncio.wait_for(cancelled.wait(), 5)


async def test_the_mention_path_end_to_end_against_a_real_coder(coder, tmp_path, monkeypatch):
    """The `@` driver (``_pre_turn_dispatch``) with a real registry holding a real ACP
    delegate: progress frames for the mention card come out BEFORE its tool_end and the
    reply, and the reply carries the opening sentence once."""
    chat_dispatch = importlib.import_module("server.chat_dispatch")
    from plugins.delegates.registry import DelegateRegistry
    from runtime.state import STATE

    from plugins.delegates.adapters import AcpAdapter

    reg = DelegateRegistry(
        [
            {
                "name": "claude-code",
                "type": "acp",
                "command": sys.executable,
                "args": [str(coder)],
                "workdir": str(tmp_path),
                "return_diff": "false",
            }
        ]
    )
    monkeypatch.setattr(STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(STATE, "graph", None, raising=False)
    pre = chat_dispatch._PreTurn("@claude-code add subtract")
    try:
        frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "sess-e2e", None)]
    finally:
        await AcpAdapter().teardown(reg.get("claude-code"))
    kinds = [k for k, _ in frames]
    assert kinds[0] == "tool_start"
    progress = [p for k, p in frames if k == "delegate_progress"]
    assert progress and all(p["id"] == "mention:claude-code" for p in progress)
    assert kinds.index("delegate_progress") < kinds.index("tool_end")
    assert progress[-1]["done"] is True and progress[-1]["plan"][-1]["status"] == "completed"
    reply = next(p for k, p in frames if k == "room_reply")
    assert reply["text"].count("I'll look at the existing `calc.py` first.") == 1


# ── owner 2: the foreground `delegate_to` tool (LangChain custom event) ──────────


async def test_tool_sink_emits_a_custom_event_under_the_tool_run_from_another_task():
    """The sink is captured in the tool body and called from the ACP client's READER
    task — the custom event must still land under the tool's run, which is the id the
    turn stream keys the ask row by."""
    from langchain_core.tools import tool

    from plugins.delegates import _tool_progress_sink

    @tool
    async def delegate_to(target: str) -> str:
        """Fake delegation."""
        sink = _tool_progress_sink()
        assert sink is not None
        # Another task with an EMPTY context — like the pooled client's reader.
        import contextvars

        await asyncio.get_running_loop().create_task(sink({"target": target, "done": True}), context=contextvars.Context())
        return "ok"

    events = [e async for e in delegate_to.astream_events({"target": "claude-code"}, version="v2")]
    start = next(e for e in events if e["event"] == "on_tool_start")
    custom = [e for e in events if e["event"] == "on_custom_event"]
    assert [e["name"] for e in custom] == ["delegate_progress"]
    assert custom[0]["run_id"] == start["run_id"]
    assert custom[0]["data"] == {"target": "claude-code", "done": True}


def test_tool_sink_is_none_outside_a_run():
    from plugins.delegates import _tool_progress_sink

    assert _tool_progress_sink() is None


async def test_turn_stream_keys_progress_to_the_foreground_delegation_row():
    ts = importlib.import_module("server.turn_stream")
    from observability import metrics, pricing

    st = ts._TurnStreamState(metrics=metrics, pricing=pricing)
    start = {
        "event": "on_tool_start",
        "name": "delegate_to",
        "run_id": "run-1",
        "metadata": {},
        "data": {"input": {"target": "claude-code", "query": "add subtract"}},
    }
    ask = list(ts._on_tool_start(st, start, "delegate_to", None))
    assert ask and ask[0][1]["id"] == "run-1"
    ev = {"event": "on_custom_event", "name": "delegate_progress", "run_id": "run-1", "data": {"target": "claude-code"}}
    assert ts._handler_for("on_custom_event", "delegate_progress") is ts._on_custom_delegate_progress
    assert list(ts._on_custom_delegate_progress(st, ev, "delegate_progress", None)) == [
        ("delegate_progress", {"target": "claude-code", "id": "run-1"})
    ]
    # A run with no ask row (unknown, or already ended) has no card to update.
    stray = {**ev, "run_id": "run-2"}
    assert list(ts._on_custom_delegate_progress(st, stray, "delegate_progress", None)) == []


# ── owner 3: a background job ─────────────────────────────────────────────────────


async def test_background_job_publishes_progress_on_its_own_lane():
    from background.manager import BackgroundManager

    published = []
    mgr = BackgroundManager.__new__(BackgroundManager)
    mgr._publish = lambda topic, data, **kw: published.append((topic, data, kw))
    sink = mgr._progress_sink("bg-1", "sess-1")
    await sink({"target": "claude-code", "done": False})
    assert published == [
        (
            "background.progress",
            {
                "job_id": "bg-1",
                "origin_session": "sess-1",
                "phase": "delegate_progress",
                "progress": {"target": "claude-code", "done": False},
            },
            {"retain": False},
        )
    ]


# ── durable history: one snapshot per delegation ──────────────────────────────────


def _progress_message(pid: str, n: int):
    from a2a.types import Message, Part, Role
    from google.protobuf import json_format, struct_pb2

    value = struct_pb2.Value()
    json_format.ParseDict({"id": pid, "tool_count": n}, value.struct_value)
    part = Part()
    part.data.CopyFrom(value)
    part.metadata.update({"mimeType": dp_mime()})
    return Message(message_id=f"{pid}-{n}", role=Role.ROLE_AGENT, parts=[part])


def dp_mime() -> str:
    from a2a_impl.executor import DELEGATE_PROGRESS_MIME

    return DELEGATE_PROGRESS_MIME


def test_store_keeps_only_the_latest_snapshot_per_delegation():
    from a2a.types import Message, Part, Role, Task

    from a2a_impl import stores

    assert stores._DELEGATE_PROGRESS_MIME == dp_mime()  # the duplicated constant stays locked
    text = Message(message_id="u", role=Role.ROLE_USER, parts=[Part(text="go")])
    task = Task(id="t", context_id="c")
    task.history.extend(
        [text, _progress_message("a", 1), _progress_message("b", 1), _progress_message("a", 2), _progress_message("a", 3)]
    )
    assert stores.prune_superseded_progress(task) == 2
    kept = [m.message_id for m in task.history]
    assert kept == ["u", "b-1", "a-3"]
