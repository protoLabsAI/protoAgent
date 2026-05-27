"""A2A protocol handler — streaming, async task lifecycle, push notifications.

Implements the A2A spec (https://a2a-protocol.org/latest/) as a FastAPI route
factory.  All route logic lives here; server.py calls register_a2a_routes()
once during startup and otherwise stays out of the way.

JSON-RPC 2.0 methods on POST /a2a
─────────────────────────────────
  message/send                         async submit, returns Task with state=submitted
  message/stream                       send + SSE stream (spec-canonical name)
  message/sendStream                   deprecated alias for message/stream
  tasks/get                            fetch current Task state + artifact
  tasks/cancel                         cooperative cancel
  tasks/resubscribe                    SSE reconnect to an in-flight task
  tasks/pushNotificationConfig/set     register a webhook for a task
  tasks/pushNotificationConfig/get     read the current webhook config
  tasks/pushNotificationConfig/list    list configs (single-slot today — always 0 or 1)
  tasks/pushNotificationConfig/delete  clear the webhook

REST convenience aliases
────────────────────────
  POST /message:send                   same as message/send   (HTTP 202)
  POST /message:stream                 same as message/stream (SSE)
  GET  /tasks/{id}                     same as tasks/get
  GET  /tasks/{id}:subscribe           same as tasks/resubscribe (plain SSE, no JSON-RPC envelope)
  POST /tasks/{id}:cancel              same as tasks/cancel
  POST /tasks/{id}/pushNotificationConfigs   same as tasks/pushNotificationConfig/set

  GET  /.well-known/agent{,-card}.json Agent card (both paths)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# ── Task state constants ──────────────────────────────────────────────────────

SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"

_TERMINAL = {COMPLETED, FAILED, CANCELED}

# MIME type for worldstate-delta-v1 artifacts. Workstacean's effect-domain
# interceptor extracts any DataPart carrying this type on a terminal Task
# and republishes the deltas as world.state.delta bus events, so the GOAP
# planner can update its cached snapshot without waiting for the next poll.
# Ref: protoWorkstacean/docs/extensions/worldstate-delta-v1.md
WORLDSTATE_DELTA_MIME = "application/vnd.protolabs.worldstate-delta+json"

# Cost-v1: token usage + duration on the terminal artifact. Workstacean's
# A2AExecutor (protoWorkstacean#372) extracts this onto result.data so the
# cost interceptor can publish autonomous.cost.{agent}.{skill} samples.
# Schema: {"usage": {"input_tokens": int, "output_tokens": int,
#                    "total_tokens": int},
#          "durationMs": int,
#          "costUsd": float?}
# Ref: protoWorkstacean/docs/extensions/cost-v1.md
COST_MIME = "application/vnd.protolabs.cost-v1+json"

# Confidence-v1: the agent's self-reported confidence + optional explanation on
# the terminal artifact. A consumer (e.g. Workstacean's confidence interceptor)
# reads result.data.confidence (clamped to [0, 1]) and optional
# result.data.confidenceExplanation to record calibration samples.
# Schema: {"confidence": float, "confidenceExplanation": str?, "success": bool}
CONFIDENCE_MIME = "application/vnd.protolabs.confidence-v1+json"

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class PushNotificationConfig:
    url: str
    token: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class TaskRecord:
    """In-memory record for a single A2A task.

    The asyncio primitives (_cancel_event, _update_event, _bg_task) are never
    serialised — _task_to_response() reads only primitive fields.
    """

    id: str
    context_id: str
    state: str
    created_at: str
    updated_at: str
    message_text: str
    accumulated_text: str = ""
    error_message: str | None = None
    push_config: PushNotificationConfig | None = None
    # Most recent tool_start / tool_end status message, e.g.
    # "🔧 my_tool: …" or "✅ my_tool → …". Surfaced in the status
    # frames that ``_watch_task``
    # emits so consumers (SSE clients, :subscribe reconnects) see tool
    # progress without being coupled to the producer's in-process event
    # stream. Cleared to None on terminal transitions.
    last_status_message: str | None = None
    # Observed world-state mutations to emit on the terminal artifact under
    # the worldstate-delta-v1 MIME type. Populated during the run whenever a
    # tool with known effects succeeds (see _chat_langgraph_stream). Shape:
    # [{"domain": "protomaker_board", "path": "data.backlog_count",
    #   "op": "inc", "value": 1}, ...]
    deltas: list[dict] = field(default_factory=list)
    # Token usage accumulated across every LLM call in the run. Emitted on
    # the terminal artifact under the cost-v1 MIME so Workstacean's
    # cost interceptor (protoWorkstacean#372) can record per-skill samples.
    # Shape: {"input_tokens": int, "output_tokens": int, "total_tokens": int}
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    # Self-reported confidence for the confidence-v1 DataPart. Set by the
    # producer when it parses a <confidence> tag out of the model's final
    # output. Clamped to [0, 1] on write; None when the model didn't report
    # one (the DataPart is then omitted).
    confidence: float | None = None
    confidence_explanation: str | None = None
    # ── asyncio primitives (not serialised) ──
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _update_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _bg_task: asyncio.Task | None = field(default=None, repr=False)


# ── Task store ────────────────────────────────────────────────────────────────


_DEFAULT_TERMINAL_TTL_S = 3600     # evict terminal tasks older than 1h
_DEFAULT_CLEANUP_INTERVAL_S = 300  # sweep every 5 min


class A2ATaskStore:
    """Asyncio-safe in-memory task store.

    Uses a rotate-event pattern: each call to update_state() replaces
    _update_event with a new asyncio.Event and sets the old one so all current
    subscribers wake up in lock-step.  The new event is ready for the next
    batch of waiters.

    Retains tasks in-memory for ``_DEFAULT_TERMINAL_TTL_S`` after they hit a
    terminal state so pollers/webhook delivery still see them, then evicts.
    Without this, a long-lived process would leak memory proportional to total
    lifetime traffic.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def create(self, record: TaskRecord) -> TaskRecord:
        async with self._lock:
            self._tasks[record.id] = record
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def update_state(
        self,
        task_id: str,
        state: str,
        accumulated_text: str | None = None,
        error: str | None = None,
        status_message: str | None = None,
    ) -> TaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            record.state = state
            record.updated_at = _now_iso()
            if accumulated_text is not None:
                record.accumulated_text = accumulated_text
            if error is not None:
                record.error_message = error
            if status_message is not None:
                record.last_status_message = status_message
            # Terminal transitions clear the status message so post-run
            # subscribers see the final state cleanly, not a stale tool ping.
            if state in _TERMINAL:
                record.last_status_message = None
            old_event = record._update_event
            record._update_event = asyncio.Event()
        # Wake subscribers outside the lock so they can re-acquire it
        old_event.set()
        return record

    async def cancel(self, task_id: str) -> bool:
        # Acquire the lock to match every other store mutation. Event.set()
        # and Task.cancel() are themselves thread-safe so we drop the lock
        # before calling them to avoid holding it across cooperative yields.
        async with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            return False
        record._cancel_event.set()
        if record._bg_task and not record._bg_task.done():
            record._bg_task.cancel()
        return True

    async def add_delta(self, task_id: str, delta: dict) -> None:
        """Append a worldstate-delta entry to the task's pending list.

        Called when a tool with a declared effect succeeds mid-run.
        The accumulated deltas are emitted as a DataPart artifact
        on the terminal task so Workstacean's effect-domain interceptor can
        publish them as ``world.state.delta`` events.
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.deltas.append(delta)

    async def add_usage(self, task_id: str, input_tokens: int, output_tokens: int) -> None:
        """Accumulate LLM token usage for the task.

        Called from the producer on every ``on_chat_model_end`` event. The
        running totals are emitted on the terminal artifact under the
        ``cost-v1`` MIME so Workstacean's cost interceptor can record
        per-skill samples (protoWorkstacean#372).
        """
        if input_tokens <= 0 and output_tokens <= 0:
            return
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.usage["input_tokens"] += int(input_tokens)
            record.usage["output_tokens"] += int(output_tokens)
            record.usage["total_tokens"] = (
                record.usage["input_tokens"] + record.usage["output_tokens"]
            )

    async def set_confidence(
        self,
        task_id: str,
        confidence: float,
        explanation: str | None = None,
    ) -> None:
        """Record the agent's self-reported confidence for this task.

        Called once from the producer when it parses a <confidence> tag out of
        the model's final output. Emitted on the terminal artifact under the
        confidence-v1 MIME. Clamped to [0, 1] defensively so the DataPart is
        always in-spec.
        """
        clamped = max(0.0, min(1.0, float(confidence)))
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.confidence = clamped
            if explanation and isinstance(explanation, str):
                record.confidence_explanation = explanation.strip() or None

    async def cancel_if_not_terminal(self, task_id: str) -> TaskRecord | None:
        """Atomically cancel a task iff it's not already terminal.

        Replaces the non-atomic get-state-then-update sequence in
        ``_cancel_task``: a runner could race between the check and the write
        and transition to COMPLETED while the caller assumed it was still
        cancellable. Returns the updated record, or None if the task was
        missing or already terminal (signal: HTTP 409 from the caller).
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.state in _TERMINAL:
                return None
            record.state = CANCELED
            record.updated_at = _now_iso()
            old_event = record._update_event
            record._update_event = asyncio.Event()
        old_event.set()
        record._cancel_event.set()
        if record._bg_task and not record._bg_task.done():
            record._bg_task.cancel()
        return record

    async def cleanup_expired(self, ttl_seconds: int = _DEFAULT_TERMINAL_TTL_S) -> int:
        """Remove terminal tasks whose ``updated_at`` is older than ttl_seconds.

        Returns the count removed. Working / submitted tasks are never evicted —
        they stay until they reach a terminal state, then age out normally.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - ttl_seconds
        removed = 0
        async with self._lock:
            for tid in list(self._tasks.keys()):
                r = self._tasks[tid]
                if r.state not in _TERMINAL:
                    continue
                try:
                    ts = datetime.fromisoformat(r.updated_at).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    del self._tasks[tid]
                    removed += 1
        if removed:
            logger.debug("[a2a] evicted %d expired terminal task(s)", removed)
        return removed

    def start_cleanup(
        self,
        interval_s: int = _DEFAULT_CLEANUP_INTERVAL_S,
        ttl_s: int = _DEFAULT_TERMINAL_TTL_S,
    ) -> None:
        """Start the background eviction loop. Idempotent — safe to call from
        every request handler. No-op if already running.

        Lazy rather than eager because __init__ runs at module import time,
        before an asyncio event loop exists.
        """
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval_s, ttl_s))

    async def _cleanup_loop(self, interval_s: int, ttl_s: int) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.cleanup_expired(ttl_s)
            except Exception as exc:
                logger.warning("[a2a] cleanup loop error: %s", exc)


# Module-level singleton — one store per process
_store = A2ATaskStore()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_artifact_parts(record: TaskRecord) -> list[dict]:
    """Build the terminal artifact's ``parts`` list: accumulated text plus
    DataParts for any A2A extension payloads observed during the run.

    Workstacean's A2A executor scans artifact parts for DataParts carrying
    one of these ``metadata.mimeType`` values and surfaces the payload
    through the matching interceptor:

    - ``WORLDSTATE_DELTA_MIME`` — effect-domain interceptor republishes
      observed mutations as ``world.state.delta`` events.
    - ``COST_MIME`` — cost interceptor records per-skill token + duration
      samples (protoWorkstacean#372).

    Each DataPart is emitted only when there's something to report, so an
    empty payload doesn't pollute the artifact.
    """
    parts: list[dict] = []
    if record.accumulated_text:
        parts.append({"kind": "text", "text": record.accumulated_text})
    if record.deltas:
        parts.append({
            "kind": "data",
            "data": {"deltas": list(record.deltas)},
            "metadata": {"mimeType": WORLDSTATE_DELTA_MIME},
        })
    cost_data = _cost_payload(record)
    if cost_data is not None:
        parts.append({
            "kind": "data",
            "data": cost_data,
            "metadata": {"mimeType": COST_MIME},
        })
    confidence_data = _confidence_payload(record)
    if confidence_data is not None:
        parts.append({
            "kind": "data",
            "data": confidence_data,
            "metadata": {"mimeType": CONFIDENCE_MIME},
        })
    return parts


def _confidence_payload(record: TaskRecord) -> dict | None:
    """Build the confidence-v1 payload, or None if the agent didn't
    self-report a confidence this run.

    ``success`` is derived from the terminal state — COMPLETED is the only
    truthy case. Reporting confidence on a FAILED run is exactly the
    "high-confidence failure" calibration signal, so it's still emitted.
    """
    if record.confidence is None:
        return None
    payload: dict = {
        "confidence": record.confidence,
        "success": record.state == COMPLETED,
    }
    if record.confidence_explanation:
        payload["confidenceExplanation"] = record.confidence_explanation
    return payload


def _cost_payload(record: TaskRecord) -> dict | None:
    """Build the cost-v1 payload for a terminal record, or None if no
    cost-relevant data is available.

    Always includes ``durationMs`` (cheap to compute from created_at →
    updated_at) when usage was tracked. ``costUsd`` is omitted for now —
    LiteLLM exposes per-call cost in its callback hooks but the runtime
    doesn't capture it yet; consumers of cost-v1 can derive cost from
    usage + their own per-model rates, or wait for a follow-up that
    plumbs ``response_cost`` through.
    """
    usage = record.usage
    if not usage or usage.get("total_tokens", 0) <= 0:
        return None
    payload: dict = {"usage": dict(usage)}
    duration_ms = _duration_ms(record)
    if duration_ms is not None:
        payload["durationMs"] = duration_ms
    return payload


def _duration_ms(record: TaskRecord) -> int | None:
    """Compute task duration from created_at → updated_at ISO timestamps."""
    try:
        start = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
        return int((end - start).total_seconds() * 1000)
    except (ValueError, AttributeError):
        return None


def _task_to_response(record: TaskRecord) -> dict:
    """Full Task snapshot (the spec's ``Task`` type).

    Returned as the result for ``message/send``, ``tasks/get``, and
    ``tasks/resubscribe``'s initial frame, plus the first frame of
    every ``message/stream``. The ``kind: "task"`` discriminator is
    what lets ``@a2a-js/sdk`` routes the event into its Task handler
    instead of silently discarding it.
    """
    resp: dict[str, Any] = {
        "kind": "task",
        "id": record.id,
        "contextId": record.context_id,
        "status": {"state": record.state, "timestamp": record.updated_at},
    }
    parts = _terminal_artifact_parts(record)
    if parts:
        resp["artifacts"] = [{
            "artifactId": record.id,
            "parts": parts,
        }]
    if record.error_message:
        resp["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.error_message}],
        }
    return resp


def _build_status_event(record: TaskRecord, *, final: bool = False) -> dict:
    """A2A ``TaskStatusUpdateEvent`` — progressive state transitions.

    Requires the ``kind: "status-update"`` discriminator plus camelCase
    field names (``taskId``, ``contextId``) per the A2A spec; without
    them ``@a2a-js/sdk`` silently skips the event and Workstacean's
    TaskTracker never sees the task. ``final=true`` signals the last
    transition in this stream (used on COMPLETED / FAILED / CANCELED).
    """
    evt: dict[str, Any] = {
        "kind": "status-update",
        "taskId": record.id,
        "contextId": record.context_id,
        "status": {"state": record.state, "timestamp": record.updated_at},
        "final": final,
    }
    if record.error_message:
        evt["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.error_message}],
        }
    elif record.last_status_message and record.state not in _TERMINAL:
        # Surface tool_start / tool_end messages to SSE subscribers. Cleared
        # on terminal transitions so consumers see the final state cleanly.
        evt["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.last_status_message}],
        }
    return evt


def _build_artifact_event(
    record: TaskRecord,
    *,
    text: str | None = None,
    append: bool = True,
    last_chunk: bool,
) -> dict:
    """A2A ``TaskArtifactUpdateEvent`` — streamed artifact chunks.

    Requires ``kind: "artifact-update"`` + camelCase ``lastChunk`` per
    the A2A spec; the artifact itself carries an ``artifactId`` so
    clients can correlate streamed chunks across events. Use
    ``text=<delta>`` + ``append=True`` for mid-run deltas, or
    ``text=<full>`` + ``append=False`` to replace the artifact (initial
    snapshot on :subscribe, terminal frame).
    """
    body_text = text if text is not None else record.accumulated_text
    return {
        "kind": "artifact-update",
        "taskId": record.id,
        "contextId": record.context_id,
        "artifact": {
            "artifactId": record.id,
            "parts": [{"kind": "text", "text": body_text}],
        },
        "append": append,
        "lastChunk": last_chunk,
    }


def _build_terminal_artifact_event(record: TaskRecord) -> dict:
    """Terminal artifact: full text + worldstate-delta DataPart if any.

    Used on COMPLETED for both the streaming and :subscribe paths so
    consumers see the authoritative final artifact (``append: false``,
    ``lastChunk: true``) with every accumulated delta attached.
    """
    return {
        "kind": "artifact-update",
        "taskId": record.id,
        "contextId": record.context_id,
        "artifact": {
            "artifactId": record.id,
            "parts": _terminal_artifact_parts(record),
        },
        "append": False,
        "lastChunk": True,
    }


def _extract_text_and_context(message: dict, context_id: str = "") -> tuple[str, str]:
    """Pull text + contextId out of an A2A Message dict."""
    parts = message.get("parts", [])
    text = next((p.get("text", "") for p in parts if p.get("kind") == "text"), "")
    if not text:
        text = next((p.get("text", "") for p in parts), "")
    context_id = context_id or f"a2a-{uuid4()}"
    return text, context_id


def _parse_allowlist() -> tuple[frozenset[str], tuple]:
    """Parse the webhook allowlist env vars once per import.

    ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` is a comma-separated list of
    hostnames (e.g. ``workstacean,automaker-server``) that bypass the
    SSRF check entirely — trusted internal agents on the docker
    network where every hostname resolves to an RFC1918 address by
    design.

    ``PUSH_NOTIFICATION_ALLOWED_CIDRS`` is a comma-separated list of
    CIDR ranges (e.g. ``10.0.14.0/24``) that bypass the SSRF check
    when the resolved IP falls inside any of them.

    Both are empty by default — the guard stays default-deny for any
    caller the operator hasn't explicitly trusted.
    """
    import ipaddress

    hosts_raw = os.environ.get("PUSH_NOTIFICATION_ALLOWED_HOSTS", "")
    cidrs_raw = os.environ.get("PUSH_NOTIFICATION_ALLOWED_CIDRS", "")
    hosts = frozenset(h.strip() for h in hosts_raw.split(",") if h.strip())
    cidrs = []
    for c in cidrs_raw.split(","):
        c = c.strip()
        if not c:
            continue
        try:
            cidrs.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            logger.warning("[a2a] ignoring malformed CIDR in allowlist: %s", c)
    return hosts, tuple(cidrs)


_ALLOWED_HOSTS, _ALLOWED_CIDRS = _parse_allowlist()


def _is_safe_webhook_url(url: str) -> bool:
    """Reject unsafe webhook targets before we accept a push config.

    Defends against SSRF: a client supplying http://169.254.169.254/... or
    http://10.0.0.1/... as a webhook would have the agent POST task payloads to
    internal cloud metadata, adjacent private services, or the loopback
    device. One-time resolution is not a full defence against DNS rebinding,
    but it closes the trivial "just give it a RFC1918 literal" vector.

    Accepts:
    - http/https URLs to globally-routable IPs.
    - Hostnames in ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` (trusted docker-network
      agents that resolve to RFC1918 by design).
    - Resolved IPs falling inside ``PUSH_NOTIFICATION_ALLOWED_CIDRS``.

    Rejects: non-http(s) schemes, unresolvable hostnames, and anything that
    resolves to loopback / link-local / private / multicast / reserved
    addresses that isn't explicitly allowlisted.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False

    # Hostname allowlist takes precedence — trusted docker-network agents
    # where the DNS name resolves to an RFC1918 address by design.
    if host in _ALLOWED_HOSTS:
        return True

    # If the hostname is already a literal IP, check it directly; otherwise
    # resolve once and check every returned address (multi-A / AAAA).
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            # getaddrinfo returns (family, type, proto, canonname, sockaddr);
            # sockaddr[0] is the IP for both AF_INET and AF_INET6.
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            return False

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if _ALLOWED_CIDRS and any(ip in cidr for cidr in _ALLOWED_CIDRS):
            continue  # CIDR allowlist bypass — trust this address
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _parse_push_config(configuration: dict) -> PushNotificationConfig | None:
    cfg = (configuration or {}).get("pushNotificationConfig") or (configuration or {}).get("taskPushNotificationConfig")
    if not cfg or not cfg.get("url"):
        return None
    url = cfg["url"]
    if not _is_safe_webhook_url(url):
        logger.warning("[a2a] rejected unsafe webhook url: %s", url)
        return None
    return PushNotificationConfig(
        url=url,
        token=_extract_push_token(cfg),
        id=cfg.get("id", str(uuid4())),
    )


def _extract_push_token(cfg: dict) -> str | None:
    """Pull the bearer token from a PushNotificationConfig.

    The A2A spec's PushNotificationConfig allows two shapes for the
    authentication secret, and clients choose between them at will:

    1. **Top-level ``token``** — the simple form every SDK produces by
       default. ``@a2a-js/sdk`` serialises ``{url, token}`` directly
       and Workstacean's ``SkillDispatcherPlugin`` takes this path
       (``a2a-executor.ts:329`` → ``setTaskPushNotificationConfig({
       pushNotificationConfig: { url, token } })``).
    2. **Structured ``authentication.credentials``** — the RFC-8821
       ``AuthenticationInfo`` form with ``schemes`` + ``credentials``.

    Real-world consumers split: Workstacean's ``SkillDispatcherPlugin``
    sends the top-level form (1), while other A2A clients send the
    structured form (2). Both must work or one or the other will silently
    fall back to polling.

    Preference: top-level ``token`` first (what most callers send), fall
    back to ``authentication.credentials``. Either one null-safely
    resolves to ``None`` which disables the header entirely — the same
    behaviour as the SSRF guard letting a public-IP callback through
    unauthenticated.
    """
    top_level = cfg.get("token")
    if isinstance(top_level, str) and top_level:
        return top_level
    auth = cfg.get("authentication") or {}
    creds = auth.get("credentials")
    return creds if isinstance(creds, str) and creds else None


# ── Webhook delivery ──────────────────────────────────────────────────────────


async def _deliver_webhook(record: TaskRecord, push_config: PushNotificationConfig) -> None:
    """POST a TaskStatusUpdateEvent to the configured webhook URL.

    Retries 3× with exponential backoff (1s / 3s / 9s).
    Skips retry on 4xx (client error — retrying won't help).
    """
    payload = _build_status_event(record, final=record.state in _TERMINAL)
    if record.state == COMPLETED:
        parts = _terminal_artifact_parts(record)
        if parts:
            payload["artifact"] = {
                "artifactId": record.id,
                "parts": parts,
                "append": False,
                "lastChunk": True,
            }

    headers = {"Content-Type": "application/json"}
    if push_config.token:
        headers["Authorization"] = f"Bearer {push_config.token}"

    backoff = [1, 3, 9]
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt, delay in enumerate(backoff):
            try:
                resp = await client.post(push_config.url, json=payload, headers=headers)
                if resp.status_code < 500:
                    # INFO (not DEBUG) — the absence of this line in DEBUG
                    # builds historically made webhook delivery issues
                    # invisible under the default WARNING log threshold.
                    logger.info(
                        "[a2a] webhook delivered task=%s state=%s → %s (%s)",
                        record.id, record.state, push_config.url, resp.status_code,
                    )
                    return
                logger.warning("[a2a] webhook 5xx (attempt %d): %s", attempt + 1, resp.status_code)
            except httpx.RequestError as exc:
                logger.warning("[a2a] webhook request error (attempt %d): %s", attempt + 1, exc)
            if attempt < len(backoff) - 1:
                await asyncio.sleep(delay)

    logger.error("[a2a] webhook failed after %d attempts: %s", len(backoff), push_config.url)


# Strong references to in-flight webhook delivery tasks. Without this the
# asyncio loop holds only weak references (Python 3.11+ docs warn about this
# explicitly) and a pending delivery can be garbage-collected mid-retry,
# silently dropping the status transition a caller registered a webhook to
# receive.
_pending_webhook_tasks: set[asyncio.Task] = set()


async def _push(record: TaskRecord) -> None:
    """Fire webhook delivery for *record* if a push config is currently
    registered on it.

    Reads record.push_config at call time rather than closing over the
    submit-time value — otherwise a caller who registered a webhook via
    POST /tasks/{id}/pushNotificationConfigs *after* submitting would
    never receive any state transitions.
    """
    cfg = record.push_config
    if cfg and record.state in _TERMINAL | {WORKING}:
        task = asyncio.create_task(_deliver_webhook(record, cfg))
        _pending_webhook_tasks.add(task)
        task.add_done_callback(_pending_webhook_tasks.discard)


# ── Background task runner ────────────────────────────────────────────────────


async def _run_task_background(
    task_id: str,
    stream_fn: Callable[[], AsyncGenerator],
) -> None:
    """Run LangGraph in the background, writing state updates to the task store."""
    record = await _store.update_state(task_id, WORKING)
    if record is None:
        return
    await _push(record)

    accumulated = ""
    try:
        async for event_type, payload in stream_fn():
            record = await _store.get(task_id)
            if record is None:
                return
            if record._cancel_event.is_set():
                canceled = await _store.update_state(task_id, CANCELED)
                if canceled is not None:
                    await _push(canceled)
                return

            if event_type == "text":
                accumulated += payload
                await _store.update_state(task_id, WORKING, accumulated_text=accumulated)

            elif event_type in ("tool_start", "tool_end"):
                # Status update only — preserve the tool message on the record
                # so SSE subscribers see it (both the initial message/sendStream
                # consumer and any :subscribe reconnect).
                await _store.update_state(
                    task_id, WORKING,
                    accumulated_text=accumulated,
                    status_message=payload,
                )

            elif event_type == "delta":
                # Worldstate-delta emitted by a tool that mutated shared state.
                # Stored on the record and emitted on the terminal artifact.
                if isinstance(payload, dict):
                    await _store.add_delta(task_id, payload)

            elif event_type == "usage":
                # Token usage from on_chat_model_end. Accumulated on the
                # record and emitted on the terminal artifact under the
                # cost-v1 MIME for Workstacean's cost interceptor.
                if isinstance(payload, dict):
                    await _store.add_usage(
                        task_id,
                        input_tokens=payload.get("input_tokens", 0),
                        output_tokens=payload.get("output_tokens", 0),
                    )

            elif event_type == "confidence":
                # Self-reported confidence parsed from the model's <confidence>
                # tag. Yielded before "done" so it lands on the COMPLETED
                # artifact's confidence-v1 DataPart.
                if isinstance(payload, dict) and payload.get("confidence") is not None:
                    await _store.set_confidence(
                        task_id,
                        confidence=payload["confidence"],
                        explanation=payload.get("explanation"),
                    )

            elif event_type == "done":
                record = await _store.update_state(
                    task_id,
                    COMPLETED,
                    accumulated_text=payload or accumulated,
                )
                await _push(record)
                return

            elif event_type == "error":
                record = await _store.update_state(task_id, FAILED, error=payload)
                await _push(record)
                return

    except asyncio.CancelledError:
        canceled = await _store.update_state(task_id, CANCELED)
        if canceled is not None:
            await _push(canceled)
        raise
    except Exception as exc:
        logger.exception("[a2a] background task %s crashed", task_id)
        record = await _store.update_state(task_id, FAILED, error=str(exc))
        if record is not None:
            await _push(record)


# ── SSE helpers ───────────────────────────────────────────────────────────────

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# How long a subscriber blocks on the rotating _update_event before yielding
# a keepalive comment. Tuned to stay comfortably below typical reverse-proxy
# idle timeouts (nginx default: 60s) while minimising chatter.
_SSE_KEEPALIVE_TIMEOUT_S = 25


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _sse_rpc(rpc_id: Any, result: dict) -> str:
    return _sse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


# ── Shared SSE consumer ───────────────────────────────────────────────────────


async def _watch_task(
    task_id: str,
    start_text_len: int = 0,
) -> AsyncGenerator[tuple[str, TaskRecord | None, str | None], None]:
    """Yield change notifications for a running task until it terminates.

    This is the shared consumer behind both ``message/sendStream`` and
    ``:subscribe``. The producer (``_run_task_background``) runs independently
    as ``record._bg_task``; the consumer only reads the store and awaits the
    rotating ``_update_event``. Dropping the SSE connection no longer stops
    the producer — a reconnect via ``:subscribe`` resumes where the previous
    connection left off.

    Yield tuples are ``(kind, record, payload)`` where kind is one of:
      - ``"status"``: state transition or tool message. payload is None;
        consumers format via ``_build_status_event(record)``.
      - ``"text_delta"``: ``accumulated_text`` grew. payload is the new
        suffix only — never the full accumulated text — so reconnects do
        not duplicate content on the wire.
      - ``"keepalive"``: timed out waiting for an update; record is None.
        Consumers should emit ``": keepalive\\n\\n"`` to keep the proxy happy.

    ``start_text_len`` is the length of ``accumulated_text`` the client has
    already seen. First-connect callers pass 0. :subscribe reconnects pass
    ``len(record.accumulated_text)`` so only genuinely-new suffix text is
    emitted. Callers that want to replay the full artifact on reconnect
    (initial snapshot UX) emit that frame themselves and then start the
    watcher at ``start_text_len = len(record.accumulated_text)``.

    Terminates when the task is deleted or reaches a terminal state. The
    final status frame is always yielded before return.
    """
    record = await _store.get(task_id)
    if record is None:
        return

    last_sent_len = start_text_len

    # Emit the current snapshot first so (re)connecting clients see the
    # state of the world before blocking on the next update.
    yield ("status", record, None)
    if record.accumulated_text and len(record.accumulated_text) > last_sent_len:
        delta = record.accumulated_text[last_sent_len:]
        last_sent_len = len(record.accumulated_text)
        yield ("text_delta", record, delta)

    if record.state in _TERMINAL:
        return

    while True:
        r = await _store.get(task_id)
        if r is None:
            return

        next_event = r._update_event
        try:
            await asyncio.wait_for(next_event.wait(), timeout=_SSE_KEEPALIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            yield ("keepalive", None, None)
            continue

        r = await _store.get(task_id)
        if r is None:
            return

        yield ("status", r, None)
        if r.accumulated_text and len(r.accumulated_text) > last_sent_len:
            delta = r.accumulated_text[last_sent_len:]
            last_sent_len = len(r.accumulated_text)
            yield ("text_delta", r, delta)

        if r.state in _TERMINAL:
            return


# ── Auth helper ───────────────────────────────────────────────────────────────


def _check_auth(request: Request, api_key: str) -> None:
    if api_key and request.headers.get("x-api-key") != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Route factory ─────────────────────────────────────────────────────────────


# Module-level mutable holder for the bearer token so hosts can
# update it at runtime without re-registering routes (e.g. when the
# setup wizard captures a token post-boot). ``register_a2a_routes``
# seeds this from its ``auth_token`` argument (or ``A2A_AUTH_TOKEN``
# env as fallback); ``set_a2a_token`` updates it live. Closures inside
# ``register_a2a_routes`` read ``_A2A_TOKEN[0]`` on every request, so
# a mutation is picked up by the next incoming call.
_A2A_TOKEN: list[str | None] = [None]


def set_a2a_token(token: str | None) -> None:
    """Update the active A2A bearer token at runtime.

    Called by the host (e.g. ``server.py``) after the wizard / drawer
    changes ``auth.token`` in the YAML — without this, bearer auth
    captured at register time would stay stale until process restart.
    """
    _A2A_TOKEN[0] = (token or "").strip() or None


def register_a2a_routes(
    app: FastAPI,
    chat_stream_fn_factory: Callable[..., AsyncGenerator],
    chat_fn: Callable,  # kept for potential future use / testing
    api_key: str,
    agent_card: dict,
    register_card_route: bool = True,
    auth_token: str = "",
) -> None:
    """Register all A2A routes on *app* and update *agent_card* capabilities.

    Host apps that already serve the agent card themselves (e.g. at multiple
    well-known paths for sdk compat) should pass ``register_card_route=False``
    so FastAPI does not raise on a duplicate route registration.

    ``auth_token`` seeds the bearer-token check. When empty, falls
    back to the ``A2A_AUTH_TOKEN`` env var. Hosts can update the
    active token post-registration via ``set_a2a_token(...)`` (e.g.
    after a wizard-driven config reload) without needing a restart.
    """

    # ── Bearer token authentication ───────────────────────────────────────────
    # Seed order: explicit arg > env. Stored in the module-level holder
    # so mutations propagate to the closure below.
    seed = (auth_token or os.environ.get("A2A_AUTH_TOKEN", "") or "").strip()
    _A2A_TOKEN[0] = seed or None
    if _A2A_TOKEN[0] is None:
        logger.warning(
            "[a2a] A2A auth token not configured — endpoint is open"
        )

    def _check_bearer_auth(request: Request) -> None:
        """Validate Authorization: Bearer <token> against the active
        token. No-ops when unset. Raises HTTP 401 on missing/invalid.

        Reads ``_A2A_TOKEN[0]`` on every call so runtime updates via
        ``set_a2a_token`` are honored without route re-registration.
        """
        active = _A2A_TOKEN[0]
        if not active:
            return
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: expected 'Authorization: Bearer <token>'",
            )
        provided = auth_header[len("Bearer "):]
        if not hmac.compare_digest(provided, active):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid bearer token")

    # ── Origin verification for SSE/streaming endpoints ───────────────────────
    _raw_allowed_origins = os.environ.get("A2A_ALLOWED_ORIGINS", "")
    _allowed_origins_str = _raw_allowed_origins.strip()
    if not _allowed_origins_str:
        logger.warning(
            "[a2a] A2A_ALLOWED_ORIGINS not set — SSE/streaming origin verification disabled"
        )
        _allowed_origins: list[str] | None = None
    elif _allowed_origins_str == "*":
        _allowed_origins = None  # wildcard: verification disabled
    else:
        _allowed_origins = [o.strip().lower() for o in _allowed_origins_str.split(",") if o.strip()]

    def _check_origin(request: Request) -> None:
        """Validate Origin header against A2A_ALLOWED_ORIGINS.

        No-ops when A2A_ALLOWED_ORIGINS is unset or '*'.
        Raises HTTP 403 when Origin is not in the allowlist.
        """
        if _allowed_origins is None:
            return
        origin = request.headers.get("Origin", "").lower()
        if origin not in _allowed_origins:
            raise HTTPException(status_code=403, detail="Forbidden: origin not allowed")

    # Update agent card capabilities
    agent_card.setdefault("capabilities", {})
    agent_card["capabilities"]["streaming"] = True
    agent_card["capabilities"]["pushNotifications"] = True
    if _A2A_TOKEN[0]:
        agent_card.setdefault("securitySchemes", {})
        agent_card["securitySchemes"]["bearer"] = {
            "type": "http",
            "scheme": "bearer",
        }

    # ── Agent card ────────────────────────────────────────────────────────────

    if register_card_route:
        @app.get("/.well-known/agent.json", include_in_schema=False)
        async def _agent_card_route():
            return agent_card

    # ── Shared submit helper ──────────────────────────────────────────────────

    async def _submit_task(
        text: str,
        context_id: str,
        push_config: PushNotificationConfig | None,
        caller_trace: dict | None = None,
    ) -> TaskRecord:
        """Create a TaskRecord, fire the background runner, return immediately."""
        _store.start_cleanup()

        task_id = str(uuid4())
        now = _now_iso()
        record = TaskRecord(
            id=task_id,
            context_id=context_id,
            state=SUBMITTED,
            created_at=now,
            updated_at=now,
            message_text=text,
            push_config=push_config,
        )
        await _store.create(record)

        ct = caller_trace or {}
        bg = asyncio.create_task(
            _run_task_background(
                task_id,
                lambda: chat_stream_fn_factory(text, context_id, caller_trace=ct),
            )
        )
        record._bg_task = bg
        logger.info("[a2a] task %s submitted (context=%s)", task_id, context_id)
        return record

    # ── Streaming SSE generator ───────────────────────────────────────────────

    async def _stream_new_task(
        text: str,
        context_id: str,
        push_config: PushNotificationConfig | None,
        rpc_id: Any = None,
        caller_trace: dict | None = None,
    ):
        """Submit a new task and stream its lifecycle as JSON-RPC SSE frames.

        The producer (``_run_task_background``) runs as ``record._bg_task``
        independently of this generator — if the SSE connection drops, work
        continues and the client can reattach via ``:subscribe``.

        Emits incremental text deltas only (``append: true``) for the
        streaming window, and the authoritative terminal artifact (full
        text + worldstate-delta DataPart, ``append: false``) on the terminal
        frame. Reconnects see the pre-disconnect text via ``:subscribe``'s
        snapshot, then continue from there.
        """
        record = await _submit_task(text, context_id, push_config, caller_trace)
        task_id = record.id

        # Frame 0: initial Task snapshot — `kind: "task"`, full Task
        # object per the A2A spec. @a2a-js/sdk routes this into its
        # Task handler; without `kind` it would silently skip the event
        # and Workstacean's TaskTracker would never see the task.
        yield _sse_rpc(rpc_id, _task_to_response(record))

        try:
            async for kind, r, payload in _watch_task(task_id, start_text_len=0):
                if kind == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                if r is None:
                    return

                if kind == "status":
                    # COMPLETED gets TWO events per A2A spec: the terminal
                    # artifact-update (full text + worldstate DataPart,
                    # append=false, lastChunk=true), then a final
                    # status-update (final=true) to close the stream.
                    if r.state == COMPLETED:
                        yield _sse_rpc(rpc_id, _build_terminal_artifact_event(r))
                        yield _sse_rpc(rpc_id, _build_status_event(r, final=True))
                    else:
                        yield _sse_rpc(
                            rpc_id,
                            _build_status_event(r, final=r.state in _TERMINAL),
                        )

                elif kind == "text_delta":
                    # Mid-run delta: just the new suffix, append=true. Only
                    # emitted when the task is still WORKING — terminal
                    # deltas roll into the artifact-update on the next
                    # status event.
                    if r.state not in _TERMINAL and payload:
                        yield _sse_rpc(
                            rpc_id,
                            _build_artifact_event(
                                r, text=payload, append=True, last_chunk=False,
                            ),
                        )
        except asyncio.CancelledError:
            # The HTTP connection closed (client disconnect). DO NOT cancel
            # the background task — it continues running, and :subscribe
            # can reattach. Just stop emitting.
            logger.info("[a2a] stream consumer for %s disconnected; bg task continues", task_id)
            raise

    # ── JSON-RPC tasks/resubscribe SSE ───────────────────────────────────────

    async def _resubscribe_jsonrpc_stream(task_id: str, rpc_id: Any):
        """Mirror of the REST :subscribe path but every frame is wrapped in
        a JSON-RPC envelope keyed to ``rpc_id`` so SDK clients can thread
        the stream back to the request they issued. Snapshot is emitted as
        an ``append: false`` full-text frame; subsequent updates are
        ``append: true`` deltas only (no duplicated content on the wire).
        """
        snapshot = await _store.get(task_id)
        if snapshot is None:
            return
        snapshot_len = len(snapshot.accumulated_text)

        # Initial snapshot — emit the full Task object (kind: "task") so
        # the client can reconstruct the whole state, then stream only
        # the deltas after that.
        yield _sse_rpc(rpc_id, _task_to_response(snapshot))

        try:
            async for kind, r, payload in _watch_task(
                task_id, start_text_len=snapshot_len,
            ):
                if kind == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                if r is None:
                    return
                if kind == "status":
                    if r.state == COMPLETED:
                        yield _sse_rpc(rpc_id, _build_terminal_artifact_event(r))
                        yield _sse_rpc(rpc_id, _build_status_event(r, final=True))
                    else:
                        yield _sse_rpc(
                            rpc_id,
                            _build_status_event(r, final=r.state in _TERMINAL),
                        )
                elif kind == "text_delta":
                    if r.state not in _TERMINAL and payload:
                        yield _sse_rpc(
                            rpc_id,
                            _build_artifact_event(
                                r, text=payload, append=True, last_chunk=False,
                            ),
                        )
        except asyncio.CancelledError:
            logger.info(
                "[a2a] resubscribe consumer for %s disconnected; bg task continues",
                task_id,
            )
            raise

    # ── POST /a2a  (JSON-RPC 2.0 — full spec surface) ────────────────────────
    # The @a2a-js/sdk Client exclusively uses JSON-RPC over this endpoint for
    # every operation: message/*, tasks/*, tasks/pushNotificationConfig/*.
    # Historically only message/send + message/sendStream were routed here,
    # which silently broke every other SDK call path with -32601. See the
    # handler docstring at the top of this module for the full method list.

    @app.post("/a2a", include_in_schema=False)
    async def _a2a_rpc(request: Request, req: dict):
        if api_key and request.headers.get("x-api-key") != api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        try:
            _check_bearer_auth(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        rpc_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        def _rpc_error(code: int, message: str):
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": code, "message": message}}

        def _rpc_result(result):
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

        # ── message/send, message/stream ─────────────────────────────────────
        # Both methods take a Message and create a new Task. `message/stream`
        # is the spec name; `message/sendStream` is a deprecated protoPen-era
        # alias kept so nothing that already sends the old name breaks.
        if method in ("message/send", "message/stream", "message/sendStream"):
            message = params.get("message") or {}
            parts = message.get("parts") or []
            text = next((p.get("text", "") for p in parts
                         if p.get("kind") == "text"), "")
            if not text:
                text = next((p.get("text", "") for p in parts), "")
            if not text:
                return _rpc_error(-32602, "Invalid params: message must contain a text part")

            context_id = params.get("contextId") or f"a2a-{uuid4()}"
            configuration = params.get("configuration") or {}
            push_config = _parse_push_config(configuration)

            # a2a.trace — distributed Langfuse trace propagation.
            # The caller stamps its current traceId + spanId into
            # params.metadata["a2a.trace"]. We read it and forward to
            # trace_session so this agent's Langfuse trace cross-references
            # the caller's.
            msg_metadata = params.get("metadata") or {}
            caller_trace = msg_metadata.get("a2a.trace") or {}

            if method in ("message/stream", "message/sendStream"):
                try:
                    _check_origin(request)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

            if method == "message/send":
                record = await _submit_task(text, context_id, push_config, caller_trace)
                # Use _task_to_response so the `kind: "task"` discriminator
                # and every other Task field land consistently with the
                # SSE / REST / tasks/get paths. Building the dict inline
                # omitted `kind`, which `@a2a-js/sdk` uses to route the
                # result into its Task handler (spec-compliance with the
                # a2a-streaming guide).
                return _rpc_result(_task_to_response(record))

            # streaming path — SSE frames wrapped in JSON-RPC envelopes
            return StreamingResponse(
                _stream_new_task(text, context_id, push_config, rpc_id=rpc_id, caller_trace=caller_trace),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        # ── tasks/get ─────────────────────────────────────────────────────────
        if method == "tasks/get":
            task_id = params.get("id")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: id is required")
            record = await _store.get(task_id)
            if record is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            return _rpc_result(_task_to_response(record))

        # ── tasks/cancel ──────────────────────────────────────────────────────
        # Atomic via cancel_if_not_terminal; fires a push notification so
        # webhook consumers see the cancel transition (same as the REST route).
        if method == "tasks/cancel":
            task_id = params.get("id")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: id is required")
            if await _store.get(task_id) is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            record = await _store.cancel_if_not_terminal(task_id)
            if record is None:
                existing = await _store.get(task_id)
                state = existing.state if existing else "unknown"
                return _rpc_error(-32002, f"Task already terminal: {state}")
            await _push(record)
            return _rpc_result(_task_to_response(record))

        # ── tasks/resubscribe ─────────────────────────────────────────────────
        # JSON-RPC-framed SSE reconnect. Mirrors the REST :subscribe route
        # but wraps every frame in a JSON-RPC envelope keyed to rpc_id so
        # the client library can thread the stream back to its request.
        if method == "tasks/resubscribe":
            task_id = params.get("id")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: id is required")
            if await _store.get(task_id) is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            try:
                _check_origin(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return StreamingResponse(
                _resubscribe_jsonrpc_stream(task_id, rpc_id),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        # ── tasks/pushNotificationConfig/{set,get,list,delete} ────────────────
        # The task store today holds a single PushNotificationConfig per task;
        # list returns [cfg] or [] to satisfy the spec shape.
        if method == "tasks/pushNotificationConfig/set":
            task_id = params.get("taskId")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: taskId is required")
            record = await _store.get(task_id)
            if record is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            cfg_in = params.get("pushNotificationConfig") or {}
            url = cfg_in.get("url", "")
            if not url:
                return _rpc_error(-32602, "Invalid params: pushNotificationConfig.url is required")
            if not _is_safe_webhook_url(url):
                return _rpc_error(
                    -32602,
                    "webhook url rejected: must be http/https, public IP, "
                    "not loopback/private/link-local/multicast/reserved",
                )
            cfg = PushNotificationConfig(
                url=url,
                token=_extract_push_token(cfg_in),
                id=cfg_in.get("id", str(uuid4())),
            )
            async with _store._lock:
                record.push_config = cfg
            # Fire immediately if the task already reached terminal state
            # before the caller got around to registering — otherwise the
            # webhook would never be delivered.
            if record.state in _TERMINAL:
                await _push(record)
            logger.info(
                "[a2a] push config registered (jsonrpc) task=%s state=%s → %s",
                task_id, record.state, cfg.url,
            )
            return _rpc_result({
                "taskId": task_id,
                "pushNotificationConfig": {"url": cfg.url, "id": cfg.id},
            })

        if method == "tasks/pushNotificationConfig/get":
            task_id = params.get("taskId")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: taskId is required")
            record = await _store.get(task_id)
            if record is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            cfg = record.push_config
            if cfg is None:
                return _rpc_result(None)
            return _rpc_result({
                "taskId": task_id,
                "pushNotificationConfig": {"url": cfg.url, "id": cfg.id},
            })

        if method == "tasks/pushNotificationConfig/list":
            task_id = params.get("taskId")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: taskId is required")
            record = await _store.get(task_id)
            if record is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            cfg = record.push_config
            if cfg is None:
                return _rpc_result([])
            return _rpc_result([{
                "taskId": task_id,
                "pushNotificationConfig": {"url": cfg.url, "id": cfg.id},
            }])

        if method == "tasks/pushNotificationConfig/delete":
            task_id = params.get("taskId")
            if not task_id:
                return _rpc_error(-32602, "Invalid params: taskId is required")
            record = await _store.get(task_id)
            if record is None:
                return _rpc_error(-32001, f"Task not found: {task_id}")
            async with _store._lock:
                record.push_config = None
            return _rpc_result(None)

        return _rpc_error(-32601, f"Method not found: {method}")

    # ── POST /message:send  (REST) ────────────────────────────────────────────

    @app.post("/message:send", include_in_schema=False)
    async def _rest_send(request: Request, body: dict):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        message = body.get("message", {})
        configuration = body.get("configuration", {})
        context_id = body.get("contextId", "")
        text, context_id = _extract_text_and_context(message, context_id)
        if not text:
            raise HTTPException(400, "No text content in message")
        push_config = _parse_push_config(configuration)
        record = await _submit_task(text, context_id, push_config)
        return JSONResponse(_task_to_response(record), status_code=202)

    # ── POST /message:stream  (REST SSE) ─────────────────────────────────────

    @app.post("/message:stream", include_in_schema=False)
    async def _rest_stream(request: Request, body: dict):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        _check_origin(request)
        message = body.get("message", {})
        configuration = body.get("configuration", {})
        context_id = body.get("contextId", "")
        text, context_id = _extract_text_and_context(message, context_id)
        if not text:
            raise HTTPException(400, "No text content in message")
        push_config = _parse_push_config(configuration)
        return StreamingResponse(
            _stream_new_task(text, context_id, push_config),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ── GET /tasks/{task_id} ──────────────────────────────────────────────────

    # ── GET /tasks/{task_id}:subscribe  (SSE reconnect) ──────────────────────
    # MUST be registered before the bare GET /tasks/{task_id} below: the
    # {task_id} param matches `[^/]+` (colons included), so the generic route
    # would otherwise capture `<id>:subscribe` as a task_id and 404.

    @app.get("/tasks/{task_id}:subscribe", include_in_schema=False)
    async def _subscribe_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        _check_origin(request)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")

        async def _sse_gen():
            # Initial snapshot: emit whatever text is already on the record as
            # an append=False replacement frame, then let _watch_task continue
            # from there with append=True deltas only. This gives reconnecting
            # clients one full payload + future incrementals — no duplication.
            snapshot = await _store.get(task_id)
            if snapshot is None:
                return
            snapshot_len = len(snapshot.accumulated_text)
            if snapshot.accumulated_text:
                yield _sse(_build_artifact_event(
                    snapshot,
                    text=snapshot.accumulated_text,
                    append=False,
                    last_chunk=snapshot.state in _TERMINAL,
                ))

            try:
                async for kind, r, payload in _watch_task(
                    task_id, start_text_len=snapshot_len,
                ):
                    if kind == "keepalive":
                        yield ": keepalive\n\n"
                        continue
                    if r is None:
                        return

                    if kind == "status":
                        if r.state == COMPLETED:
                            # Terminal: authoritative full artifact (text +
                            # worldstate-delta DataPart) as append=false.
                            yield _sse(_build_status_event(r))
                            yield _sse(_build_terminal_artifact_event(r))
                        else:
                            yield _sse(_build_status_event(r))

                    elif kind == "text_delta":
                        if r.state not in _TERMINAL and payload:
                            yield _sse(_build_artifact_event(
                                r, text=payload, append=True, last_chunk=False,
                            ))
            except asyncio.CancelledError:
                logger.info(
                    "[a2a] subscribe consumer for %s disconnected; bg task continues",
                    task_id,
                )
                raise

        return StreamingResponse(_sse_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.get("/tasks/{task_id}", include_in_schema=False)
    async def _get_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        return _task_to_response(record)

    # ── POST /tasks/{task_id}:cancel ──────────────────────────────────────────

    @app.post("/tasks/{task_id}:cancel", include_in_schema=False)
    async def _cancel_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        # Single atomic read+write under the store lock. The previous
        # get → sleep → cancel → update sequence could race with the
        # background runner and clobber a legitimate COMPLETED state.
        if await _store.get(task_id) is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        record = await _store.cancel_if_not_terminal(task_id)
        if record is None:
            # Either disappeared under us (very unlikely) or already terminal.
            existing = await _store.get(task_id)
            if existing is None:
                raise HTTPException(404, f"Task not found: {task_id}")
            raise HTTPException(409, f"Task already terminal: {existing.state}")
        # Webhook consumers should hear about the cancel transition, same as
        # any other terminal state.
        await _push(record)
        return _task_to_response(record)

    # ── POST /tasks/{task_id}/pushNotificationConfigs ─────────────────────────

    @app.post("/tasks/{task_id}/pushNotificationConfigs", include_in_schema=False)
    async def _create_push_config(task_id: str, request: Request, body: dict):
        _check_auth(request, api_key)
        _check_bearer_auth(request)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")

        url = body.get("url", "")
        if not url:
            raise HTTPException(400, "url is required")
        if not _is_safe_webhook_url(url):
            raise HTTPException(
                400,
                "webhook url rejected: must be http/https, public IP, "
                "not loopback/private/link-local/multicast/reserved",
            )

        cfg = PushNotificationConfig(
            url=url,
            token=_extract_push_token(body),
            id=body.get("id", str(uuid4())),
        )

        async with _store._lock:
            record.push_config = cfg

        # If task already terminal, fire webhook immediately via the tracked
        # _push path so the delivery task isn't GC'd mid-retry.
        if record.state in _TERMINAL:
            await _push(record)

        logger.info("[a2a] push config registered for task %s → %s", task_id, cfg.url)
        return {"id": cfg.id, "taskId": task_id, "url": cfg.url}

    logger.info("[a2a] routes registered (streaming=True, pushNotifications=True)")
