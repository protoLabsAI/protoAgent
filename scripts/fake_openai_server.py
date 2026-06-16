#!/usr/bin/env python3
"""A zero-dependency fake OpenAI-compatible endpoint for the CI live-smoke.

Boots the REAL protoAgent server against this so a live A2A turn exercises the
real wire path (executor → chat stream → SSE framing → a2a-sdk handler) without
a real model or gateway. The model just needs to return a valid completion so
the turn reaches a terminal state and emits artifact/COMPLETED frames — that's
what catches the green-but-wire-broken class (CRLF SSE, A2A routing/version,
lean-image boot) that unit/mock tests miss.

Serves:
  GET  /v1/models               → a one-model list (drawer/validation)
  POST /v1/chat/completions     → a streaming (or non-streaming) chat completion

Run: python scripts/fake_openai_server.py <port>
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The agent wraps its answer in <output>…</output> (graph/output_format.py); emit
# that shape so the parser extracts a clean final answer.
_ANSWER = "<output>live smoke ok</output>"
_MODEL = "protolabs/reasoning"


def _chunk(delta: dict, finish=None, usage=None) -> bytes:
    obj = {
        "id": "smoke-1",
        "object": "chat.completion.chunk",
        "model": _MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        obj["usage"] = usage
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": _MODEL, "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        streaming = bool(req.get("stream"))
        usage = {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return

        if streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # role chunk, content chunk, finish chunk (with usage), [DONE].
            self.wfile.write(_chunk({"role": "assistant", "content": ""}))
            self.wfile.write(_chunk({"content": _ANSWER}))
            self.wfile.write(_chunk({}, finish="stop", usage=usage))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(
                {
                    "id": "smoke-1",
                    "object": "chat.completion",
                    "model": _MODEL,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": _ANSWER}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"[fake-openai] listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
