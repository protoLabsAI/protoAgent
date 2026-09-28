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

The agent exports to ``http://127.0.0.1:<port>/<token>/v1/traces`` with a per-client
token, so it needs no Langfuse credentials of its own to do so (a deployment that exports
``LANGFUSE_*`` into this process's environment still passes those down to every child).

The server starts lazily, on 127.0.0.1 only, on the first ``register()``.
"""

from __future__ import annotations

import bisect
import json
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
#: How long after a turn ends the relay waits for its model-call spans before recording the
#: agent-reported fallback instead (Claude Code exports ~5s after a turn; this is generous).
FALLBACK_GRACE_S = 45.0


@dataclass
class _Turn:
    start_ns: int
    trace_id: bytes
    span_id: bytes
    #: Model-call spans delivered to Langfuse for this turn.
    model_calls: int = 0


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


def rewrite(request: Any, client: _Client | None) -> dict[bytes, int]:
    """Rewrite an ``ExportTraceServiceRequest`` in place: drop identity attributes, map
    usage, redact every string (attribute values at every level, span names, status and
    event messages), and re-parent later turns' spans. Returns the model-call spans
    (ones carrying usage) per turn, keyed by the turn's span id."""
    from graph.middleware.redaction import redact

    model_calls: dict[bytes, int] = {}
    for resource_spans in request.resource_spans:
        _filter_attributes(resource_spans.resource.attributes, redact)
        for scope_spans in resource_spans.scope_spans:
            _filter_attributes(scope_spans.scope.attributes, redact)
            for span in scope_spans.spans:
                if _filter_attributes(span.attributes, redact) and client is not None:
                    turn = client.turn_for(span.start_time_unix_nano)
                    if turn is not None:
                        model_calls[turn.span_id] = model_calls.get(turn.span_id, 0) + 1
                span.name = redact(span.name)
                if span.status.message:
                    span.status.message = redact(span.status.message)
                for event in span.events:
                    event.name = redact(event.name)
                    _filter_attributes(event.attributes, redact)
                for link in span.links:
                    _filter_attributes(link.attributes, redact)
                if client is not None:
                    _reparent(span, client)
    return model_calls


def _filter_attributes(attributes: Any, redact: Any, prefix: str = "") -> bool:
    """Drop identity keys, map usage keys, redact strings (nested ones too). ``prefix`` is
    the dotted path of an enclosing kvlist, so ``user`` → ``{email: …}`` is caught as
    ``user.email``. True when a usage attribute was present (a model call)."""
    keep, usage = [], False
    for attr in attributes:
        path = f"{prefix}{attr.key}"
        if path in DROP_ATTRIBUTES:
            continue
        if attr.key in USAGE_ATTRIBUTES:
            usage = True
            attr.key = USAGE_ATTRIBUTES[attr.key]
            if attr.value.HasField("string_value") and attr.value.string_value.isdigit():
                attr.value.int_value = int(attr.value.string_value)
        else:
            # Content is off unless the operator opted in (OTEL_LOG_*); this is the backstop.
            _redact_value(attr.value, redact, f"{path}.")
        keep.append(attr)
    del attributes[:]
    attributes.extend(keep)
    return usage


def _redact_value(value: Any, redact: Any, prefix: str = "") -> None:
    if value.HasField("string_value"):
        value.string_value = redact(value.string_value)
    elif value.HasField("array_value"):
        for item in value.array_value.values:
            _redact_value(item, redact, prefix)
    elif value.HasField("kvlist_value"):
        _filter_attributes(value.kvlist_value.values, redact, prefix)


def _reparent(span: Any, client: _Client) -> None:
    """Move a span that carries the spawn context into the turn it actually belongs to.

    Keyed on the turn's SPAN, not its trace: two runs of one pooled coder inside the same
    orchestrator turn share a trace id, and run 2 still belongs under run 2's span."""
    if span.trace_id != client.spawn_trace_id:
        return
    turn = client.turn_for(span.start_time_unix_nano)
    if turn is None or turn.span_id == client.spawn_span_id:
        return  # the spawning turn, or no turn registered: already right
    span.trace_id = turn.trace_id
    if span.parent_span_id == client.spawn_span_id:
        span.parent_span_id = turn.span_id  # the interaction root: under this turn's span


def client_known(token: str) -> bool:
    with _lock:
        return token in _clients


def handle(token: str, body: bytes) -> tuple[int, bytes]:
    """Relay one OTLP/HTTP protobuf export. Returns ``(status, response body)``."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from observability import tracing

    with _lock:
        client = _clients.get(token)
        # A snapshot: the rewrite runs OUTSIDE the lock, so a large export's redaction
        # never stalls begin_turn() / register() on the event loop.
        snapshot = None if client is None else _Client(client.spawn_trace_id, client.spawn_span_id, list(client.turns))
    if snapshot is None:
        return 404, b""
    target = tracing.otlp_export_target()
    if target is None:
        return 200, b""  # tracing went away: accept and drop, don't make the agent retry
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception:  # noqa: BLE001 — a malformed export
        return 400, b""
    model_calls = rewrite(request, snapshot)
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
    except Exception:  # noqa: BLE001 — Langfuse unreachable: tell the exporter to retry later
        log.debug("[otlp-relay] forward failed", exc_info=True)
        return 503, b""
    if 200 <= resp.status_code < 300 and model_calls:
        with _lock:
            for turn in client.turns:  # the live turns, not the snapshot
                turn.model_calls += model_calls.get(turn.span_id, 0)
    return resp.status_code, resp.content


def end_turn(token: str, span_id: str, end_ns: int, fallback: dict | None, *, grace: float = FALLBACK_GRACE_S) -> None:
    """A turn ended. ``fallback`` is the agent-reported ``{name, service, model, usage,
    cost_usd}`` for it: if no model-call span for the turn has reached Langfuse after
    ``grace`` seconds (an older Claude Code, a sampler turned off, an export Langfuse
    rejected), the relay records it as one generation under the turn's span instead.

    Deciding this here rather than when the turn ends is the point: Claude Code exports a
    turn's spans a few seconds AFTER it, so at turn end nothing can tell whether they will
    come, and guessing either way double-counts the cost or silently drops it."""
    if not fallback:
        return
    with _lock:
        client = _clients.get(token)
        turn = None
        if client is not None:
            raw = bytes.fromhex(span_id)
            turn = next((t for t in client.turns if t.span_id == raw), None)
    if turn is None:
        return
    timer = threading.Timer(grace, _emit_fallback_if_needed, args=(turn, end_ns, fallback))
    timer.daemon = True
    timer.start()


def _emit_fallback_if_needed(turn: _Turn, end_ns: int, fallback: dict) -> None:
    from observability import tracing

    with _lock:
        delivered = turn.model_calls
    target = tracing.otlp_export_target()
    if delivered or target is None:
        return
    try:
        import httpx

        url, auth = target
        httpx.post(
            url,
            content=_fallback_request(turn, end_ns, fallback).SerializeToString(),
            headers={
                "Content-Type": "application/x-protobuf",
                "Authorization": auth,
                "User-Agent": "protoagent-otlp-relay",
            },
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001 — best-effort, like the rest of tracing
        log.debug("[otlp-relay] fallback generation failed", exc_info=True)


def _fallback_request(turn: _Turn, end_ns: int, fallback: dict) -> Any:
    """One OTLP generation span, under the turn's span, carrying the agent-reported totals."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    def kv(key: str, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            return KeyValue(key=key, value=AnyValue(string_value=str(value)))
        return KeyValue(key=key, value=AnyValue(int_value=value))

    usage = {k: int(v) for k, v in (fallback.get("usage") or {}).items() if v}
    attrs = [
        kv("langfuse.observation.type", "generation"),
        kv("langfuse.observation.model.name", fallback.get("model") or ""),
        kv("langfuse.observation.usage_details", json.dumps(usage)),
        kv("langfuse.observation.metadata.usage_source", "reported by the coding agent (no native spans arrived)"),
    ]
    if fallback.get("cost_usd"):
        attrs.append(kv("langfuse.observation.cost_details", json.dumps({"total": float(fallback["cost_usd"])})))
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    rs.resource.attributes.extend([kv("service.name", fallback.get("service") or "protoagent")])
    ss = rs.scope_spans.add()
    ss.scope.name = "protoagent.otlp-relay"
    span = ss.spans.add()
    span.trace_id = turn.trace_id
    span.span_id = secrets.token_bytes(8)
    span.parent_span_id = turn.span_id
    span.name = fallback.get("name") or "coder-model"
    span.start_time_unix_nano = turn.start_ns
    span.end_time_unix_nano = max(end_ns, turn.start_ns)
    span.attributes.extend(attrs)
    return request


class _Handler(BaseHTTPRequestHandler):
    # A socket timeout, so an idle or slow connection can't pin a thread indefinitely.
    timeout = 10

    def _reply(self, status: int, payload: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        parts = self.path.strip("/").split("/")
        # Token first, before reading a byte of body: a caller without one gets nothing.
        if len(parts) != 3 or parts[1:] != ["v1", "traces"] or not client_known(parts[0]):
            self._reply(404)
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._reply(400)
            return
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._reply(413 if length > _MAX_BODY_BYTES else 411)
            return
        body = self.rfile.read(length)
        if (self.headers.get("content-encoding") or "").lower() == "gzip":
            import gzip

            try:
                body = gzip.decompress(body)
            except OSError:
                self._reply(400)
                return
            if len(body) > _MAX_BODY_BYTES:
                self._reply(413)
                return
        status, payload = handle(parts[0], body)
        self._reply(status, payload)

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
