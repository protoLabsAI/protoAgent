"""A small async A2A 1.0 client for a running protoAgent, plus a frame decoder.

The wire facts are protoAgent's, and they are the same ones the fleet deck
(``deck/a2a.py``) and the console honour — this package cannot import either (it
must run against a remote fleet member with nothing of protoAgent installed), so
the handful it needs are restated here:

* proto method names (``SendStreamingMessage`` / ``CancelTask`` / ``GetTask``) and
  the mandatory ``A2A-Version: 1.0`` header — without it a2a-sdk falls back to 0.3
  and rejects those methods; ``role: "ROLE_USER"``; untyped ``parts: [{"text": …}]``;
* a streamed frame is a JSON-RPC envelope whose ``result`` carries exactly one of
  ``task`` / ``statusUpdate`` / ``artifactUpdate`` (0.3's flat ``kind`` is tolerated);
* tool calls ride the status MESSAGE's ``metadata[tool-call-v1 URI]`` — not a part
  — with ``phase`` started/completed/failed, ``args`` on the start and ``result`` on
  the end. A call is announced twice: once early with empty args, once with args;
* ``append`` on an artifact update has no wire presence when false (proto3), so text
  is appended ONLY on an explicit ``true``; the terminal frame is a full REPLACE;
* reasoning / hitl are MIME-discriminated DataParts in any of three encodings;
* a paused turn is ``input-required`` (not terminal) with a hitl-v1 part.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

A2A_VERSION = "1.0"
TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1"
COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1"
HITL_MIME = "application/vnd.protolabs.hitl-v1+json"
REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"

_TERMINAL = ("completed", "failed", "canceled", "cancelled", "rejected")
_PAUSED = ("input-required", "auth-required")

# A turn can run for minutes between frames only if the server stops keep-aliving;
# protoAgent's executor emits well inside this. Connect is short so a dead URL fails fast.
_STREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
_RPC_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class A2AError(RuntimeError):
    """The server refused (JSON-RPC error) or the transport failed."""


class A2AUnauthorized(A2AError):
    """401/403 — the credential is missing or wrong."""


def norm_state(state: Any) -> str:
    """``TASK_STATE_INPUT_REQUIRED`` / ``input_required`` → ``input-required``."""
    s = str(state or "")
    if s.startswith("TASK_STATE_"):
        s = s[len("TASK_STATE_") :]
    return s.lower().replace("_", "-")


# ── decoded events ────────────────────────────────────────────────────────────


@dataclass
class TextEvent:
    text: str
    append: bool


@dataclass
class ReasoningEvent:
    text: str


@dataclass
class ToolEvent:
    id: str
    name: str
    phase: str  # "start" | "end"
    args: Any = None
    result: Any = None
    error: bool = False
    parent_id: str | None = None


@dataclass
class StateEvent:
    state: str  # normalized
    task_id: str
    text: str = ""
    hitl: dict | None = None
    final: bool = False

    @property
    def terminal(self) -> bool:
        return self.final or self.state in _TERMINAL

    @property
    def paused(self) -> bool:
        return self.state in _PAUSED


@dataclass
class UsageEvent:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None


Event = TextEvent | ReasoningEvent | ToolEvent | StateEvent | UsageEvent


def _text_from_parts(parts: Any) -> str:
    out = []
    for p in parts or []:
        if isinstance(p, dict) and p.get("kind") in (None, "text") and p.get("text"):
            out.append(str(p["text"]))
    return "".join(out)


def _data_by_mime(parts: Any, mime: str) -> Any:
    """A DataPart's payload iff ``metadata.mimeType`` matches: 1.0 member-discriminated
    (``content.$case == "data"``), 1.0 flattened (``data``), or 0.3 (``kind: data``)."""
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        meta = p.get("metadata")
        if not isinstance(meta, dict) or meta.get("mimeType") != mime:
            continue
        content = p.get("content")
        if isinstance(content, dict) and content.get("$case") == "data":
            return content.get("value")
        return p.get("data")
    return None


def _tool_event(metadata: Any) -> ToolEvent | None:
    if not isinstance(metadata, dict):
        return None
    d = metadata.get(TOOL_CALL_EXT_URI)
    if not isinstance(d, dict):
        return None
    phase = str(d.get("phase") or "")
    return ToolEvent(
        id=str(d.get("toolCallId") or ""),
        name=str(d.get("name") or ""),
        phase="start" if phase == "started" else "end",
        args=d.get("args"),
        result=d.get("result") if d.get("result") is not None else d.get("error"),
        error=phase == "failed" or bool(d.get("error")),
        parent_id=str(d["parentToolCallId"]) if d.get("parentToolCallId") else None,
    )


def _usage_event(metadata: Any) -> UsageEvent | None:
    if not isinstance(metadata, dict):
        return None
    d = metadata.get(COST_EXT_URI)
    if not isinstance(d, dict) or not isinstance(d.get("usage"), dict):
        return None
    u = d["usage"]

    def n(k: str) -> int:
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    cost = d.get("costUsd")
    return UsageEvent(
        input_tokens=n("input_tokens"),
        output_tokens=n("output_tokens"),
        cache_read_tokens=n("cache_read_input_tokens"),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
    )


def _unwrap(frame: dict) -> tuple[dict | None, dict | None, dict | None]:
    result = frame.get("result")
    if not isinstance(result, dict):
        return None, None, None
    kind = result.get("kind")
    task = result.get("task") if isinstance(result.get("task"), dict) else (result if kind == "task" else None)
    su = result.get("statusUpdate") if isinstance(result.get("statusUpdate"), dict) else (result if kind == "status-update" else None)
    au = result.get("artifactUpdate") if isinstance(result.get("artifactUpdate"), dict) else (result if kind == "artifact-update" else None)
    return task, su, au


def frame_context_id(frame: dict) -> str | None:
    for obj in _unwrap(frame):
        if obj and obj.get("contextId"):
            return str(obj["contextId"])
    return None


def decode_frame(frame: dict) -> list[Event]:
    """One SSE frame → the events the shim acts on. Raises :class:`A2AError` on a
    JSON-RPC error frame."""
    err = frame.get("error")
    if isinstance(err, dict):
        raise A2AError(str(err.get("message") or err))
    events: list[Event] = []
    task, su, au = _unwrap(frame)
    if task:
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        state = norm_state(status.get("state"))
        if state:
            events.append(StateEvent(state=state, task_id=str(task.get("id") or "")))
    if su:
        status = su.get("status") if isinstance(su.get("status"), dict) else {}
        message = status.get("message") if isinstance(status.get("message"), dict) else {}
        parts = message.get("parts")
        tool = _tool_event(message.get("metadata"))
        if tool:
            events.append(tool)
        reasoning = _data_by_mime(parts, REASONING_MIME)
        if isinstance(reasoning, dict) and reasoning.get("text"):
            events.append(ReasoningEvent(str(reasoning["text"])))
        state = norm_state(status.get("state"))
        hitl = _data_by_mime(parts, HITL_MIME)
        events.append(
            StateEvent(
                state=state,
                task_id=str(su.get("taskId") or ""),
                # Plain status text (a legacy producer's progress line, a failure reason,
                # a question with no hitl part). Tool/reasoning frames carry none.
                text=_text_from_parts(parts),
                hitl=hitl if isinstance(hitl, dict) else None,
                final=bool(su.get("final")),
            )
        )
    if au:
        art = au.get("artifact") if isinstance(au.get("artifact"), dict) else {}
        text = _text_from_parts(art.get("parts"))
        if text:
            events.append(TextEvent(text=text, append=au.get("append") is True))
        usage = _usage_event(art.get("metadata"))
        if usage:
            events.append(usage)
    return events


def task_snapshot(task: Any) -> tuple[StateEvent | None, str]:
    """A ``GetTask`` result → ``(state, answer text)``. The durable task holds the final
    status (a failure's reason rides ``status.message``) and the answer artifact, so it is
    the ground truth when a live stream closed without delivering its terminal frame."""
    if not isinstance(task, dict):
        return None, ""
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    parts = message.get("parts")
    hitl = _data_by_mime(parts, HITL_MIME)
    state = StateEvent(
        state=norm_state(status.get("state")),
        task_id=str(task.get("id") or ""),
        text=_text_from_parts(parts),
        hitl=hitl if isinstance(hitl, dict) else None,
    )
    arts = [a for a in task.get("artifacts") or [] if isinstance(a, dict)]
    return state, "".join(_text_from_parts(a.get("parts")) for a in arts)


# ── transport ─────────────────────────────────────────────────────────────────


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    buf: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if buf:
                payload, buf = "\n".join(buf), []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("dropping unparseable SSE event: %.200s", payload)
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
    if buf:
        try:
            yield json.loads("\n".join(buf))
        except json.JSONDecodeError:
            return


class A2AClient:
    """``url`` is the instance (``http://127.0.0.1:7870``) or a hub; ``slug`` routes
    through the hub's member proxy (``/agents/<slug>/…``) exactly as the deck does."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        slug: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        trace: Any = None,
    ) -> None:
        self.base_url = url.rstrip("/")
        self.prefix = f"/agents/{slug}" if slug else ""
        self.token = token or None
        self._trace = trace  # optional file-like: every raw frame, one JSON per line
        self._client = httpx.AsyncClient(base_url=self.base_url, transport=transport, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "A2A-Version": A2A_VERSION}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @staticmethod
    def _raise_for(r: httpx.Response) -> None:
        if r.status_code in (401, 403):
            raise A2AUnauthorized(f"{r.request.url} rejected the credential ({r.status_code})")
        if r.status_code >= 400:
            raise A2AError(f"{r.request.url} answered {r.status_code}: {(r.text or '')[:300]}")

    async def rpc(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        try:
            r = await self._client.post(f"{self.prefix}/a2a", headers=self._headers(), json=payload, timeout=_RPC_TIMEOUT)
        except httpx.TransportError as exc:
            raise A2AError(f"{self.base_url} unreachable ({type(exc).__name__})") from exc
        self._raise_for(r)
        return r.json()

    async def probe(self) -> None:
        """Cheap auth/liveness check: ``GetTask`` on an id that cannot exist. A JSON-RPC
        "task not found" is success; 401 raises :class:`A2AUnauthorized`."""
        await self.rpc("GetTask", {"id": f"protoagent-acp-probe-{uuid.uuid4().hex[:8]}"})

    async def get_task(self, task_id: str) -> dict | None:
        """``GetTask`` → the task dict (1.0 puts it flat on ``result``; some responses nest
        it under ``result.task``), or ``None`` when it can't be read."""
        try:
            body = await self.rpc("GetTask", {"id": task_id})
        except A2AError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            return None
        task = result.get("task")
        return task if isinstance(task, dict) else result

    async def cancel(self, task_id: str) -> None:
        await self.rpc("CancelTask", {"id": task_id})

    async def get_json(self, path: str) -> Any:
        """GET an operator ``/api`` path; ``None`` on any failure (callers degrade)."""
        try:
            r = await self._client.get(f"{self.prefix}{path}", headers=self._headers(), timeout=_RPC_TIMEOUT)
        except httpx.TransportError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def stream(
        self,
        text: str,
        *,
        context_id: str,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> AsyncIterator[dict]:
        """``SendStreamingMessage``: yield every frame (raw JSON-RPC envelope) until the
        server closes the stream."""
        message: dict[str, Any] = {
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "messageId": str(uuid.uuid4()),
            "contextId": context_id,
        }
        if task_id:
            message["taskId"] = task_id
        if metadata:
            message["metadata"] = dict(metadata)
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }
        try:
            async with self._client.stream(
                "POST", f"{self.prefix}/a2a", headers=self._headers(), json=payload, timeout=_STREAM_TIMEOUT
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise_for(r)
                if "text/event-stream" not in (r.headers.get("content-type") or ""):
                    # Answered in one piece (or refused with a JSON-RPC error body).
                    await r.aread()
                    body = r.json()
                    self._record(body)
                    yield body
                    return
                async for frame in _parse_sse(r.aiter_lines()):
                    self._record(frame)
                    yield frame
        except httpx.TransportError as exc:
            raise A2AError(f"{self.base_url} stream broke ({type(exc).__name__})") from exc

    def _record(self, frame: Any) -> None:
        if self._trace is not None:
            try:
                self._trace.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self._trace.flush()
            except (OSError, ValueError):
                pass
