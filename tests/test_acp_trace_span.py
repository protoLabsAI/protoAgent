"""Fleet tracing for ACP coders: `AcpClient.prompt` wraps every coder run in an
`acp:<name>` agent observation, and each finished coder tool call lands as an
EXPLICIT child of it — the reader task that sees the tool events does not carry
the span's context, so ambient nesting would orphan them into stray traces.

The project board dispatches coders from a background loop, outside any turn, so
before this a coder run never reached Langfuse at all."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

from observability import tracing
from plugins.coding_agent.acp_client import AcpClient, _coder_session_id

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
    fake.started_under_valid_parent = []

    def _start(**_kw):
        from opentelemetry import trace as otel_trace

        fake.started_under_valid_parent.append(otel_trace.get_current_span().get_span_context().is_valid)
        return cm

    fake.start_as_current_observation.side_effect = _start

    # Each tool call gets its OWN child span (opened at its start event, updated on a
    # refinement, ended at its end event), so they're recorded in order.
    span.children = []

    def _child(**kwargs):
        child = MagicMock()
        child.start_kwargs = kwargs
        span.children.append(child)
        return child

    span.start_observation.side_effect = _child
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return fake, span


def _tool_spans(span) -> list[dict]:
    """Each tool span's final fields: its start kwargs overlaid with every update, in
    order, plus whether it was ended."""
    out = []
    for child in span.children:
        fields = dict(child.start_kwargs)
        for call in child.update.call_args_list:
            fields.update(call.kwargs)
        fields["ended"] = child.end.called
        out.append(fields)
    return out


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
    tool, terminal, env = _tool_spans(span)
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
    for tool in _tool_spans(span):
        assert tool["input"] == {"input": ""} and tool["output"] == ""


# claude-agent-acp's streaming shape: the call opens at content_block_start with no
# arguments and a generic title, and a later ``tool_call_update`` (REFINE) refines both.
_STREAMING_AGENT = r"""
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
        update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read File",
                "kind": "read", "status": "pending", "rawInput": {}})
        update(REFINE)
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


async def _run_streaming(tmp_path, refine: dict) -> None:
    script = tmp_path / "streaming_agent.py"
    script.write_text(
        f"REFINE = {json.dumps(refine)!r}\nimport json as _j\nREFINE = _j.loads(REFINE)\n" + _STREAMING_AGENT,
        encoding="utf-8",
    )
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="opus", record_runs=False)
    try:
        await client.prompt("fix it", timeout=30.0)
    finally:
        await client.close()


async def test_streamed_tool_call_records_the_refined_arguments(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    await _run_streaming(
        tmp_path,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "title": "Read src/app.py",
            "kind": "read",
            "rawInput": {"file_path": "src/app.py", "api_key": "hunter2"},
        },
    )

    (tool,) = _tool_spans(span)
    # The refined title and arguments — not "Read File" / {"input": "read"}.
    assert tool["name"] == "tool:Read src/app.py"
    assert tool["input"] == {"input": '{"file_path": "src/app.py", "api_key": "[REDACTED]"}'}


def _capture_propagation(monkeypatch) -> list[dict]:
    import langfuse

    calls: list[dict] = []

    def fake_propagate(**kwargs):
        calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)
    return calls


async def test_root_coder_run_carries_a_session_tags_and_input(fake_agent, tmp_path, fake_langfuse, monkeypatch):
    _fake, span = fake_langfuse
    # No current observation: dispatched outside any turn, as the board does.
    monkeypatch.setenv("AGENT_NAME", "designSystem")
    calls = _capture_propagation(monkeypatch)

    await _run(fake_agent, tmp_path)

    assert calls == [{"session_id": _coder_session_id("codex", str(tmp_path)), "tags": ["designSystem"]}]
    assert calls[0]["session_id"].startswith(f"coder:codex:{tmp_path.name}:")
    assert span.update.call_args_list[0].kwargs == {"input": "fix it"}


def _span_from(scope: str):
    """A real SDK span from tracer ``scope``: ``langfuse-sdk`` is what the Langfuse SDK
    exports; ``a2a-python-sdk`` is the a2a-sdk's handler span, which it drops."""
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider().get_tracer(scope).start_as_current_span("outer")


async def test_nested_coder_run_leaves_the_turns_session_alone(fake_agent, tmp_path, fake_langfuse, monkeypatch):
    fake, span = fake_langfuse
    calls = _capture_propagation(monkeypatch)

    with _span_from("langfuse-sdk"):  # inside a traced turn
        await _run(fake_agent, tmp_path)

    assert calls == []
    assert span.update.call_args_list[0].kwargs == {"input": "fix it"}
    # ...and it nests: the span is NOT started from a fresh context.
    assert fake.started_under_valid_parent == [True]


async def test_a_foreign_otel_span_does_not_make_the_run_a_child(fake_agent, tmp_path, fake_langfuse, monkeypatch):
    """#3696: an a2a-sdk handler span is current but never exported. The run is still a
    ROOT: it gets the session and tags, and starts from an empty context rather than
    nesting under a parent that never reaches Langfuse."""
    fake, _span = fake_langfuse
    calls = _capture_propagation(monkeypatch)

    with _span_from("a2a-python-sdk"):
        await _run(fake_agent, tmp_path)

    assert calls and calls[0]["session_id"].startswith("coder:codex:")
    assert fake.started_under_valid_parent == [False]


def test_in_active_trace_counts_only_spans_langfuse_exports(fake_langfuse):
    assert tracing.in_active_trace() is False
    with _span_from("langfuse-sdk"):
        assert tracing.in_active_trace() is True
    with _span_from("a2a-python-sdk"):
        assert tracing.in_active_trace() is False


def test_a_sampled_out_turn_still_counts_so_the_coder_run_drops_with_it(fake_langfuse):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    dropped = TracerProvider(sampler=TraceIdRatioBased(0)).get_tracer("langfuse-sdk")
    with dropped.start_as_current_span("turn"):
        assert tracing.in_active_trace() is True


def test_a_remote_only_context_is_not_a_local_trace(fake_langfuse):
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext

    remote = NonRecordingSpan(SpanContext(trace_id=0xA1, span_id=0xB2, is_remote=True))
    with otel_trace.use_span(remote):
        assert tracing.in_active_trace() is False


def test_a_pre_v4_sdk_without_the_export_filter_counts_every_span(fake_langfuse, monkeypatch):
    monkeypatch.setitem(sys.modules, "langfuse.span_filter", None)  # import → ImportError
    with _span_from("a2a-python-sdk"):
        assert tracing.in_active_trace() is True


async def test_incognito_coder_run_sends_no_input(fake_agent, tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    token = tracing._io_suppressed_ctx.set(True)
    try:
        await _run(fake_agent, tmp_path)
    finally:
        tracing._io_suppressed_ctx.reset(token)

    assert all("input" not in c.kwargs for c in span.update.call_args_list)


def test_coder_session_keeps_same_named_worktrees_apart():
    a = _coder_session_id("opus", "/box/projects/protoContent/.worktrees/feat-x")
    b = _coder_session_id("opus", "/box/projects/protoAgent/.worktrees/feat-x")
    assert a != b
    assert a == _coder_session_id("opus", "/box/projects/protoContent/.worktrees/feat-x")  # retries group
    # Fits the trace-attribute cap whole, however deep the worktree sits.
    assert len(_coder_session_id("opus", "/" + "d" * 400 + "/feat-x")) <= tracing._PROPAGATED_VALUE_MAX


async def test_update_with_inline_title_args_refines_the_input(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    # No rawInput on the update: the args ride inline in the title.
    await _run_streaming(
        tmp_path,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "kind": "execute",
            "title": 'Run {"cmd": "make", "api_key": "hunter2"}',
        },
    )

    (tool,) = _tool_spans(span)
    assert tool["input"] == {"input": '{"cmd": "make", "api_key": "[REDACTED]"}'}


# Replays the update sequence in the JSON file argv[1] names (a file, not argv itself:
# the pathological-title case is far past Windows' 32k command-line limit).
_REPLAY_AGENT = r"""
import sys, json
with open(sys.argv[1], encoding="utf-8") as f:
    UPDATES = json.load(f)

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
        for u in UPDATES:
            update(u)
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""

_KEY = "sk-" + "A" * 48
_OPEN = {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Terminal", "kind": "execute", "rawInput": {}}
_EXPORT_KEY = [
    _OPEN,
    {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "t1",
        "title": f"`export OPENAI_API_KEY={_KEY}`",
        "rawInput": {"command": f"export OPENAI_API_KEY={_KEY}"},
    },
    {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
]


async def _replay(tmp_path, updates: list[dict], events: list | None = None) -> None:
    script = tmp_path / "replay_agent.py"
    script.write_text(_REPLAY_AGENT, encoding="utf-8")
    updates_file = tmp_path / "updates.json"
    updates_file.write_text(json.dumps(updates), encoding="utf-8")

    async def on_tool(event: dict) -> None:
        if events is not None:
            events.append(event)

    client = AcpClient(
        sys.executable, [str(script), str(updates_file)], cwd=str(tmp_path), name="claude", record_runs=False
    )
    try:
        await client.prompt("go", tool_callback=on_tool, timeout=30.0)
    finally:
        await client.close()


async def test_a_refined_title_is_redacted_in_the_span_name(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    await _replay(tmp_path, _EXPORT_KEY)

    kw = _tool_spans(span)[-1]
    # The title is the whole command line: the name is content, redacted like the input.
    assert "A" * 20 not in kw["name"]
    assert "A" * 20 not in kw["input"]["input"]


async def test_incognito_sends_no_command_in_the_span_name(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    token = tracing._io_suppressed_ctx.set(True)
    try:
        await _replay(tmp_path, _EXPORT_KEY)
    finally:
        tracing._io_suppressed_ctx.reset(token)

    kw = _tool_spans(span)[-1]
    assert kw["input"] == {"input": ""}
    assert "export" not in kw["name"]


async def test_a_repeated_title_fragment_keeps_the_refined_input(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    title = "`awk '{print $1}' f`"
    await _replay(
        tmp_path,
        [
            _OPEN,
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": title, "rawInput": {"command": "awk"}},
            # The completion repeats the title, whose brace fragment is not JSON.
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": title, "status": "completed"},
        ],
    )

    assert _tool_spans(span)[-1]["input"] == {"input": '{"command": "awk"}'}


async def test_a_pathological_title_still_ends_the_tool(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    events: list[dict] = []
    deep = 'x {"a": ' + "[" * 100000
    await _replay(
        tmp_path,
        [_OPEN, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": deep, "status": "completed"}],
        events,
    )

    assert [e["phase"] for e in events] == ["start", "end"]
    assert span.start_observation.call_count == 1


# ─── #3691: the refinement reaches the chat card too, as an `update` event ─────────────


async def test_a_refinement_is_emitted_as_an_update_for_the_ui(tmp_path, monkeypatch):
    # Tracing OFF: the card fix must not depend on Langfuse.
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_langfuse", None)
    events: list[dict] = []
    await _replay(
        tmp_path,
        [
            _OPEN,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "title": "Run make test",
                "rawInput": {"command": "make test"},
            },
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
        ],
        events,
    )

    assert [e["phase"] for e in events] == ["start", "update", "end"]
    assert events[2]["name"] == "Run make test"  # the end is named after the refined card
    update = events[1]
    assert update == {"phase": "update", "id": "t1", "name": "Run make test", "input": '{"command": "make test"}'}


async def test_no_update_for_an_ended_or_unknown_call(tmp_path, fake_langfuse):
    events: list[dict] = []
    await _replay(
        tmp_path,
        [
            _OPEN,
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
            # Arrives after the end: the card is closed, nothing to fill in.
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": "late", "rawInput": {"a": 1}},
            # Never started: no card to update.
            {"sessionUpdate": "tool_call_update", "toolCallId": "ghost", "title": "x", "rawInput": {"a": 1}},
        ],
        events,
    )

    assert [e["phase"] for e in events] == ["start", "end"]


# ─── trace fidelity: timing, usage/cost, failure output, reasoning, shutdown ───────────

# A scriptable agent: argv[1] is a JSON file {"new": {...}, "updates": [...],
# "prompt_result": {...}} or {..., "prompt_error": {"code": ..., "message": ...}}.
_SCRIPTED_AGENT = r"""
import sys, json
with open(sys.argv[1], encoding="utf-8") as f:
    SPEC = json.load(f)

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1", **SPEC.get("new", {})}})
    elif method == "session/prompt":
        for u in SPEC.get("updates", []):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": u}})
        if "prompt_error" in SPEC:
            send({"jsonrpc": "2.0", "id": mid, "error": SPEC["prompt_error"]})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": SPEC.get("prompt_result", {"stopReason": "end_turn"})})
"""


async def _scripted(tmp_path, spec: dict):
    script = tmp_path / "scripted_agent.py"
    script.write_text(_SCRIPTED_AGENT, encoding="utf-8")
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    client = AcpClient(sys.executable, [str(script), str(spec_file)], cwd=str(tmp_path), name="opus", record_runs=False)
    try:
        return await client.prompt("fix it", timeout=30.0)
    finally:
        await client.close()


def _msg(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


async def test_a_tool_span_opens_at_its_start_and_closes_at_its_end(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    await _scripted(
        tmp_path,
        {
            "updates": [
                {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run tests", "rawInput": {"cmd": "pytest"}},
                _msg("working"),
                {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed"},
            ]
        },
    )

    (child,) = span.children
    assert child.start_kwargs["name"] == "tool:Run tests" and child.start_kwargs["as_type"] == "tool"
    assert child.end.call_count == 1
    assert child.update.call_args.kwargs["level"] == "ERROR"


async def test_a_tool_still_open_at_turn_end_is_closed_as_unfinished(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    await _scripted(
        tmp_path,
        {"updates": [{"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run", "rawInput": {}}, _msg("x")]},
    )

    (tool,) = _tool_spans(span)
    assert tool["ended"] and tool["level"] == "WARNING"
    assert "turn ended" in tool["status_message"]


async def test_the_coders_reported_usage_and_cost_become_a_generation(tmp_path, fake_langfuse):
    fake, _span = fake_langfuse
    await _scripted(
        tmp_path,
        {
            "new": {"models": {"currentModelId": "claude-opus-5-5"}},
            "updates": [
                _msg("done"),
                {
                    "sessionUpdate": "usage_update",
                    "used": 900,
                    "size": 200000,
                    "cost": {"amount": 0.42, "currency": "USD"},
                },
            ],
            "prompt_result": {
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cachedReadTokens": 700,
                    "cachedWriteTokens": 30,
                    "totalTokens": 880,
                },
            },
        },
    )

    gen = fake.start_observation.call_args.kwargs
    assert gen["as_type"] == "generation" and gen["name"] == "acp:opus-model"
    assert gen["model"] == "claude-opus-5-5"
    assert gen["usage_details"] == {
        "input": 100,
        "output": 50,
        "input_cache_read": 700,
        "input_cache_creation": 30,
        "total": 880,
    }
    assert gen["cost_details"] == {"total": 0.42}
    assert gen["metadata"]["cost_basis"] == "API-equivalent"


async def test_the_model_is_read_from_the_newer_config_options_shape(tmp_path, fake_langfuse):
    fake, _span = fake_langfuse
    await _scripted(
        tmp_path,
        {
            "new": {"configOptions": [{"id": "mode", "currentValue": "auto"}, {"id": "model", "currentValue": "opus"}]},
            "updates": [_msg("done")],
            "prompt_result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            },
        },
    )
    assert fake.start_observation.call_args.kwargs["model"] == "opus"


async def test_no_generation_when_the_agent_reports_nothing(fake_agent, tmp_path, fake_langfuse):
    fake, _span = fake_langfuse
    await _run(fake_agent, tmp_path)
    fake.start_observation.assert_not_called()


async def test_a_failed_run_records_why_as_its_output(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    with pytest.raises(Exception):
        await _scripted(tmp_path, {"prompt_error": {"code": -32000, "message": "model overloaded"}})

    outcome = span.update.call_args.kwargs
    assert outcome["level"] == "ERROR"
    assert outcome["output"].startswith("[failed] AcpError:") and "model overloaded" in outcome["output"]


async def test_reasoning_tail_and_plan_ride_on_the_run_span(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    await _scripted(
        tmp_path,
        {
            "updates": [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "key sk-" + "Q" * 40 + " then fix"},
                },
                {
                    "sessionUpdate": "plan",
                    "entries": [{"content": "fix bug", "status": "in_progress", "priority": "high"}],
                },
                _msg("done"),
            ]
        },
    )

    md = span.update.call_args.kwargs["metadata"]
    assert md["plan"] == [{"content": "fix bug", "status": "in_progress", "priority": "high"}]
    assert md["reasoning_tail"].endswith("then fix") and "Q" * 40 not in md["reasoning_tail"]


async def test_incognito_sends_no_reasoning(tmp_path, fake_langfuse):
    _fake, span = fake_langfuse
    token = tracing._io_suppressed_ctx.set(True)
    try:
        await _scripted(
            tmp_path,
            {
                "updates": [
                    {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret plan"}},
                    _msg("x"),
                ]
            },
        )
    finally:
        tracing._io_suppressed_ctx.reset(token)

    assert "reasoning_tail" not in span.update.call_args.kwargs["metadata"]


def test_end_open_spans_ends_what_is_still_running(fake_langfuse):
    _fake, span = fake_langfuse
    with tracing.trace_span("acp:opus", as_type="agent"):
        assert tracing.end_open_spans("restart") == 1
        span.update.assert_called_with(level="WARNING", status_message="not finished: restart")
        span.end.assert_called_once()
    assert tracing.end_open_spans("again") == 0  # the block's own exit untracked nothing twice
