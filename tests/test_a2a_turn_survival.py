"""S0 of Swap & Resume: a turn survives its SSE consumer disconnecting.

The operator swapping agents (or reloading, or losing wifi) aborts the console's
``SendStreamingMessage`` response mid-turn. The turn is SERVER-OWNED: the
producer must keep running to a terminal state, and everything it did while
nobody was watching must be recoverable afterwards via ``GetTask`` — that's the
ground S1's resubscribe-and-replay reattach stands on.

Nothing asserted this before: the hardened registry (#1713) retires an
ActiveTask's producer 0.5s after the last strong reference drops, and nothing
in code or tests distinguished "turn finished" from "last subscriber left".

Transport note: httpx's ``ASGITransport`` cannot abort a response mid-stream
(its close AWAITS the app — a parked producer deadlocks the test), so the
streaming half drives the ASGI app directly and delivers a real
``http.disconnect``, exactly what uvicorn sends when the browser goes away.
The follow-up ``GetTask`` polling then uses the normal httpx client.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_a2a_handler import A2A_HEADERS, _build_app, _poll_terminal


def _stream_scope() -> dict:
    headers = [(k.lower().encode(), v.encode()) for k, v in A2A_HEADERS.items()]
    headers += [(b"content-type", b"application/json"), (b"host", b"test")]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/a2a",
        "raw_path": b"/a2a",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


def _stream_body() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "s",
            "method": "SendStreamingMessage",
            "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
        }
    ).encode()


async def _stream_then_disconnect(app, *, frames_before_disconnect: int = 1) -> str:
    """Open SendStreamingMessage against the raw ASGI app, read SSE frames until a
    task id appears (at least ``frames_before_disconnect`` frames), then deliver
    ``http.disconnect`` — the browser-walked-away signal. Returns the task id."""
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await to_app.get()

    async def send(message):
        await from_app.put(message)

    await to_app.put({"type": "http.request", "body": _stream_body(), "more_body": False})
    app_task = asyncio.create_task(app(_stream_scope(), receive, send))

    task_id = ""
    seen = 0
    try:
        while not task_id or seen < frames_before_disconnect:
            message = await asyncio.wait_for(from_app.get(), timeout=10)
            if message["type"] != "http.response.body":
                continue
            body = message.get("body", b"").decode(errors="replace")
            for line in body.splitlines():
                if not line.startswith("data:"):
                    continue
                seen += 1
                frame = json.loads(line[5:].strip())
                result = frame.get("result", {})
                task_id = (
                    result.get("task", {}).get("id")
                    or result.get("statusUpdate", {}).get("taskId")
                    or result.get("artifactUpdate", {}).get("taskId")
                    or task_id
                )
    finally:
        # The operator swaps away: uvicorn would deliver http.disconnect.
        await to_app.put({"type": "http.disconnect"})

    # Drain whatever the response layer still writes, then let the request task end.
    async def _drain():
        while True:
            m = await from_app.get()
            if m["type"] == "http.response.body" and not m.get("more_body", True):
                return

    drain = asyncio.create_task(_drain())
    await asyncio.wait({app_task}, timeout=10)
    drain.cancel()
    if not app_task.done():  # the request task itself must not leak either
        app_task.cancel()
        await asyncio.gather(app_task, return_exceptions=True)
        raise AssertionError("ASGI request task did not end after http.disconnect")
    assert task_id, "never learned the task id from the stream"
    return task_id


@pytest.mark.asyncio
async def test_turn_survives_sse_consumer_disconnect():
    gate = asyncio.Event()  # holds the turn mid-flight until the client has left
    resumed = asyncio.Event()  # proves the producer ran ON past the disconnect

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "first ")
        await gate.wait()
        resumed.set()
        yield ("text", "second")
        yield ("done", "first second")

    app = _build_app(stream)
    task_id = await _stream_then_disconnect(app)

    # Wait deliberately PAST the hardened registry's 0.5s retire grace before
    # releasing the turn — if "subscriber left" is treated as "cancel the turn",
    # this is where the producer dies.
    await asyncio.sleep(0.8)
    gate.set()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        t = await _poll_terminal(c, task_id)

    assert resumed.is_set(), "the producer never ran past the disconnect — the turn was killed"
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", f"turn ended {t['status']['state']}"
    arts = t.get("artifacts") or []
    text = "".join(p.get("text", "") for a in arts for p in (a.get("parts") or []))
    assert "second" in text, f"post-disconnect output missing from the durable task: {text!r}"


@pytest.mark.asyncio
async def test_disconnected_turn_tool_frames_land_in_task_history():
    """The catch-up content S1 replays: tool frames emitted while nobody was
    subscribed must still land in the durable task's history."""
    gate = asyncio.Event()

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "working ")
        await gate.wait()
        yield ("tool_start", {"id": "t1", "name": "file_bug", "input": {"x": 1}})
        yield ("tool_end", {"id": "t1", "name": "file_bug", "output": "BUG-9"})
        yield ("done", "filed")

    app = _build_app(stream)
    task_id = await _stream_then_disconnect(app)
    await asyncio.sleep(0.8)
    gate.set()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        t = await _poll_terminal(c, task_id)

    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    history_blob = json.dumps(t.get("history") or [])
    assert "file_bug" in history_blob, "tool frames emitted while detached must be in task.history"
