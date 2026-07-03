"""Tests for the A2A 1.0 port: protolabs_a2a conventions + ProtoAgentExecutor.

The hand-rolled ``a2a_handler.py`` (JSON-RPC/SSE/task-store/push by hand) was
replaced by ``a2a-sdk`` 1.0 + two thin layers:

  - ``protolabs_a2a`` — the fleet conventions (the four custom DataPart
    extensions, the 1.0 member-discriminated Part shape, the agent card).
  - ``executor.ProtoAgentExecutor`` — bridges protoagent's LangGraph stream
    (``(event_type, payload)`` tuples) onto the SDK's event queue.
  - ``a2a_impl.auth`` — request-time bearer / X-API-Key / origin enforcement.

These tests assert the same behaviors the hand-rolled handler guaranteed, now
in the 1.0 shapes:

  - terminal artifact carries the accumulated text + the cost / confidence /
    worldstate-delta DataParts, in order;
  - tool events surface as tool-call-v1 DataParts on working status frames;
  - input_required parks the task (non-terminal) carrying the question;
  - errors land the task FAILED; cancel lands it CANCELED;
  - the terminal hook fires a ``TurnOutcome`` for telemetry + the Activity feed;
  - bearer / X-API-Key / origin are enforced on /a2a.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

import protolabs_a2a as pa
from a2a_impl.executor import _CONTEXT_MIME, ProtoAgentExecutor, TurnOutcome, set_terminal_hook

A2A_HEADERS = {"A2A-Version": "1.0"}


# ── protolabs_a2a: parts ──────────────────────────────────────────────────────


def test_text_part_member_discriminated_shape():
    assert pa.text_part("hello") == {"content": {"$case": "text", "value": "hello"}}
    assert pa.read_text(pa.text_part("hello")) == "hello"


def test_data_part_member_discriminated_shape():
    dp = pa.data_part({"a": 1}, "application/x")
    assert dp == {
        "content": {"$case": "data", "value": {"a": 1}},
        "metadata": {"mimeType": "application/x"},
        "filename": "",
        "mediaType": "application/json",
    }
    assert pa.read_data(dp) == ("application/x", {"a": 1})


def test_read_data_accepts_flattened_proto_json():
    """The SDK's protobuf serializer flattens content.$case to a top-level
    `data` field; read_data must parse both encodings so a part produced by
    either runtime round-trips."""
    assert pa.read_data({"data": {"b": 2}, "metadata": {"mimeType": "m"}}) == ("m", {"b": 2})
    assert pa.read_text({"text": "x"}) == "x"


def test_read_data_returns_none_for_non_data_part():
    assert pa.read_data(pa.text_part("hi")) == (None, None)


# ── protolabs_a2a: the four extensions ────────────────────────────────────────


def test_cost_extension_mime_uri_and_payload():
    assert pa.COST_MIME == "application/vnd.protolabs.cost-v1+json"
    assert pa.COST_EXT_URI == "https://proto-labs.ai/a2a/ext/cost-v1"
    part = pa.emit_cost(
        {"input_tokens": 1500, "output_tokens": 420},
        duration_ms=900,
        cost_usd=0.0123,
        success=True,
    )
    assert part["metadata"]["mimeType"] == pa.COST_MIME
    payload = pa.parse_cost(part)
    assert payload["usage"] == {"input_tokens": 1500, "output_tokens": 420}
    assert payload["durationMs"] == 900
    assert payload["costUsd"] == 0.0123
    assert payload["success"] is True


def test_cost_omits_costusd_when_not_supplied():
    part = pa.emit_cost({"input_tokens": 10, "output_tokens": 5}, duration_ms=10)
    assert "costUsd" not in pa.parse_cost(part)


def test_confidence_extension_mime_uri_and_payload():
    assert pa.CONFIDENCE_MIME == "application/vnd.protolabs.confidence-v1+json"
    assert pa.CONFIDENCE_EXT_URI == "https://proto-labs.ai/a2a/ext/confidence-v1"
    part = pa.emit_confidence(0.9, explanation="sure", success=True)
    assert pa.parse_confidence(part) == {"confidence": 0.9, "explanation": "sure", "success": True}


def test_worldstate_delta_mime_and_uri_both_carry_v1():
    """The MIME (...worldstate-delta-v1+json) and the card URI
    (.../worldstate-delta-v1) both carry -v1, matching the other three
    extensions. Locking this prevents a silent interop break."""
    assert pa.WORLDSTATE_DELTA_MIME == "application/vnd.protolabs.worldstate-delta-v1+json"
    assert pa.WORLDSTATE_DELTA_EXT_URI == "https://proto-labs.ai/a2a/ext/worldstate-delta-v1"
    part = pa.emit_worldstate_delta([{"domain": "board", "path": "x", "op": "inc", "value": 1}])
    assert part["metadata"]["mimeType"] == pa.WORLDSTATE_DELTA_MIME
    assert pa.parse_worldstate_delta(part)["deltas"][0]["op"] == "inc"


def test_tool_call_extension_mime_uri_and_payload():
    assert pa.TOOL_CALL_MIME == "application/vnd.protolabs.tool-call-v1+json"
    assert pa.TOOL_CALL_EXT_URI == "https://proto-labs.ai/a2a/ext/tool-call-v1"
    part = pa.emit_tool_call("id1", "file_bug", "completed", args={"x": 1}, result="ok")
    assert pa.parse_tool_call(part) == {
        "toolCallId": "id1",
        "name": "file_bug",
        "phase": "completed",
        "args": {"x": 1},
        "result": "ok",
    }


def test_parsers_return_none_on_mime_mismatch():
    cost = pa.emit_cost({"input_tokens": 1, "output_tokens": 1})
    assert pa.parse_confidence(cost) is None
    assert pa.parse_worldstate_delta(cost) is None
    assert pa.parse_tool_call(cost) is None


# ── protolabs_a2a: agent card ─────────────────────────────────────────────────


def _skill() -> AgentSkill:
    return AgentSkill(id="chat", name="Chat", description="general chat", tags=["t"])


def test_build_agent_card_applies_conventions():
    card = pa.build_agent_card(
        name="protoagent",
        description="d",
        url="http://h/a2a",
        version="1.0.0",
        skills=[_skill()],
        bearer=True,
    )
    j = MessageToDict(card)
    assert j["provider"] == {"organization": "protoLabs AI", "url": "https://protolabs.ai"}
    iface = j["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC" and iface["protocolVersion"] == "1.0"
    assert iface["url"].endswith("/a2a")
    declared = {e["uri"] for e in j["capabilities"]["extensions"]}
    assert declared == set(pa.ALL_EXTENSION_URIS)
    assert set(j["securitySchemes"]) == {"apiKey", "bearer"}


def test_build_agent_card_omits_bearer_when_not_configured():
    card = pa.build_agent_card(
        name="a",
        description="d",
        url="http://h/a2a",
        version="1.0.0",
        skills=[_skill()],
        bearer=False,
    )
    j = MessageToDict(card)
    assert set(j["securitySchemes"]) == {"apiKey"}


# ── ProtoAgentExecutor end-to-end (through a2a-sdk) ───────────────────────────


def _build_app(stream_fn, *, bearer=None, api_key="", allowed_origins=None, task_store=None):
    """Mount a real a2a-sdk app driven by ProtoAgentExecutor(stream_fn)."""
    card = pa.build_agent_card(
        name="test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[_skill()],
        bearer=bool(bearer),
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=task_store if task_store is not None else InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    app = FastAPI()
    if bearer is not None or api_key or allowed_origins is not None:
        from a2a_impl import auth

        auth.install(
            app,
            bearer_token=bearer or "",
            api_key=api_key,
            allowed_origins_raw=allowed_origins or "",
        )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    return app


def _send_msg(client, text="hi", rpc_id="r1"):
    return client.post(
        "/a2a",
        headers=A2A_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "SendMessage",
            "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}]}},
        },
    )


async def _poll_terminal(client, task_id, *, tries=60):
    for _ in range(tries):
        g = await client.post(
            "/a2a",
            headers=A2A_HEADERS,
            json={"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": task_id}},
        )
        t = g.json()["result"]
        if t["status"]["state"] in (
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_INPUT_REQUIRED",
        ):
            return t
        await asyncio.sleep(0.03)
    raise AssertionError(f"task {task_id} never reached terminal")


@pytest.fixture(autouse=True)
def _clear_terminal_hook():
    set_terminal_hook(None)
    yield
    set_terminal_hook(None)


@pytest.mark.asyncio
async def test_send_message_runs_to_completed_with_text_artifact():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "hello ")
        yield ("text", "world")
        yield ("done", "hello world")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        r = await _send_msg(c)
        assert r.status_code == 200
        task = r.json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = final["artifacts"][0]["parts"]
    assert parts[0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_terminal_artifact_carries_all_extensions_in_order():
    """text → worldstate-delta → cost-v1 → context-v1, matching the order the
    hand-rolled handler emitted (consumers read parts in order); the context-v1
    readout (#1372) trails as a pure append."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "done text")
        yield ("usage", {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001})
        yield ("usage", {"input_tokens": 140, "output_tokens": 20, "cost_usd": 0.001})
        yield ("delta", {"domain": "board", "path": "data.backlog", "op": "inc", "value": 1})
        yield ("done", "done text")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    parts = final["artifacts"][0]["parts"]
    assert parts[0]["text"] == "done text"
    mimes = [p.get("metadata", {}).get("mimeType") for p in parts[1:]]
    assert mimes == [pa.WORLDSTATE_DELTA_MIME, pa.COST_MIME, _CONTEXT_MIME]
    # cost-v1 payload carries the SUMMED token usage (100+140 input) + the turn duration.
    cost = pa.parse_cost(parts[2])
    assert cost["usage"]["input_tokens"] == 240
    assert cost["success"] is True
    # durationMs is present (proto-JSON round-trips numbers as floats, so compare numerically).
    assert isinstance(cost["durationMs"], (int, float)) and cost["durationMs"] >= 0
    # context-v1 carries the PEAK prompt size (max single call's input_tokens), the live
    # context-window fill — distinct from the summed spend above.
    _ctx_mime, ctx = pa.read_data(parts[3])
    assert _ctx_mime == _CONTEXT_MIME
    assert ctx["contextTokens"] == 140


@pytest.mark.asyncio
async def test_no_extension_parts_when_nothing_to_report():
    """A bare text completion yields only the text part — no empty DataParts."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "just text")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])
    parts = final["artifacts"][0]["parts"]
    assert len(parts) == 1 and parts[0]["text"] == "just text"


@pytest.mark.asyncio
async def test_error_event_lands_task_failed():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "partial")
        yield ("error", "boom")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])
    assert final["status"]["state"] == "TASK_STATE_FAILED"
    msg_parts = final["status"]["message"]["parts"]
    assert any(p.get("text") == "boom" for p in msg_parts)


@pytest.mark.asyncio
async def test_input_required_parks_task_with_question():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "thinking… ")
        yield ("input_required", {"question": "Approve the merge?"})
        yield ("done", "should not reach")  # runner must stop at input_required

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])
    assert final["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    text = " ".join(p.get("text", "") for p in final["status"]["message"]["parts"])
    assert "Approve the merge?" in text


@pytest.mark.asyncio
async def test_tool_events_surface_as_tool_call_dataparts():
    """tool_start/tool_end become tool-call-v1 DataParts on working status
    frames — observed via the streaming endpoint (the real consumer path; a
    GetTask poll only sees the collapsed terminal state)."""
    import json

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "t1", "name": "file_bug", "input": {"x": 1}})
        yield ("tool_end", {"id": "t1", "name": "file_bug", "output": "BUG-9"})
        yield ("done", "filed")

    app = _build_app(stream)
    tool_payloads = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "s",
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frame = json.loads(line[5:].strip())
                status = frame.get("result", {}).get("statusUpdate", {}).get("status", {})
                msg = status.get("message")
                if not msg:
                    continue
                for part in msg.get("parts", []):
                    payload = pa.parse_tool_call(part)
                    if payload is not None:
                        tool_payloads.append(payload)

    phases = [p["phase"] for p in tool_payloads]
    assert "started" in phases and "completed" in phases
    assert all(p["name"] == "file_bug" for p in tool_payloads)
    completed = next(p for p in tool_payloads if p["phase"] == "completed")
    assert completed["result"] == "BUG-9"


@pytest.mark.asyncio
async def test_errored_tool_end_surfaces_phase_failed():
    """A tool_end flagged error → a phase=\"failed\" tool-call DataPart (the card
    renders the X), carrying the error text. A genuine command failure takes this
    path — a DECLINE no longer does (#1692: it returns a normal result now)."""
    import json

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "t1", "name": "run_command", "input": {"command": "false"}})
        yield (
            "tool_end",
            {"id": "t1", "name": "run_command", "output": "Error: command exited 1", "error": True},
        )
        yield ("done", "ok")

    app = _build_app(stream)
    payloads = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "s",
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frame = json.loads(line[5:].strip())
                status = frame.get("result", {}).get("statusUpdate", {}).get("status", {})
                msg = status.get("message")
                if not msg:
                    continue
                for part in msg.get("parts", []):
                    p = pa.parse_tool_call(part)
                    if p is not None:
                        payloads.append(p)

    failed = next((p for p in payloads if p["phase"] == "failed"), None)
    assert failed is not None, [p["phase"] for p in payloads]
    assert failed["name"] == "run_command"
    assert "exited 1" in (failed.get("error") or "")


@pytest.mark.asyncio
async def test_text_deltas_stream_as_incremental_artifact_frames():
    """Token-by-token: each `text` delta is forwarded as its own artifact-update
    (append) frame, so the console fills the bubble live instead of the whole
    answer landing at turn end. The terminal replaces with the canonical text."""
    import json

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        # each chunk is over the executor's flush threshold so it emits a frame
        yield ("text", "First sentence of the streamed answer here. ")
        yield ("text", "Second sentence that continues the answer. ")
        yield ("text", "Third and final sentence to wrap it up.")
        yield (
            "done",
            "First sentence of the streamed answer here. "
            "Second sentence that continues the answer. "
            "Third and final sentence to wrap it up.",
        )

    app = _build_app(stream)
    artifact_texts: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "s",
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                au = json.loads(line[5:].strip()).get("result", {}).get("artifactUpdate")
                if not au:
                    continue
                for part in au.get("artifact", {}).get("parts", []):
                    if part.get("text"):
                        artifact_texts.append(part["text"])

    # Streamed incrementally — more than one text-bearing artifact frame (a single
    # terminal frame would mean no streaming), and the first delta arrives early.
    assert len(artifact_texts) >= 2, artifact_texts
    assert any("First sentence" in t for t in artifact_texts)
    assert "Third and final sentence" in "".join(artifact_texts)


# ── Durable-store pair: #1710 reasoning flood + #1709 truncated artifact ──────

_REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"


def _reasoning_texts_from_history(history: list[dict]) -> list[str]:
    """The concatenated reasoning text of each all-reasoning history Message."""
    out: list[str] = []
    for m in history or []:
        parts = m.get("parts") or []
        texts: list[str] | None = []
        for p in parts:
            mime = (p.get("metadata") or {}).get("mimeType")
            if mime == _REASONING_MIME and isinstance(p.get("data"), dict):
                texts.append(p["data"].get("text", ""))
            else:
                texts = None
                break
        if texts:
            out.append("".join(texts))
    return out


@pytest.mark.asyncio
async def test_reasoning_flood_coalesces_in_durable_history(tmp_path):
    """#1710: a token-per-event reasoning stream (~200 deltas) lands in durable
    task history as ONE coalesced reasoning Message carrying the full text —
    not a Message row per word — so GetTask history stays conversation-shaped."""
    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine

    words = [f" think{i}" for i in range(200)]

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        for w in words:
            yield ("reasoning", w)
        yield ("done", "short answer")

    store = ReasoningCoalescingTaskStore(make_sqlite_engine(str(tmp_path / "a2a-tasks.db")))
    await store.initialize()
    app = _build_app(stream, task_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    history = final.get("history") or []
    reasoning = _reasoning_texts_from_history(history)
    assert len(reasoning) == 1, f"expected ONE coalesced reasoning Message, got {len(reasoning)} in {len(history)} rows"
    assert reasoning[0] == "".join(words)  # nothing dropped by coalescing
    # The flood never lands as per-word rows: user message + reasoning (+ status moves).
    assert len(history) <= 4, f"history bloated: {len(history)} rows"
    # The answer artifact is unaffected.
    parts = final["artifacts"][0]["parts"]
    assert parts[0]["text"] == "short answer"


@pytest.mark.asyncio
async def test_reasoning_stream_stays_live_but_batched():
    """The live thinking bubble still fills incrementally — multiple reasoning
    frames on the wire, batched to ~_FLUSH_CHARS granularity (not one WORKING
    frame per token), with no reasoning content lost and the tail flushed
    before the answer text frame (stream order)."""
    import json

    words = [f" w{i}" for i in range(120)]
    answer = "answer text long enough to flush as its own frame"

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        for w in words:
            yield ("reasoning", w)
        yield ("text", answer)
        yield ("done", answer)

    app = _build_app(stream)
    reasoning_frames: list[str] = []
    saw_artifact_frame = False
    reasoning_after_artifact = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "s",
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                result = json.loads(line[5:].strip()).get("result", {})
                status = result.get("statusUpdate", {}).get("status", {})
                for part in (status.get("message") or {}).get("parts", []):
                    if (part.get("metadata") or {}).get("mimeType") == _REASONING_MIME:
                        reasoning_frames.append(part.get("data", {}).get("text", ""))
                        if saw_artifact_frame:
                            reasoning_after_artifact = True
                if result.get("artifactUpdate"):
                    saw_artifact_frame = True

    assert "".join(reasoning_frames) == "".join(words)  # wire carries ALL reasoning
    # Live (more than one frame) but batched (far fewer frames than tokens).
    assert 2 <= len(reasoning_frames) < len(words) // 2, len(reasoning_frames)
    # The buffered reasoning tail flushed BEFORE the answer frame (stream order).
    assert saw_artifact_frame and not reasoning_after_artifact


@pytest.mark.asyncio
async def test_finalize_repairs_durable_artifact_after_downstream_frame_loss(monkeypatch):
    """#1709: mid-stream artifact-append frames lost DOWNSTREAM of the executor
    (emitted, but never persisted) — the terminal last_chunk frame must REPLACE
    the artifact with the canonical text so the durable task self-heals, instead
    of appending meta-only and permanently sealing the truncation."""
    from a2a.server.tasks.task_manager import TaskManager
    from a2a.types import TaskArtifactUpdateEvent

    chunks = [
        "First chunk of the streamed answer, well over the flush threshold. ",
        "Second chunk that the durable store never sees. ",
        "Third chunk, also lost downstream of the executor.",
    ]
    full = "".join(chunks)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        for c in chunks:
            yield ("text", c)
        yield ("usage", {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001})
        yield ("done", full)

    orig_process = TaskManager.process

    async def lossy_process(self, event):
        # The forensic loss shape from #1709: mid-stream appends vanish; the
        # first (create) frame and the terminal last_chunk frame arrive.
        if isinstance(event, TaskArtifactUpdateEvent) and event.append and not event.last_chunk:
            return event
        return await orig_process(self, event)

    monkeypatch.setattr(TaskManager, "process", lossy_process)

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = final["artifacts"][0]["parts"]
    text_parts = [p["text"] for p in parts if p.get("text")]
    # The canonical answer, exactly once — neither truncated nor doubled.
    assert text_parts == [full]
    # The meta parts still land exactly once.
    mimes = [(p.get("metadata") or {}).get("mimeType") for p in parts]
    assert mimes.count(pa.COST_MIME) == 1


@pytest.mark.asyncio
async def test_streamed_answer_lands_exactly_once_in_durable_artifact():
    """Normal (no-loss) path: the authoritative terminal REPLACE leaves the
    durable artifact holding ONE canonical text part — the streamed chunk parts
    don't linger (concat readers of the stored task would re-join them with
    separators) and the replace doesn't double the answer."""

    chunks = ["First streamed chunk, over the threshold. ", "Second streamed chunk, also flushed."]
    full = "".join(chunks)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        for c in chunks:
            yield ("text", c)
        yield ("done", full)

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    text_parts = [p["text"] for p in final["artifacts"][0]["parts"] if p.get("text")]
    assert text_parts == [full]


@pytest.mark.asyncio
async def test_terminal_replace_wire_shape_omits_append_key():
    """The CONSUMER CONTRACT of the terminal replace, at the wire level: proto3
    gives the A2A ``append`` bool no presence, so the SDK OMITS the key when it's
    False — the terminal frame arrives as ``{artifact: <full text + meta>,
    lastChunk: true}`` with NO ``append`` key at all. Every frame consumer must
    read that ABSENCE as replace (only an explicit ``true`` appends); mapping
    absent→append re-appends the whole canonical answer and doubles every
    streamed turn (the operator-console regression this locks against —
    ``artifactAppends`` in apps/web/src/lib/api.ts, mirrored by
    evals/client.py)."""
    import json

    chunks = ["First streamed chunk, over the flush threshold. ", "Second streamed chunk, also flushed."]
    full = "".join(chunks)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        for c in chunks:
            yield ("text", c)
        yield ("done", full)

    app = _build_app(stream)
    artifact_frames: list[dict] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": "s",
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                update = json.loads(line[5:].strip()).get("result", {}).get("artifactUpdate")
                if update:
                    artifact_frames.append(update)

    assert len(artifact_frames) >= 2  # streamed chunks + the terminal replace
    terminal, mid = artifact_frames[-1], artifact_frames[:-1]
    # Terminal frame: last chunk, full canonical text — and the ``append`` key is
    # ABSENT (proto3 omitted-false), which consumers MUST read as replace.
    assert terminal.get("lastChunk") is True
    assert "append" not in terminal, f"expected proto3 to omit append=False, got {terminal.get('append')!r}"
    terminal_texts = [p["text"] for p in terminal["artifact"]["parts"] if p.get("text")]
    assert terminal_texts == [full]
    # Mid-stream chunks after the first carry an EXPLICIT append=true — the only
    # value a consumer may treat as append. (The first chunk creates the artifact
    # with append=False, so it too arrives with the key omitted.)
    assert "append" not in mid[0]
    assert all(f.get("append") is True for f in mid[1:])


@pytest.mark.asyncio
async def test_input_required_form_carries_hitl_datapart():
    """A request_user_input form (or run_command approval) parks the task with a
    protoAgent-local hitl-v1 DataPart carrying the full payload, plus a text
    part that falls back to the form title — so the console renders the form,
    not just a stringified blob."""
    from a2a_impl.executor import HITL_MIME

    form = {
        "kind": "form",
        "title": "Deploy params",
        "description": "Confirm before rollout",
        "steps": [{"id": "env", "label": "Environment", "type": "string"}],
    }

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("input_required", form)
        yield ("done", "must not reach")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        final = await _poll_terminal(c, task["id"])

    assert final["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    parts = final["status"]["message"]["parts"]
    # Plain consumers see the title as the prompt text.
    assert "Deploy params" in " ".join(p.get("text", "") for p in parts)
    # The full form payload rides a hitl-v1 DataPart for the console to render.
    hitl = next((payload for p in parts for mime, payload in [pa.read_data(p)] if mime == HITL_MIME), None)
    assert hitl is not None
    assert hitl["kind"] == "form"
    assert hitl["title"] == "Deploy params"
    assert hitl["steps"][0]["id"] == "env"


# ── Terminal hook (telemetry + Activity feed) ─────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_hook_fires_turn_outcome_on_completion():
    outcomes: list[TurnOutcome] = []
    set_terminal_hook(outcomes.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", {"input_tokens": 30, "output_tokens": 12, "cost_usd": 0.002, "model": "claude-x"})
        yield ("tool_start", {"id": "t", "name": "n"})
        yield ("done", "answer")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        await _poll_terminal(c, task["id"])
        # hook fires inside execute() — already completed by poll time
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.state == "completed"
    assert o.text == "answer"
    assert o.usage["input_tokens"] == 30 and o.usage["output_tokens"] == 12
    assert o.cost_usd == 0.002
    assert o.llm_calls == 1 and o.tool_calls == 1
    assert o.models == ["claude-x"]


@pytest.mark.asyncio
async def test_terminal_hook_fires_failed_outcome_on_error():
    outcomes: list[TurnOutcome] = []
    set_terminal_hook(outcomes.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("error", "kaboom")

    app = _build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await _send_msg(c)).json()["result"]["task"]
        await _poll_terminal(c, task["id"])
    assert [o.state for o in outcomes] == ["failed"]


# ── Auth + origin enforcement (a2a_impl.auth middleware) ──────────────────────────


@pytest.mark.asyncio
async def test_missing_bearer_token_returns_401():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "x")

    app = _build_app(stream, bearer="secret-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await _send_msg(c)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invalid_bearer_token_returns_401():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "x")

    app = _build_app(stream, bearer="secret-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/a2a",
            headers={**A2A_HEADERS, "Authorization": "Bearer wrong"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_token_passes():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "x")

    app = _build_app(stream, bearer="secret-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/a2a",
            headers={**A2A_HEADERS, "Authorization": "Bearer secret-token"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        )
    assert r.status_code == 200
    assert "result" in r.json()


@pytest.mark.asyncio
async def test_rejected_origin_returns_403():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "x")

    app = _build_app(stream, allowed_origins="https://example.com")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/a2a",
            headers={**A2A_HEADERS, "Origin": "https://evil.com"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_allowed_origin_passes():
    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "x")

    app = _build_app(stream, allowed_origins="https://example.com,https://other.com")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/a2a",
            headers={**A2A_HEADERS, "Origin": "https://example.com"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        )
    assert r.status_code == 200


# ── native vision: inbound image part extraction ──────────────────────────────
def test_extract_image_parts_inline_and_url():
    import base64
    import types as _t

    from a2a_impl.executor import _extract_image_parts

    img = b"\x89PNG-fake-bytes"
    parts = [
        _t.SimpleNamespace(media_type="", raw=b"", url=""),  # text → skip
        _t.SimpleNamespace(media_type="image/png", raw=img, url=""),  # inline image
        _t.SimpleNamespace(media_type="image/jpeg", raw=b"", url="https://x/y.jpg"),  # url image
        _t.SimpleNamespace(media_type="application/pdf", raw=b"x", url=""),  # non-image → skip
    ]
    ctx = _t.SimpleNamespace(message=_t.SimpleNamespace(parts=parts))
    out = _extract_image_parts(ctx)
    assert len(out) == 2
    assert out[0] == ("image/png", f"data:image/png;base64,{base64.b64encode(img).decode()}")
    assert out[1] == ("image/jpeg", "https://x/y.jpg")


def test_extract_image_parts_empty_when_no_message():
    import types as _t

    from a2a_impl.executor import _extract_image_parts

    assert _extract_image_parts(_t.SimpleNamespace(message=None)) == []
