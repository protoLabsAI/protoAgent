"""Streamed tool arguments → tool-args-v1 DataPart (ADR 0118 D3, server half, #4078).

Three layers are covered here:

* the partial-JSON string scanner in ``server/turn_stream.py`` (``_ArgStream``) — escapes,
  a chunk split mid-escape, the arg appearing after other keys, and a non-string value
  emitting nothing;
* the turn stream: a fake model streaming a ``stream_args`` tool's call yields ordered
  ``tool_args`` frames whose concatenated chunks equal the final value, ending ``done:True``
  — and a tool WITHOUT the metadata yields none, while the tool-call-v1 card is unchanged;
* the durable store: tool-args previews are dropped from task history (live-only).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

import server.turn_stream as turn_stream
from tests._turn_driver_fakes import Clock, ScriptedGraph, chunk, model_end, model_start

# ── (a) the partial-JSON string scanner ─────────────────────────────────────────


def _value(arg: str, *fragments: str) -> turn_stream._ArgStream:
    """Feed ``fragments`` of raw tool-call-chunk JSON into a fresh scanner. ``feed`` never
    flushes, so ``.pending`` holds the whole decoded value so far."""
    s = turn_stream._ArgStream(arg=arg)
    for f in fragments:
        s.feed(f)
    return s


def test_scanner_decodes_escapes():
    # \n, \t, \", \\ and A (= "A"), all in one value.
    raw = r'{"code":"a\nb\t\"q\\A"}'
    s = _value("code", raw)
    assert s.pending == 'a\nb\t"q\\A'
    assert s.done is True


def test_scanner_holds_back_a_split_unicode_escape():
    # The A straddles the chunk boundary: the first feed must hold back at the
    # backslash and emit nothing past it, the second completes it to "A".
    s = _value("code", '{"code":"x\\u00', '41y"}')
    assert s.pending == "xAy"
    assert s.done is True


def test_scanner_holds_back_a_split_simple_escape():
    s = _value("code", '{"code":"a\\', 'nb"}')
    assert s.pending == "a\nb"
    assert s.done is True


def test_scanner_finds_arg_after_other_keys():
    s = _value("code", '{"kind": "html", "title": "T", "code": "<p>hi</p>"}')
    assert s.pending == "<p>hi</p>"
    assert s.done is True


@pytest.mark.parametrize(
    "raw",
    ['{"code": 123}', '{"code": {"x": 1}}', '{"code": [1, 2]}', '{"code": true}', '{"code": null}'],
)
def test_scanner_emits_nothing_for_a_non_string_value(raw):
    s = _value("code", raw)
    assert s.pending == ""
    assert s.done is False
    assert s.dead is True  # resolved as not-a-string ⇒ no frames ever


def test_scanner_stays_silent_until_the_arg_arrives():
    # Only other keys so far — the arg hasn't appeared, so nothing is extractable yet and
    # the scanner has not given up.
    s = _value("code", '{"kind": "html"')
    assert s.pending == "" and s.done is False and s.dead is False


# ── (b) the turn stream ──────────────────────────────────────────────────────────


def _tool(name: str, **metadata) -> SimpleNamespace:
    return SimpleNamespace(name=name, metadata=dict(metadata))


def _tc_chunk(run: str, *, tcid=None, name=None, args: str = "", index: int = 0) -> dict:
    """An ``on_chat_model_stream`` event carrying one streamed tool-call-chunk fragment."""
    msg = AIMessageChunk(content="", tool_call_chunks=[{"id": tcid, "name": name, "args": args, "index": index}])
    return chunk(run, msg)


@pytest.fixture
def env(monkeypatch):
    from observability import metrics, pricing

    import runtime.state as rs

    clock = Clock()
    monkeypatch.setattr(turn_stream, "time", clock)
    monkeypatch.setattr(metrics, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(pricing, "cost_usd", lambda model, usage: 0.0)
    for attr, val in {"background_mgr": None, "goal_controller": None}.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    class Env:
        pass

    e = Env()
    e.clock = clock

    def install(events, *, tools=()):
        g = ScriptedGraph([list(events)])
        g.bound_tools = list(tools)
        monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
        return g

    e.install = install
    return e


def _stream():
    return turn_stream._run_turn_stream("hi", "s-sa", {"configurable": {"thread_id": "t-sa"}})


def _artifact_script(clock) -> list:
    """A show_artifact call whose ``code`` arg streams in four fragments (one escape),
    with clock ticks so the time-floor flush fires between them."""
    return [
        model_start("m1"),
        _tc_chunk("m1", tcid="tc1", name="show_artifact", args='{"kind": "html", "code": "', index=0),
        clock.tick(0.3),
        _tc_chunk("m1", args="<h1>", index=0),
        clock.tick(0.3),
        _tc_chunk("m1", args="Hi\\n", index=0),
        clock.tick(0.3),
        _tc_chunk("m1", args='</h1>"}', index=0),
        model_end("m1", tool_calls=[("tc1", "show_artifact", {"kind": "html", "code": "<h1>Hi\n</h1>"})], usage=(10, 1, 0, 0)),
    ]


@pytest.mark.asyncio
async def test_stream_args_tool_yields_ordered_frames_concatenating_to_the_value(env):
    env.install(_artifact_script(env.clock), tools=[_tool("show_artifact", stream_args="code")])
    frames = [f async for f in _stream()]

    targs = [p for k, p in frames if k == "tool_args"]
    assert len(targs) >= 2  # the ticks force more than one frame
    # Concatenated chunks reconstruct the exact final value, escapes decoded.
    assert "".join(p["chunk"] for p in targs) == "<h1>Hi\n</h1>"
    # Ordered, contiguous offsets, carrying the tool-call id and the streamed arg name.
    offset = 0
    for p in targs:
        assert p["id"] == "tc1" and p["arg"] == "code"
        assert p["offset"] == offset
        offset += len(p["chunk"])
    # Ends with exactly one done:True frame; every earlier frame is done:False.
    assert targs[-1]["done"] is True
    assert all(p["done"] is False for p in targs[:-1])


@pytest.mark.asyncio
async def test_tool_without_stream_args_metadata_yields_no_tool_args_frames(env):
    # Same stream, but the tool declares no stream_args (empty bound_tools).
    env.install(_artifact_script(env.clock), tools=[])
    frames = [f async for f in _stream()]
    assert [k for k, _ in frames if k == "tool_args"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [[_tool("show_artifact", stream_args="code")], []])
async def test_tool_call_v1_card_is_unchanged_by_streaming(env, tools):
    # The early tool_start card (tool-call-v1) still fires with the id + name, whether or
    # not the tool streams its args — the preview rides alongside it, never replaces it.
    env.install(_artifact_script(env.clock), tools=tools)
    frames = [f async for f in _stream()]
    starts = [p for k, p in frames if k == "tool_start"]
    assert any(p.get("id") == "tc1" and p.get("name") == "show_artifact" for p in starts)


# ── (c) the durable store drops previews (live-only) ─────────────────────────────


def _tool_args_part(**payload):
    from a2a_impl.executor import TOOL_ARGS_MIME, _data_part_proto

    return _data_part_proto(payload, TOOL_ARGS_MIME)


def _tool_args_msg(**payload):
    from a2a.types import a2a_pb2

    return a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, parts=[_tool_args_part(**payload)])


def test_drop_tool_args_history_removes_only_pure_previews():
    from a2a.types import a2a_pb2

    from a2a_impl.stores import drop_tool_args_history

    task = a2a_pb2.Task(id="t", context_id="c")
    task.history.append(a2a_pb2.Message(role=a2a_pb2.ROLE_USER, parts=[a2a_pb2.Part(text="draw a chart")]))
    task.history.append(_tool_args_msg(id="tc1", arg="code", offset=0, chunk="<sv", done=False))
    task.history.append(_tool_args_msg(id="tc1", arg="code", offset=3, chunk="g/>", done=True))
    # A message mixing a preview with other parts is content, not a pure preview — keep it.
    mixed = a2a_pb2.Message(
        role=a2a_pb2.ROLE_AGENT,
        parts=[_tool_args_part(id="tc1", arg="code", offset=0, chunk="x", done=True), a2a_pb2.Part(text="done")],
    )
    task.history.append(mixed)
    task.history.append(a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, parts=[a2a_pb2.Part(text="the answer")]))

    removed = drop_tool_args_history(task)

    assert removed == 2
    assert len(task.history) == 3
    assert task.history[0].parts[0].text == "draw a chart"
    assert len(task.history[1].parts) == 2  # the mixed message is untouched
    assert task.history[2].parts[0].text == "the answer"
    # Idempotent: the SDK TaskManager re-saves its one Task per event.
    assert drop_tool_args_history(task) == 0


def test_tool_args_mime_constant_matches_executor():
    from a2a_impl import stores
    from a2a_impl.executor import TOOL_ARGS_MIME

    assert stores._TOOL_ARGS_MIME == TOOL_ARGS_MIME


# ── (d) the executor relays tool_args on live WORKING frames ─────────────────────


@pytest.mark.asyncio
async def test_tool_args_frames_ride_working_dataframes_never_the_terminal_artifact(monkeypatch):
    """Through the real a2a-sdk: a ``tool_args`` frame becomes a tool-args-v1 DataPart on a
    WORKING status message (the console's preview channel), and never rides the terminal
    answer artifact — so a plain re-fetch sees only the finished answer."""
    import asyncio
    import json

    import httpx

    from a2a_impl.executor import TOOL_ARGS_MIME, set_progress_hook, set_terminal_hook
    from tests.test_a2a_handler import A2A_HEADERS, _HANDLERS, _build_app

    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)
    set_terminal_hook(None)
    set_progress_hook(None)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "tc1", "name": "show_artifact", "input": ""})
        yield ("tool_args", {"id": "tc1", "arg": "code", "offset": 0, "chunk": "<sv", "done": False})
        yield ("tool_args", {"id": "tc1", "arg": "code", "offset": 3, "chunk": "g/>", "done": True})
        yield ("done", "the chart is ready")

    app = _build_app(stream)
    previews, artifact_previews = [], []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
            async with c.stream(
                "POST",
                "/a2a",
                headers=A2A_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": "s",
                    "method": "SendStreamingMessage",
                    "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "draw it"}]}},
                },
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    result = json.loads(line[5:].strip()).get("result", {})
                    status = result.get("statusUpdate", {}).get("status", {})
                    for part in status.get("message", {}).get("parts", []):
                        if (part.get("metadata") or {}).get("mimeType") == TOOL_ARGS_MIME:
                            previews.append(part.get("data", {}))
                    for part in result.get("artifactUpdate", {}).get("artifact", {}).get("parts", []):
                        if (part.get("metadata") or {}).get("mimeType") == TOOL_ARGS_MIME:
                            artifact_previews.append(part)
    finally:
        for handler in _HANDLERS:
            reg = getattr(handler, "_active_task_registry", None)
            tasks = set(getattr(reg, "_cleanup_tasks", ()) or ())
            if tasks:
                await asyncio.wait(tasks, timeout=5)
        _HANDLERS.clear()
        set_terminal_hook(None)
        set_progress_hook(None)

    assert [p.get("chunk") for p in previews] == ["<sv", "g/>"]
    assert previews[-1].get("done") is True
    assert artifact_previews == []  # previews never ride the terminal artifact
