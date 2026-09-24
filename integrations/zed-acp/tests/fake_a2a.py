"""A scripted stand-in for a protoAgent ``/a2a`` + the ``/api`` reads the shim makes.

Frames are built in the exact A2A 1.0 JSON shapes protoAgent's executor emits (a
``result`` with one of ``task`` / ``statusUpdate`` / ``artifactUpdate``; tool calls on the
status message's ``metadata[tool-call-v1 URI]``; ``append`` absent when false).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOOL_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1"
COST_URI = "https://proto-labs.ai/a2a/ext/cost-v1"
HITL_MIME = "application/vnd.protolabs.hitl-v1+json"
REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"


def _env(result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "result": result}


def task(ctx: str, tid: str = "t1", state: str = "TASK_STATE_SUBMITTED") -> dict:
    return _env({"task": {"id": tid, "contextId": ctx, "status": {"state": state}}})


def status(ctx: str, tid: str = "t1", state: str = "TASK_STATE_WORKING", parts: list | None = None, metadata: dict | None = None) -> dict:
    msg: dict[str, Any] = {"role": "ROLE_AGENT", "parts": parts or [], "messageId": "m"}
    if metadata:
        msg["metadata"] = metadata
    return _env({"statusUpdate": {"taskId": tid, "contextId": ctx, "status": {"state": state, "message": msg}}})


def tool(ctx: str, call_id: str, name: str, phase: str, *, args: Any = None, result: Any = None, tid: str = "t1") -> dict:
    d: dict[str, Any] = {"toolCallId": call_id, "name": name, "phase": phase}
    if args is not None:
        d["args"] = args
    if result is not None:
        d["result"] = result
    return status(ctx, tid, metadata={TOOL_URI: d})


def reasoning(ctx: str, text: str, tid: str = "t1") -> dict:
    return status(ctx, tid, parts=[{"data": {"text": text}, "metadata": {"mimeType": REASONING_MIME}}])


def text(ctx: str, chunk: str, *, append: bool, tid: str = "t1", usage: dict | None = None) -> dict:
    art: dict[str, Any] = {"artifactId": "answer", "parts": [{"text": chunk}]}
    if usage:
        art["metadata"] = {COST_URI: {"usage": usage, "costUsd": 0.01}}
    au: dict[str, Any] = {"taskId": tid, "contextId": ctx, "artifact": art}
    if append:
        au["append"] = True  # proto3: false has no wire presence
    return _env({"artifactUpdate": au})


def done(ctx: str, tid: str = "t1", state: str = "TASK_STATE_COMPLETED", reason: str = "") -> dict:
    return status(ctx, tid, state=state, parts=[{"text": reason}] if reason else [])


def hitl(ctx: str, payload: dict, tid: str = "t1") -> dict:
    return status(ctx, tid, state="TASK_STATE_INPUT_REQUIRED", parts=[{"data": payload, "metadata": {"mimeType": HITL_MIME}}])


class FakeA2A:
    """``script(ctx, request_message) -> list[frame]`` is called per SendStreamingMessage."""

    def __init__(self, token: str | None = "secret", roots: dict | None = None) -> None:
        self.token = token
        self.roots = roots
        self.script: Any = lambda ctx, msg: [done(ctx)]
        self.requests: list[dict] = []
        self.cancels: list[str] = []
        self.hold = threading.Event()  # set() to release a frame list that ends in HOLD
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _authed(self) -> bool:
                if fake.token is None:
                    return True
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                return True

            def _json(self, body: Any, code: int = 200) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if not self._authed():
                    return
                if self.path == "/api/fs/roots" and fake.roots is not None:
                    self._json({"roots": fake.roots})
                else:
                    self._json({"detail": "Not Found"}, 404)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if not self._authed():
                    return
                assert self.headers.get("A2A-Version") == "1.0"
                method = body.get("method")
                if method == "GetTask":
                    self._json({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32001, "message": "Task not found"}})
                    return
                if method == "CancelTask":
                    fake.cancels.append(body["params"]["id"])
                    fake.hold.set()
                    self._json({"jsonrpc": "2.0", "id": body["id"], "result": {"id": body["params"]["id"]}})
                    return
                assert method == "SendStreamingMessage", method
                msg = body["params"]["message"]
                fake.requests.append(msg)
                frames = fake.script(msg.get("contextId"), msg)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for f in frames:
                    if f == "HOLD":
                        fake.hold.wait(10)
                        continue
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.write(f"data: {json.dumps(f)}\n\n".encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeA2A:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()
