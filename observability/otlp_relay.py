"""A local OTLP relay between a coding agent's native OpenTelemetry export and Langfuse (#3742).

Claude Code (inside ``claude-agent-acp``) can export its own spans (one per model call,
per tool, per interaction) and join our trace through ``TRACEPARENT``. Sending them
straight to Langfuse has three problems this relay exists to fix:

- **Identity.** Every span carries ``user.email``, ``organization.id`` and account ids,
  and Claude Code offers no way to turn ``user.email`` / ``organization.id`` off. The
  relay drops them, and runs our redaction over the remaining string attributes.
- **Usage.** Token counts arrive as string ``input_tokens`` / ``output_tokens`` /
  ``cache_*_tokens``, which Langfuse doesn't read; renamed to integer ``gen_ai.usage.*``,
  each model call gets real usage and a Langfuse-computed cost.
- **Pooled processes.** The Agent SDK injects trace context only when its query process
  spawns, and ``claude-agent-acp`` keeps one query per session, so every later turn's spans
  carry the FIRST turn's trace id. The client registers each turn's window; spans are
  moved into the turn whose window their start time falls in.

It also keeps the Langfuse key out of the coding agent's environment: the agent exports to
``http://127.0.0.1:<port>/<token>/v1/traces``, the token is per client, and only this
process holds the credentials it forwards with.

The server starts lazily, on 127.0.0.1 only, on the first ``register()``.
"""

from __future__ import annotations

import bisect
import logging
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("protoagent.otlp_relay")

#: Identity attributes Claude Code stamps on every span (resource or span level).
DROP_ATTRIBUTES = frozenset(
    {"user.email", "user.id", "organization.id", "user.account_uuid", "user.account_id", "terminal.type"}
)
#: Claude Code's token attributes → the names Langfuse maps to usage.
USAGE_ATTRIBUTES = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read_input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation_input_tokens",
}
_MAX_BODY_BYTES = 8 * 1024 * 1024


@dataclass
class _Turn:
    start_ns: int
    trace_id: bytes
    span_id: bytes


@dataclass
class _Client:
    """One coding-agent process: the trace context it was spawned under, and its turns."""

    spawn_trace_id: bytes
    spawn_span_id: bytes
    turns: list[_Turn] = field(default_factory=list)

    def turn_for(self, start_ns: int) -> _Turn | None:
        """The turn a span that started at ``start_ns`` belongs to: the latest turn that
        began at or before it (a span between turns belongs to the one before)."""
        starts = [t.start_ns for t in self.turns]
        i = bisect.bisect_right(starts, start_ns) - 1
        return self.turns[i] if i >= 0 else None


_clients: dict[str, _Client] = {}
_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None


def _hex_id(value: str, size: int) -> bytes:
    raw = bytes.fromhex(value)
    if len(raw) != size:
        raise ValueError(f"expected a {size}-byte id")
    return raw


def register(trace_id: str, span_id: str) -> tuple[str, int] | None:
    """Register a coding-agent process spawned under ``(trace_id, span_id)`` (hex) and
    return ``(token, port)`` for its export URL, or None when the relay can't run
    (tracing off, a bad id, the port unavailable)."""
    from observability import tracing

    if tracing.otlp_export_target() is None:
        return None
    try:
        # Transitive via the Langfuse SDK's OTLP exporter; if a future SDK drops it, native
        # tracing just stays off rather than failing every export.
        import opentelemetry.proto.collector.trace.v1.trace_service_pb2  # noqa: F401

        client = _Client(_hex_id(trace_id, 16), _hex_id(span_id, 8))
        port = _ensure_server()
    except Exception:  # noqa: BLE001 — native tracing is optional; never fail the spawn
        log.debug("[otlp-relay] could not register a client", exc_info=True)
        return None
    token = secrets.token_urlsafe(24)
    with _lock:
        _clients[token] = client
    return token, port


def begin_turn(token: str, trace_id: str, span_id: str, start_ns: int) -> None:
    """Record that the process behind ``token`` started a turn traced as ``(trace_id,
    span_id)`` at ``start_ns`` (``time.time_ns()``)."""
    with _lock:
        client = _clients.get(token)
        if client is None:
            return
        try:
            client.turns.append(_Turn(start_ns, _hex_id(trace_id, 16), _hex_id(span_id, 8)))
        except ValueError:
            return
        client.turns.sort(key=lambda t: t.start_ns)


def unregister(token: str, *, delay: float = 0.0) -> None:
    """Forget a client. ``delay`` keeps it registered a little longer: a coding agent
    flushes its last spans as it exits, after the caller has already let go of it."""
    if delay > 0:
        timer = threading.Timer(delay, unregister, args=(token,))
        timer.daemon = True
        timer.start()
        return
    with _lock:
        _clients.pop(token, None)


def rewrite(request: Any, client: _Client | None) -> int:
    """Rewrite an ``ExportTraceServiceRequest`` in place: drop identity attributes, map
    usage, redact string attributes, and re-parent later turns' spans. Returns the
    number of spans."""
    from graph.middleware.redaction import redact

    spans = 0
    for resource_spans in request.resource_spans:
        _filter_attributes(resource_spans.resource.attributes, redact)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                spans += 1
                _filter_attributes(span.attributes, redact)
                if client is not None:
                    _reparent(span, client)
    return spans


def _filter_attributes(attributes: Any, redact: Any) -> None:
    keep = []
    for attr in attributes:
        if attr.key in DROP_ATTRIBUTES:
            continue
        if attr.key in USAGE_ATTRIBUTES:
            attr.key = USAGE_ATTRIBUTES[attr.key]
            if attr.value.HasField("string_value") and attr.value.string_value.isdigit():
                attr.value.int_value = int(attr.value.string_value)
        elif attr.value.HasField("string_value"):
            # Content is off unless the operator opted in (OTEL_LOG_*); this is the backstop.
            attr.value.string_value = redact(attr.value.string_value)
        keep.append(attr)
    del attributes[:]
    attributes.extend(keep)


def _reparent(span: Any, client: _Client) -> None:
    """Move a span that carries the spawn trace into the turn it actually belongs to."""
    if span.trace_id != client.spawn_trace_id:
        return
    turn = client.turn_for(span.start_time_unix_nano)
    if turn is None or turn.trace_id == client.spawn_trace_id:
        return  # the first turn, or no turn registered: it already has the right trace
    span.trace_id = turn.trace_id
    if span.parent_span_id == client.spawn_span_id:
        span.parent_span_id = turn.span_id  # the interaction root: under this turn's span


def handle(token: str, body: bytes) -> tuple[int, bytes]:
    """Relay one OTLP/HTTP protobuf export. Returns ``(status, response body)``."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from observability import tracing

    with _lock:
        client = _clients.get(token)
    if client is None:
        return 404, b""
    target = tracing.otlp_export_target()
    if target is None:
        return 200, b""  # tracing went away: accept and drop, don't make the agent retry
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception:  # noqa: BLE001 — a malformed export
        return 400, b""
    with _lock:
        rewrite(request, client)
    url, auth = target
    try:
        import httpx

        resp = httpx.post(
            url,
            content=request.SerializeToString(),
            headers={
                "Content-Type": "application/x-protobuf",
                "Authorization": auth,
                "User-Agent": "protoagent-otlp-relay",
            },
            timeout=15.0,
        )
        return resp.status_code, resp.content
    except Exception:  # noqa: BLE001 — Langfuse unreachable: tell the exporter to retry later
        log.debug("[otlp-relay] forward failed", exc_info=True)
        return 503, b""


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[1:] != ["v1", "traces"]:
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("content-length") or 0)
        if length <= 0 or length > _MAX_BODY_BYTES:
            self.send_response(413 if length > _MAX_BODY_BYTES else 400)
            self.end_headers()
            return
        status, payload = handle(parts[0], self.rfile.read(length))
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        pass


def _ensure_server() -> int:
    global _server
    with _lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="otlp-relay", daemon=True).start()
        return _server.server_address[1]
