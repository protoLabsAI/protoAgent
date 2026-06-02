"""Tests for the tool-call-v1 DataPart — structured tool events streamed to the
console so it can render live tool-call cards.

The producer (`server.py::_run_turn_stream`) yields structured
``("tool_start"|"tool_end", {id, name, input|output})`` tuples; the runner
stores the latest on ``record.last_tool_event`` and ``_build_status_event``
attaches it as a ``tool-call-v1`` DataPart alongside the existing text part.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from a2a_handler import (
    COMPLETED,
    HITL_MIME,
    INPUT_REQUIRED,
    SUBMITTED,
    TOOL_CALL_MIME,
    WORKING,
    A2ATaskStore,
    TaskRecord,
    _build_status_event,
    _now_iso,
    _run_task_background,
    _store,
    register_a2a_routes,
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Keep the module-level _store hermetic between integration tests."""
    from a2a_handler import _pending_webhook_tasks
    _store._tasks.clear()
    _pending_webhook_tasks.clear()
    yield
    _store._tasks.clear()
    _pending_webhook_tasks.clear()


def _make_record(**kwargs) -> TaskRecord:
    now = _now_iso()
    defaults = dict(
        id="tool-task",
        context_id="ctx",
        state=SUBMITTED,
        created_at=now,
        updated_at=now,
        message_text="hi",
    )
    defaults.update(kwargs)
    return TaskRecord(**defaults)


async def _mock_stream(*events):
    for event in events:
        yield event
        await asyncio.sleep(0)


# ── _build_status_event: HITL form DataPart (Sprint A) ───────────────────────


def test_status_event_includes_hitl_datapart_on_input_required():
    """An input-required frame carries the HITL payload as a hitl-v1 DataPart
    (so the console renders a form/approval) plus the text part for text-only
    clients."""
    record = _make_record(state=INPUT_REQUIRED)
    record.last_status_message = "Pick a model"
    record.hitl_payload = {
        "kind": "form",
        "title": "Pick a model",
        "steps": [{"schema": {"type": "object", "properties": {"model": {"type": "string"}}}}],
    }
    parts = _build_status_event(record)["status"]["message"]["parts"]
    hitl = [p for p in parts if p.get("kind") == "data" and p["metadata"]["mimeType"] == HITL_MIME]
    assert len(hitl) == 1 and hitl[0]["data"] == record.hitl_payload
    assert any(p.get("kind") == "text" for p in parts)  # text part stays


# ── _build_status_event: tool-call DataPart ──────────────────────────────────


def test_status_event_includes_tool_call_datapart_alongside_text():
    """When a tool event is present, the status message carries BOTH the text
    part (back-compat for text-only consumers) AND a tool-call-v1 DataPart."""
    record = _make_record(state=WORKING)
    record.last_status_message = "🔧 web_search: latest news"
    record.last_tool_event = {
        "id": "run-1",
        "name": "web_search",
        "input": "latest news",
        "phase": "start",
    }
    evt = _build_status_event(record)
    parts = evt["status"]["message"]["parts"]

    text_parts = [p for p in parts if p.get("kind") == "text"]
    data_parts = [p for p in parts if p.get("kind") == "data"]
    assert text_parts and text_parts[0]["text"] == "🔧 web_search: latest news"
    assert len(data_parts) == 1
    assert data_parts[0]["metadata"]["mimeType"] == TOOL_CALL_MIME
    assert data_parts[0]["data"] == record.last_tool_event


def test_status_event_text_only_when_no_tool_event():
    """No tool event → just the text part, no empty DataPart that consumers
    looking for tool cards would have to special-case."""
    record = _make_record(state=WORKING)
    record.last_status_message = "thinking..."
    record.last_tool_event = None
    parts = _build_status_event(record)["status"]["message"]["parts"]
    assert all(p.get("kind") == "text" for p in parts)
    assert len(parts) == 1


def test_status_event_terminal_state_drops_tool_event():
    """On a terminal transition the status message branch is skipped entirely,
    so no stale tool ping rides the final frame."""
    record = _make_record(state=COMPLETED)
    record.last_status_message = "🔧 web_search: ..."
    record.last_tool_event = {"id": "run-1", "name": "web_search", "phase": "start"}
    evt = _build_status_event(record, final=True)
    assert "message" not in evt["status"]


# ── update_state: tool_event persistence ─────────────────────────────────────


@pytest.mark.asyncio
async def test_update_state_persists_tool_event():
    store = A2ATaskStore()
    record = _make_record()
    await store.create(record)
    evt = {"id": "r1", "name": "calc", "input": "2+2", "phase": "start"}
    await store.update_state("tool-task", WORKING, tool_event=evt)
    assert (await store.get("tool-task")).last_tool_event == evt


@pytest.mark.asyncio
async def test_update_state_clears_tool_event_on_terminal():
    """Terminal transitions wipe the rolling tool event (same contract as
    last_status_message) so a completed task shows a clean final state."""
    store = A2ATaskStore()
    record = _make_record(state=WORKING)
    record.last_tool_event = {"id": "r1", "name": "calc", "phase": "start"}
    await store.create(record)
    await store.update_state("tool-task", COMPLETED)
    assert (await store.get("tool-task")).last_tool_event is None


# ── Background runner: structured tool events ────────────────────────────────


@pytest.mark.asyncio
async def test_runner_records_structured_tool_events():
    """Structured tool_start/tool_end dicts flow through the runner: each is
    passed to update_state as a structured tool_event (phase-tagged) AND a text
    status is derived for back-compat."""
    store = A2ATaskStore()
    record = _make_record(id="bg-tools")
    await store.create(record)

    # Spy on the tool_event kwarg the runner hands to update_state — _push only
    # fires on the initial WORKING + terminal transitions, so it can't observe
    # the mid-run tool pings.
    captured: list[dict] = []
    orig_update = store.update_state

    async def _spy(task_id, state, *, tool_event=None, **kwargs):
        if tool_event is not None:
            captured.append(tool_event)
        return await orig_update(task_id, state, tool_event=tool_event, **kwargs)

    stream_fn = lambda: _mock_stream(
        ("text", "working"),
        ("tool_start", {"id": "r1", "name": "web_search", "input": "news"}),
        ("tool_end", {"id": "r1", "name": "web_search", "output": "3 results"}),
        ("done", "working"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        store.update_state = _spy
        try:
            await _run_task_background("bg-tools", stream_fn)
        finally:
            store.update_state = orig_update

    final = await store.get("bg-tools")
    assert final.state == COMPLETED
    # Terminal clears the rolling tool event.
    assert final.last_tool_event is None
    # The runner emitted a phase-tagged start (with input) and end (with output).
    starts = [e for e in captured if e.get("phase") == "start"]
    ends = [e for e in captured if e.get("phase") == "end"]
    assert starts and starts[0]["input"] == "news"
    assert ends and ends[0]["output"] == "3 results"


@pytest.mark.asyncio
async def test_runner_accepts_legacy_string_tool_payloads():
    """Back-compat: a producer that yields plain-string tool payloads still
    works — used as the text status verbatim, no structured event, no crash."""
    store = A2ATaskStore()
    record = _make_record(id="bg-legacy")
    await store.create(record)

    stream_fn = lambda: _mock_stream(
        ("tool_start", "🔧 file_bug: draft"),
        ("tool_end", "✅ file_bug → done"),
        ("done", "ok"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        await _run_task_background("bg-legacy", stream_fn)

    assert (await store.get("bg-legacy")).state == COMPLETED


# ── End-to-end: tool-call DataPart over the live SSE stream ──────────────────


def _make_tool_app():
    """A FastAPI app whose chat stream emits a structured tool_start/tool_end
    pair, so the SSE path can be exercised without langgraph or a real model."""
    from fastapi import FastAPI

    app = FastAPI()
    card = {"name": "test", "capabilities": {}}

    async def _fake_stream(text, context_id, **kwargs):
        # Sleeps model real tool latency: in production tool_start and tool_end
        # are separated by the tool actually running, so the SSE watcher (which
        # coalesces bursts) wakes between them and emits each tool frame.
        yield ("text", "looking that up")
        await asyncio.sleep(0.03)
        yield ("tool_start", {"id": "r1", "name": "current_time", "input": ""})
        await asyncio.sleep(0.03)
        yield ("tool_end", {"id": "r1", "name": "current_time", "output": "2026-05-29T12:00:00Z"})
        await asyncio.sleep(0.03)
        yield ("done", "It is noon UTC.")

    async def _fake_chat(text, session_id):
        return [{"role": "assistant", "content": "It is noon UTC."}]

    register_a2a_routes(
        app=app,
        chat_stream_fn_factory=_fake_stream,
        chat_fn=_fake_chat,
        api_key="",
        agent_card=card,
    )
    return app


@pytest.mark.asyncio
async def test_message_stream_carries_tool_call_datapart():
    """The whole backend path: a tool-emitting producer → runner → store →
    _build_status_event → SSE. A status-update frame must carry a tool-call-v1
    DataPart with {name, input/output} so the console can render a tool card.
    The text status part rides alongside it (back-compat)."""
    app = _make_tool_app()

    tool_parts: list[dict] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        async with client.stream(
            "POST", "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "s1",
                "method": "message/stream",
                "params": {"message": {"parts": [{"kind": "text", "text": "what time is it"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                result = json.loads(line[6:]).get("result", {})
                if result.get("kind") != "status-update":
                    continue
                for part in result.get("status", {}).get("message", {}).get("parts", []):
                    if part.get("metadata", {}).get("mimeType") == TOOL_CALL_MIME:
                        tool_parts.append(part)
                if result.get("final"):
                    break

    # At least the tool_start (and ideally tool_end) DataParts were streamed.
    assert tool_parts, "no tool-call-v1 DataPart appeared on any status-update frame"
    names = {p["data"]["name"] for p in tool_parts}
    phases = {p["data"]["phase"] for p in tool_parts}
    assert names == {"current_time"}
    assert "start" in phases or "end" in phases


@pytest.mark.asyncio
async def test_get_authenticated_extended_card_returns_card():
    """agent/getAuthenticatedExtendedCard returns the card from card_provider."""
    from fastapi import FastAPI

    app = FastAPI()

    async def _fake_stream(text, session_id, **kw):
        yield ("done", "ok")

    register_a2a_routes(
        app=app,
        chat_stream_fn_factory=_fake_stream,
        chat_fn=lambda *a, **k: [],
        api_key="",
        agent_card={},
        card_provider=lambda host: {"name": "protoagent", "url": f"http://{host}/a2a", "capabilities": {"streaming": True}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        resp = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "c1",
            "method": "agent/getAuthenticatedExtendedCard", "params": {},
        })
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["name"] == "protoagent"
        assert result["capabilities"]["streaming"] is True


@pytest.mark.asyncio
async def test_input_required_round_trip_over_stream():
    """HITL: a turn that pauses surfaces input-required + final:true; a follow-up
    message with the same taskId resumes it to completed (ADR 0003)."""
    from fastapi import FastAPI

    from a2a_handler import _store

    _store._tasks.clear()

    # Fake producer: first turn pauses (ask_human), the resume turn completes.
    async def _fake_stream(text, session_id, *, resume=False, caller_trace=None):
        if resume:
            yield ("done", f"answer: {text}")
        else:
            yield ("input_required", {"question": "What is your favorite color?"})

    app = FastAPI()
    register_a2a_routes(
        app=app, chat_stream_fn_factory=_fake_stream, chat_fn=lambda *a, **k: [], api_key="", agent_card={},
    )

    async def _drive(client, params):
        state = question = artifact = None
        task_id = None
        async with client.stream("POST", "/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "message/stream", "params": params,
        }) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                r = json.loads(line[6:]).get("result", {})
                if r.get("kind") == "task":
                    task_id = r.get("id")
                st = (r.get("status") or {}).get("state")
                if st:
                    state = st
                for p in ((r.get("status") or {}).get("message", {}) or {}).get("parts", []):
                    if p.get("kind") == "text":
                        question = p.get("text")
                for a in (r.get("artifacts") or ([r["artifact"]] if r.get("artifact") else [])):
                    for p in a.get("parts", []):
                        if p.get("kind") == "text":
                            artifact = p.get("text")
        return task_id, state, question, artifact

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5.0,
    ) as client:
        tid, state, question, _ = await _drive(client, {
            "contextId": "c", "message": {"parts": [{"kind": "text", "text": "ask me"}]},
        })
        assert state == "input-required"
        assert question == "What is your favorite color?"

        _, state2, _, artifact = await _drive(client, {
            "contextId": "c",
            "message": {"taskId": tid, "contextId": "c", "parts": [{"kind": "text", "text": "teal"}]},
        })
        assert state2 == "completed"
        assert artifact == "answer: teal"

    _store._tasks.clear()
