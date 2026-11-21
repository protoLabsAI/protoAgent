"""A decision in a chat that is open in a console is still pushed to the phone (#5648).

#5663 dropped that push, and a desktop app that keeps a chat open then stopped every
decision push. That rule is reverted; this is the real-process guard against its return.

A real hub boots with one push-enrolled device. The decision is raised through the tool
path: a chat turn sent to the hub's ``/a2a`` endpoint, in which the (fake) model calls the
``request_decision`` tool, so the hub parks the turn and records the decision exactly as it
does for a real agent.

While the test holds ``GET /api/chat/attend?session=<sid>`` open, as a console showing that
chat does, the decision's row in the hub's ``push/delivery.sqlite3`` is attempted and is
not ``obsolete``. A second decision, in a chat with no attend stream, is attempted the same.
No push service is contacted: the seeded device's client keys are not a real subscription.

Slow + opt-in: ``PA_RUN_INTEGRATION=1 pytest tests/integration``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from infra.paths import reset_instance_paths
from security import devices
from tests.integration.conftest import http_get, http_post, poll, requires_integration

pytestmark = requires_integration

_MODEL = "protolabs/reasoning"
_MARK = "RAISE-A-DECISION"
_DECISION = {"question": "Ship the release?", "options": ["Ship it", "Hold it"]}


class _DecidingModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible model that calls ``request_decision`` when the operator's
    message carries the marker, and otherwise answers in plain text."""

    log_message = lambda self, *a: None  # quiet

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": _MODEL, "object": "model"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        messages = req.get("messages") or []
        offered = {t.get("function", {}).get("name") for t in req.get("tools") or []}
        # The hub appends its own context message after the operator's, so check them all.
        asked = any(m.get("role") == "user" and _MARK in json.dumps(m.get("content")) for m in messages)
        if "request_decision" in offered and asked and not any(m.get("role") == "tool" for m in messages):
            delta = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": "call_decide", "type": "function",
                "function": {"name": "request_decision", "arguments": json.dumps(_DECISION)},
            }]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "<output>done</output>"}, "stop"
        usage = {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}
        base = {"id": "decide-1", "model": _MODEL}
        self.send_response(200)  # the hub always streams its chat completions
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for choice, extra in (
            ({"index": 0, "delta": delta, "finish_reason": None}, {}),
            ({"index": 0, "delta": {}, "finish_reason": finish}, {"usage": usage}),
        ):
            chunk = {**base, "object": "chat.completion.chunk", "choices": [choice], **extra}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def fake_gateway():
    """Replaces the shared canned-answer gateway: ``fleet`` points each config at this port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DecidingModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _raise_decision(base: str, session: str) -> str:
    """Send the marked chat turn into ``session``; returns the id of the decision it parks on."""
    message = {"role": "ROLE_USER", "parts": [{"text": _MARK}], "messageId": f"m-{session}", "contextId": session}
    body = {"jsonrpc": "2.0", "id": session, "method": "SendStreamingMessage", "params": {"message": message}}
    st, raw = http_post(f"{base}/a2a", body, timeout=120, headers={"A2A-Version": "1.0"})
    assert st == 200, f"the chat turn was refused: {st} {raw[:300]}"

    def recorded():
        _, body = http_get(f"{base}/api/decisions?state=pending&session_id={session}")
        return json.loads(body).get("decisions")

    assert poll(recorded, timeout=60, interval=0.5), f"{session!r} never parked on a decision: {raw[:600]}"
    return recorded()[0]["id"]


def _attempt(ledger, decision_id: str):
    """The (state, attempts) of the decision's one push row, or None before it is queued."""
    if not ledger.exists():
        return None
    with sqlite3.connect(ledger) as db:
        query = "SELECT state, attempts FROM attempts WHERE notice_id = ?"
        return db.execute(query, (f"decision-{decision_id}",)).fetchone()


def test_a_decision_in_a_chat_open_in_a_console_is_still_pushed(fleet, monkeypatch):
    hub = fleet(name="hubattended")
    ledger = hub.home / "push" / "delivery.sqlite3"

    # One push-enrolled device in the hub's instance root (the registry is read per request).
    with monkeypatch.context() as patch:
        patch.setenv("PROTOAGENT_HOME", str(hub.home))
        patch.delenv("PROTOAGENT_INSTANCE", raising=False)
        reset_instance_paths()
        device, token = devices._register("phone")
        keys = {"p256dh": "client-key", "auth": "secret", "vapid_version": 1}
        assert devices.set_push_binding(device.id, token, endpoint="https://web.push.apple.com/test", **keys)
    reset_instance_paths()

    # 1. A console has the chat open: hold its attend stream until the push has been attempted.
    stream = urllib.request.urlopen(f"{hub.base}/api/chat/attend?session=watched-chat", timeout=30)
    try:
        assert stream.readline().strip() == b": attending"
        watched = _raise_decision(hub.base, "watched-chat")
        # The sender rejects the fake keys, so the row need not end accepted: it must have been tried.
        tried = lambda r: r and (r[1] >= 1 or r[0] == "obsolete") and r  # noqa: E731
        row = poll(lambda: tried(_attempt(ledger, watched)), timeout=30, interval=0.25)
        assert row and row[1] >= 1 and row[0] != "obsolete", f"an attended decision must still be pushed, got {row}"
    finally:
        stream.close()

    # 2. Nobody has this chat open: the push is attempted just the same.
    unwatched = _raise_decision(hub.base, "unwatched-chat")
    row = poll(lambda: (r := _attempt(ledger, unwatched)) and r[1] >= 1 and r, timeout=30, interval=0.25)
    assert row and row[0] != "obsolete", f"an unattended decision must still be pushed, got {row}"
