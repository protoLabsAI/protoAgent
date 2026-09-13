"""A2A client + frame reducer for the fleet deck (#3469, epic #3466) — talk to a member and
watch its turn: text, thinking, every tool call, a parked question, the cost.

Two halves, both synchronous and Textual-free (the app runs them in thread workers):

- :class:`A2AClient` — ``SendStreamingMessage`` / ``SubscribeToTask`` / ``GetTask`` /
  ``CancelTask`` over the hub's slug proxy (``/agents/<slug>/a2a``; the host at ``/a2a``),
  speaking the **A2A 1.0** wire ``a2a-sdk`` serves (proto method names, the mandatory
  ``A2A-Version: 1.0`` header — without it the SDK falls back to 0.3 and rejects these
  methods — ``role: "ROLE_USER"``, untyped ``parts: [{"text": …}]``). Frames are yielded
  as parsed JSON dicts from the SSE ``data:`` lines. Seeded from ``evals/client.py``.
- :class:`Turn` + :func:`apply_frame` — ONE reducer for live streams, resubscribe, and
  durable replay, mirroring the console byte-for-byte in semantics
  (``apps/web/src/lib/api.ts::makeA2ADispatcher`` / ``replayTaskSnapshot``,
  ``apps/web/src/chat/turnReducers.ts``, ``apps/web/src/chat/parts.ts``). The wire facts it
  honours are the ones that bit the console (each has a regression test):

  * tool calls ride the status MESSAGE's ``metadata[tool-call-v1 URI]`` — NOT a DataPart
    (protolabs-a2a 0.3.0); ``phase`` started/completed/failed; ``parentToolCallId`` nests a
    subagent's own call under its ``task`` card; ``outputChars`` is the true result size;
  * ``append`` on an artifact-update has NO wire presence when false (proto3), so text is
    appended ONLY on an explicit ``true`` — the terminal frame is a full REPLACE (#1717) and
    reading absence as append doubled every answer;
  * a replace that the parts already render is a no-op (keeps text↔tool interleaving);
    a real divergence rebuilds — the answer renders exactly once (#1709/#1938);
  * reasoning-v1 / hitl-v1 / component-v1 / context-v1 / room-v1 / steer-consumed-v1 are
    MIME-discriminated DataParts in any of the three encodings the fleet emits;
  * cost-v1 rides the terminal artifact's ``metadata``;
  * a frame stamped with a DIFFERENT contextId is cross-talk and is dropped; a frame with
    no contextId is never treated as foreign;
  * a ``Task`` snapshot (the first resubscribe frame, a GetTask result, a durable turn)
    replays history tool/reasoning/component frames first, then the accumulated text.

Never imports ``server``, ``operator_api``, or ``graph`` (the deck's import contract).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from deck import hub as deckhub

A2A_VERSION = "1.0"

TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1"
COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1"
HITL_MIME = "application/vnd.protolabs.hitl-v1+json"
COMPONENT_MIME = "application/vnd.protolabs.component-v1+json"
REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json"
CONTEXT_MIME = "application/vnd.protolabs.context-v1+json"
ROOM_MIME = "application/vnd.protolabs.room-v1+json"
STEER_CONSUMED_MIME = "application/vnd.protolabs.steer-consumed-v1+json"

# Kept in sync with the console (chat/reattach.ts TERMINAL, streamWatchdog.ts): paused
# states (input-required / auth-required) are deliberately NOT terminal — the turn resumes
# with the operator's answer.
_TERMINAL_RE = re.compile(r"completed|failed|canceled|cancelled|rejected", re.I)
# The console's sentinel for dismissing a parked question without answering it
# (apps/web/src/chat/ChatSurface.tsx::dismissHitl): the task must not stay parked forever.
DISMISS_SENTINEL = (
    "[dismissed] The operator dismissed this request without providing input. Continue "
    "without it — proceed using your best judgment, or stop and explain what you need."
)

# A turn can run for minutes, but a HALF-OPEN connection (laptop sleep, a member host that
# vanished, a proxy holding the socket) must not block the reader thread until the TCP
# RTO: the read budget is the stall window plus a margin, and a ReadTimeout is the
# caller's cue to consult GetTask (the console's watchdog does the same after 45 s).
STREAM_READ_S = 60.0
_STREAM_TIMEOUT = httpx.Timeout(STREAM_READ_S, connect=5.0)
_RPC_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def norm_state(state: str | None) -> str:
    """``TASK_STATE_INPUT_REQUIRED`` / ``input_required`` / ``input-required`` → ``input-required``."""
    s = str(state or "")
    if s.startswith("TASK_STATE_"):
        s = s[len("TASK_STATE_") :]
    return s.lower().replace("_", "-")


def is_terminal(state: str | None) -> bool:
    return bool(_TERMINAL_RE.search(norm_state(state)))


def is_paused(state: str | None) -> bool:
    return norm_state(state) in ("input-required", "auth-required")


def new_session_id() -> str:
    """The console's chat session id shape (``chat-<ms>-<rand>``): the server's session list
    filters ``chat-%``, so a deck conversation shows up in the console and vice versa."""
    return f"chat-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


# ── parts (apps/web/src/chat/parts.ts) ────────────────────────────────────────


@dataclass
class TextPart:
    text: str
    kind: str = "text"


@dataclass
class ReasoningPart:
    text: str
    kind: str = "reasoning"


@dataclass
class ToolsPart:
    ids: list[str] = field(default_factory=list)
    kind: str = "tools"


@dataclass
class ComponentPart:
    spec: dict
    kind: str = "component"


Part = TextPart | ReasoningPart | ToolsPart | ComponentPart


def append_text(parts: list[Part], text: str, append: bool) -> list[Part]:
    """Extend the open text run, or start a new one after a tool/reasoning block (a new
    run drops its leading whitespace; pure whitespace is skipped so a stray ``\\n`` between
    two tool calls neither renders an empty block nor splits the tool group)."""
    nxt = list(parts)
    if nxt and isinstance(nxt[-1], TextPart):
        nxt[-1] = TextPart(nxt[-1].text + text if append else text)
        return nxt
    trimmed = text.lstrip()
    if not trimmed:
        return nxt
    nxt.append(TextPart(trimmed))
    return nxt


def text_runs(parts: list[Part]) -> list[str]:
    return [p.text for p in parts if isinstance(p, TextPart)]


def rendered_prefix_end(canonical: str, runs: list[str]) -> int:
    """How far ``runs`` reproduce ``canonical``: the index just past the last run, or -1."""
    at = 0
    for run in runs:
        body = run.strip()
        if not body:
            continue
        while at < len(canonical) and canonical[at].isspace():
            at += 1
        if not canonical.startswith(body, at):
            return -1
        at += len(body)
    return at


def renders_text(runs: list[str], canonical: str) -> bool:
    end = rendered_prefix_end(canonical, runs)
    return end >= 0 and not canonical[end:].strip()


def replace_text(parts: list[Part], text: str) -> list[Part]:
    """The full-turn REPLACE (the terminal frame). Keep the parts when they already render
    ``text`` (preserving interleaving); on a real divergence drop every text run and land
    the canonical text once at the end."""
    nxt = list(parts)
    if renders_text(text_runs(nxt), text):
        return nxt
    kept = [p for p in nxt if not isinstance(p, TextPart)]
    trimmed = text.lstrip()
    if not trimmed:
        return kept
    return [*kept, TextPart(trimmed)]


def append_reasoning(parts: list[Part], text: str) -> list[Part]:
    nxt = list(parts)
    if nxt and isinstance(nxt[-1], ReasoningPart):
        nxt[-1] = ReasoningPart(nxt[-1].text + text)
        return nxt
    trimmed = text.lstrip()
    if not trimmed:
        return nxt
    nxt.append(ReasoningPart(trimmed))
    return nxt


def add_tool_ref(parts: list[Part], tool_id: str) -> list[Part]:
    nxt = list(parts)
    if nxt and isinstance(nxt[-1], ToolsPart):
        if tool_id not in nxt[-1].ids:
            nxt[-1] = ToolsPart([*nxt[-1].ids, tool_id])
        return nxt
    nxt.append(ToolsPart([tool_id]))
    return nxt


def add_component(parts: list[Part], spec: dict) -> list[Part]:
    return [*parts, ComponentPart(spec)]


# ── the turn ──────────────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any = None
    output: Any = None
    status: str = "running"  # running | done | error
    started_at: float | None = None
    duration_ms: int | None = None
    output_chars: int | None = None
    parent_id: str | None = None


@dataclass
class ToolEvent:
    id: str
    name: str
    phase: str  # "start" | "end"
    input: Any = None
    output: Any = None
    error: bool = False
    output_chars: int | None = None
    parent_id: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Turn:
    """Everything one server turn has told us so far — the single model the screen renders."""

    context_id: str
    task_id: str = ""
    state: str = ""  # normalized
    status_text: str = ""
    content: str = ""  # flat accumulation (the console's `content`)
    parts: list[Part] = field(default_factory=list)
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)
    room_replies: list[dict] = field(default_factory=list)
    consumed_steers: list[dict] = field(default_factory=list)
    hitl: dict | None = None  # a parked question / form / approval, until answered
    usage: Usage | None = None
    context: dict | None = None
    failed: str = ""
    done: bool = False
    last_frame_at: float = field(default_factory=time.monotonic)

    def tool(self, tool_id: str) -> ToolCall | None:
        return next((c for c in self.tool_calls if c.id == tool_id), None)

    def top_level_tools(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.parent_id is None]

    def children_of(self, tool_id: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.parent_id == tool_id]


class TurnError(RuntimeError):
    """A JSON-RPC error frame — the server refused the turn."""


class StreamStalled(deckhub.HubUnreachable):
    """No frame within :data:`STREAM_READ_S` — the caller should consult ``GetTask``
    rather than assume the turn died (the server may have finished and lost the tail)."""


# ── frame decoding (apps/web/src/lib/api.ts) ──────────────────────────────────


def _text_from_parts(parts: list | None) -> str:
    out = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if (kind is None or kind == "text") and p.get("text"):
            out.append(str(p["text"]))
    return "".join(out)


def _data_by_mime(parts: list | None, mime: str) -> Any:
    """A DataPart's payload iff its ``metadata.mimeType`` matches: 1.0 member-discriminated
    (``content.$case == "data"``, payload under ``content.value``), 1.0 flattened
    (top-level ``data``), or legacy 0.3 (``kind: "data"`` + ``data``)."""
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        meta = p.get("metadata") or {}
        if not isinstance(meta, dict) or meta.get("mimeType") != mime:
            continue
        content = p.get("content")
        if isinstance(content, dict) and content.get("$case") == "data":
            return content.get("value")
        if "data" in p:
            return p.get("data")
        return None
    return None


def _ext_by_uri(metadata: Any, uri: str) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    v = metadata.get(uri)
    return v if isinstance(v, dict) else None


def tool_event_from_meta(metadata: Any) -> ToolEvent | None:
    d = _ext_by_uri(metadata, TOOL_CALL_EXT_URI)
    if not d:
        return None
    phase = str(d.get("phase") or "")
    out_chars = d.get("outputChars")
    try:
        out_chars = int(out_chars) if out_chars is not None else None  # proto-JSON floats
    except (TypeError, ValueError):
        out_chars = None
    return ToolEvent(
        id=str(d.get("toolCallId") or ""),
        name=str(d.get("name") or ""),
        phase="start" if phase == "started" else "end",
        input=d.get("args"),
        output=d.get("result") if d.get("result") is not None else d.get("error"),
        error=phase == "failed" or bool(d.get("error")),
        output_chars=out_chars,
        parent_id=str(d["parentToolCallId"]) if d.get("parentToolCallId") else None,
    )


def usage_from_meta(metadata: Any) -> Usage | None:
    d = _ext_by_uri(metadata, COST_EXT_URI)
    if not d or not isinstance(d.get("usage"), dict):
        return None
    u = d["usage"]

    def n(k: str) -> int:
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    return Usage(
        input_tokens=n("input_tokens"),
        output_tokens=n("output_tokens"),
        cache_read_tokens=n("cache_read_input_tokens"),
        cache_creation_tokens=n("cache_creation_input_tokens"),
        cost_usd=float(d["costUsd"]) if isinstance(d.get("costUsd"), int | float) else None,
        duration_ms=int(d["durationMs"]) if isinstance(d.get("durationMs"), int | float) else None,
    )


def hitl_from_parts(parts: list | None) -> dict | None:
    d = _data_by_mime(parts, HITL_MIME)
    return d if isinstance(d, dict) else None


def reasoning_from_parts(parts: list | None) -> str | None:
    d = _data_by_mime(parts, REASONING_MIME)
    return str(d["text"]) if isinstance(d, dict) and d.get("text") else None


def component_from_parts(parts: list | None) -> dict | None:
    d = _data_by_mime(parts, COMPONENT_MIME)
    if isinstance(d, dict) and isinstance(d.get("component"), str):
        return {"component": d["component"], "props": d.get("props") if isinstance(d.get("props"), dict) else {}}
    return None


def context_from_parts(parts: list | None) -> dict | None:
    d = _data_by_mime(parts, CONTEXT_MIME)
    return d if isinstance(d, dict) and d.get("contextTokens") is not None else None


def room_reply_from_parts(parts: list | None) -> dict | None:
    d = _data_by_mime(parts, ROOM_MIME)
    if not isinstance(d, dict):
        return None
    if not (d.get("author") or d.get("addressed_to")):
        return None
    return d


def consumed_steers_from_parts(parts: list | None) -> list[dict] | None:
    d = _data_by_mime(parts, STEER_CONSUMED_MIME)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return None
    items = [
        {"id": str(i["id"]), "text": str(i["text"])}
        for i in d["items"]
        if isinstance(i, dict) and i.get("id") and i.get("text")
    ]
    return items or None


def _unwrap(frame: dict) -> tuple[dict | None, dict | None, dict | None]:
    """(task, status_update, artifact_update) from a 1.0 oneof or a 0.3 flat result."""
    result = frame.get("result")
    if not isinstance(result, dict):
        return None, None, None
    kind = result.get("kind")
    task = result.get("task") if isinstance(result.get("task"), dict) else (result if kind == "task" else None)
    su = result.get("statusUpdate") if isinstance(result.get("statusUpdate"), dict) else (result if kind == "status-update" else None)
    au = result.get("artifactUpdate") if isinstance(result.get("artifactUpdate"), dict) else (result if kind == "artifact-update" else None)
    return task, su, au


def frame_context_id(frame: dict) -> str | None:
    task, su, au = _unwrap(frame)
    for obj in (task, su, au, frame.get("result") if isinstance(frame.get("result"), dict) else None):
        if obj and obj.get("contextId"):
            return str(obj["contextId"])
    return None


def frame_is_foreign(frame: dict, expected_context_id: str) -> bool:
    cid = frame_context_id(frame)
    return bool(cid) and cid != expected_context_id


# ── the reducer ───────────────────────────────────────────────────────────────


def apply_tool_event(turn: Turn, evt: ToolEvent) -> None:
    """apps/web/src/chat/turnReducers.ts::applyToolEvent, verbatim in behaviour."""
    idx = next((i for i, c in enumerate(turn.tool_calls) if c.id == evt.id), None)
    now = time.monotonic()
    if evt.phase == "start":
        # Nest a subagent's own tool under its `task` card: the server tags the child
        # with the parent id (authoritative); fall back to "last open task wins".
        open_task = next((c for c in reversed(turn.tool_calls) if c.name == "task" and c.status == "running" and c.id != evt.id), None)
        parent = evt.parent_id if evt.parent_id is not None else (open_task.id if open_task else None)
        card = ToolCall(id=evt.id, name=evt.name, input=evt.input, status="running", started_at=now, parent_id=parent)
        if idx is not None:
            old = turn.tool_calls[idx]
            turn.tool_calls[idx] = ToolCall(**{**old.__dict__, **{k: v for k, v in card.__dict__.items()}})
        else:
            turn.tool_calls.append(card)
        if parent is None:
            turn.parts = add_tool_ref(turn.parts, evt.id)
        return
    status = "error" if evt.error else "done"
    if idx is not None:
        c = turn.tool_calls[idx]
        c.output, c.output_chars, c.status = evt.output, evt.output_chars, status
        if c.started_at is not None:
            c.duration_ms = int((now - c.started_at) * 1000)
    else:
        # Missed start — still render it as a fresh top-level call.
        turn.tool_calls.append(ToolCall(id=evt.id, name=evt.name, output=evt.output, output_chars=evt.output_chars, status=status))
        turn.parts = add_tool_ref(turn.parts, evt.id)


def _apply_text(turn: Turn, text: str, append: bool) -> None:
    turn.content = turn.content + text if append else text
    turn.parts = append_text(turn.parts, text, True) if append else replace_text(turn.parts, text)


def _apply_reasoning(turn: Turn, delta: str) -> None:
    turn.reasoning += delta
    turn.parts = append_reasoning(turn.parts, delta)


def _apply_status_message(turn: Turn, message: dict | None) -> None:
    """The parts + metadata of one status message (live frame OR a history entry)."""
    if not isinstance(message, dict):
        return
    parts = message.get("parts")
    evt = tool_event_from_meta(message.get("metadata"))
    if evt:
        apply_tool_event(turn, evt)
    r = reasoning_from_parts(parts)
    if r:
        _apply_reasoning(turn, r)
    comp = component_from_parts(parts)
    if comp:
        turn.parts = add_component(turn.parts, comp)
        turn.components.append(comp)
    room = room_reply_from_parts(parts)
    if room:
        turn.room_replies.append(room)
    steers = consumed_steers_from_parts(parts)
    if steers:
        turn.consumed_steers.extend(steers)


def replay_task_snapshot(turn: Turn, task: dict) -> None:
    """A Task snapshot (resubscribe's first frame, GetTask, a durable turn): history's
    tool / reasoning / component frames first, then the accumulated artifact text —
    which for a terminal task IS the final answer. A live stream's initial Task frame is
    bare (submitted; no history, no artifacts), so this is a no-op there."""
    if task.get("id"):
        turn.task_id = str(task["id"])
    history = [m for m in (task.get("history") or []) if isinstance(m, dict)]
    if history:
        # Rebuild the history-derived state in a scratch turn and REPLACE it, never append:
        # a resubscribe's snapshot repeats every status message the live stream already
        # delivered (reasoning, components, room replies, consumed steers would double).
        fresh = Turn(context_id=turn.context_id, task_id=turn.task_id)
        for msg in history:
            role = str(msg.get("role") or "")
            if "USER" in role.upper() and "AGENT" not in role.upper() or role == "user":
                continue
            _apply_status_message(fresh, msg)
        turn.reasoning = fresh.reasoning
        turn.components = fresh.components
        turn.room_replies = fresh.room_replies
        turn.consumed_steers = fresh.consumed_steers
        turn.parts = fresh.parts
        known = {c.id: c for c in turn.tool_calls}
        merged: list[ToolCall] = []
        for c in fresh.tool_calls:  # a card seen live keeps its timing; new ones join in order
            live = known.get(c.id)
            if live is not None:
                live.status, live.output, live.output_chars = c.status, c.output if c.output is not None else live.output, c.output_chars or live.output_chars
                live.parent_id = live.parent_id or c.parent_id
                merged.append(live)
            else:
                merged.append(c)
        merged_ids = {c.id for c in merged}
        turn.tool_calls = merged + [c for c in turn.tool_calls if c.id not in merged_ids]
    arts = [a for a in (task.get("artifacts") or []) if isinstance(a, dict)]
    accumulated = "".join(_text_from_parts(a.get("parts")) for a in arts)
    for a in arts:
        u = usage_from_meta(a.get("metadata"))
        if u:
            turn.usage = u
        ctx = context_from_parts(a.get("parts"))
        if ctx:
            turn.context = ctx
    if accumulated:
        _apply_text(turn, accumulated, False)
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    state = norm_state(status.get("state"))
    if state:
        turn.state = state
    if is_paused(state):
        parts = (status.get("message") or {}).get("parts") if isinstance(status.get("message"), dict) else None
        turn.hitl = hitl_from_parts(parts) or {"question": _text_from_parts(parts) or "Input required."}
    if is_terminal(state):
        turn.done = True


def apply_frame(turn: Turn, frame: dict) -> None:
    """Fold one SSE frame into the turn. Raises :class:`TurnError` on a JSON-RPC error."""
    err = frame.get("error")
    if isinstance(err, dict) and err.get("message"):
        raise TurnError(str(err["message"]))
    if frame_is_foreign(frame, turn.context_id):
        return
    turn.last_frame_at = time.monotonic()
    task, su, au = _unwrap(frame)
    if task and task.get("id"):
        replay_task_snapshot(turn, task)
    if su:
        if su.get("taskId"):
            turn.task_id = str(su["taskId"])
        status = su.get("status") if isinstance(su.get("status"), dict) else {}
        state = norm_state(status.get("state"))
        message = status.get("message") if isinstance(status.get("message"), dict) else None
        parts = message.get("parts") if message else None
        msg_text = _text_from_parts(parts)
        if not reasoning_from_parts(parts):
            # a reasoning-only frame carries no status text; don't clobber the status line
            turn.status_text = msg_text or state
        _apply_status_message(turn, message)
        if state:
            turn.state = state
        if is_paused(state):
            turn.hitl = hitl_from_parts(parts) or {"question": msg_text or "Input required."}
        if state == "failed":
            turn.failed = msg_text or "the turn failed"
        if su.get("final") or is_terminal(state):
            turn.done = True
    if au:
        if au.get("taskId"):
            turn.task_id = str(au["taskId"])
        art = au.get("artifact") if isinstance(au.get("artifact"), dict) else {}
        text = _text_from_parts(art.get("parts"))
        if text:
            _apply_text(turn, text, au.get("append") is True)
        u = usage_from_meta(art.get("metadata"))
        if u:
            turn.usage = u
        ctx = context_from_parts(art.get("parts"))
        if ctx:
            turn.context = ctx


def turn_from_durable(context_id: str, durable: dict) -> Turn:
    """A :class:`Turn` from one row of ``GET /api/chat/sessions/<id>/turns`` (ADR 0104):
    the same status / artifacts / history shapes the stream carries, so the snapshot
    replay applies unchanged — tool cards included."""
    turn = Turn(context_id=context_id, task_id=str(durable.get("task_id") or ""))
    replay_task_snapshot(
        turn,
        {
            "id": durable.get("task_id"),
            "status": durable.get("status") if isinstance(durable.get("status"), dict) else {"state": durable.get("state")},
            "artifacts": durable.get("artifacts") or [],
            "history": durable.get("history") or [],
        },
    )
    return turn


def user_text_from_durable(durable: dict) -> str:
    """The operator's message that started the turn: the first USER entry in history."""
    for msg in durable.get("history") or []:
        if isinstance(msg, dict) and "USER" in str(msg.get("role") or "").upper():
            t = _text_from_parts(msg.get("parts"))
            if t:
                return t
    return ""


# ── the client ────────────────────────────────────────────────────────────────


def _parse_sse(lines: Iterator[str]) -> Iterator[dict]:
    """``data:`` lines → parsed frames; comments (``: keepalive``) and blanks skipped; a
    multi-line ``data:`` event is joined per the SSE spec."""
    buf: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if buf:
                payload = "\n".join(buf)
                buf = []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
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
    """One member's A2A endpoint, reached through the hub proxy with the hub credential:
    ``<hub>/agents/<slug>/a2a`` (the reserved ``host`` slug is the hub itself, ``<hub>/a2a``).
    The hub URL and the slug are kept apart on purpose — URL normalization strips paths, and
    the member 401 scoping keys on the slug."""

    def __init__(
        self,
        hub_url: str,
        token: str | None = None,
        *,
        slug: str = "host",
        transport: httpx.BaseTransport | None = None,
        insecure_http: bool = False,
    ):
        self.base_url = deckhub.normalize_url(hub_url)
        self.slug = slug or "host"
        self.a2a_path = deckhub.HubClient.member_path(self.slug, "/a2a")
        self._token = token or None
        deckhub.credential_allowed(self.base_url, self._token, insecure_http=insecure_http)
        self._client = httpx.Client(base_url=self.base_url, transport=transport, follow_redirects=False)
        self._active: httpx.Response | None = None  # the open stream, for abort()

    @classmethod
    def for_member(cls, hub_client: deckhub.HubClient, slug: str, **kw: Any) -> A2AClient:
        """A member's A2A behind an already-opened hub connection: same URL and credential,
        but its OWN connection pool — closing this client must never tear down the pool the
        roster poll, the detail reads, and an in-flight CancelTask are using (a shared
        transport's close() drops every pooled connection, in-flight ones included)."""
        kw.pop("transport", None)  # never a caller's (shared) transport: this client's close() closes its pool
        return cls(hub_client.url, hub_client._token, slug=slug, **kw)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{self.a2a_path}"

    def abort(self) -> None:
        """Unblock a reader stuck in ``iter_lines`` from ANOTHER thread (the worker unwinds
        through ``HubUnreachable``). See :func:`deck.hub.wake_blocked_reader` for why the
        move differs per platform. Safe to call when nothing is open."""
        deckhub.wake_blocked_reader(self._active)

    def close(self) -> None:
        self.abort()
        self._client.close()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "A2A-Version": A2A_VERSION}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _raise_for(self, r: httpx.Response) -> None:
        if r.status_code in (401, 403):
            if self.slug != "host":
                raise deckhub.MemberUnauthorized(self.base_url, self.slug, f"member {self.slug!r} rejected the credential the hub attached")
            raise deckhub.HubUnauthorized(self.base_url, f"{self.base_url} rejected the credential ({r.status_code})")
        if r.status_code >= 400:
            raise deckhub.HubRequestError(self.base_url, r.status_code, (r.text or "").strip()[:300] or r.reason_phrase)

    def _message(self, text: str, *, context_id: str, task_id: str | None, metadata: dict | None) -> dict:
        mid = str(uuid.uuid4())
        message: dict[str, Any] = {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": mid, "contextId": context_id}
        if task_id:
            message["taskId"] = task_id  # resume a parked input-required task
        if metadata:
            message["metadata"] = dict(metadata)
        return message

    def stream(
        self,
        text: str,
        *,
        context_id: str,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> Iterator[dict]:
        """``SendStreamingMessage``: yield every SSE frame until the stream closes. The
        caller reduces with :func:`apply_frame` and stops on ``turn.done``."""
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendStreamingMessage",
            "params": {"message": self._message(text, context_id=context_id, task_id=task_id, metadata=metadata)},
        }
        yield from self._sse("POST", payload)

    def subscribe(self, task_id: str) -> Iterator[dict]:
        """``SubscribeToTask``: re-attach to an in-flight task; the first frame is a Task
        snapshot (history + text so far), then the live frames."""
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SubscribeToTask", "params": {"id": task_id}}
        yield from self._sse("POST", payload)

    def _sse(self, method: str, payload: dict) -> Iterator[dict]:
        try:
            with self._client.stream(method, self.a2a_path, headers=self._headers(), json=payload, timeout=_STREAM_TIMEOUT) as r:
                self._active = r
                try:
                    if r.status_code >= 400:
                        r.read()
                        self._raise_for(r)
                    yield from _parse_sse(r.iter_lines())
                finally:
                    self._active = None
        except httpx.ReadTimeout as exc:
            raise StreamStalled(self.base_url, f"no frame from {self.endpoint} for {STREAM_READ_S:g}s") from exc
        except (httpx.TransportError, httpx.StreamError) as exc:
            raise deckhub.HubUnreachable(self.base_url, f"{self.base_url} stream broke ({type(exc).__name__})") from exc

    def _rpc(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        try:
            r = self._client.post(self.a2a_path, headers=self._headers(), json=payload, timeout=_RPC_TIMEOUT)
        except httpx.TransportError as exc:
            raise deckhub.HubUnreachable(self.base_url, f"{self.base_url} did not answer ({type(exc).__name__})") from exc
        self._raise_for(r)
        try:
            body = r.json()
        except ValueError as exc:
            raise deckhub.HubError(self.base_url, f"malformed {method} response") from exc
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            raise TurnError(str(body["error"].get("message") or body["error"]))
        return body if isinstance(body, dict) else {}

    def get_task(self, task_id: str) -> dict:
        """``GetTask`` — the durable task, FLAT on ``result`` (1.0)."""
        body = self._rpc("GetTask", {"id": task_id})
        result = body.get("result")
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            return result["task"]
        return result if isinstance(result, dict) else {}

    def cancel(self, task_id: str) -> dict:
        return self._rpc("CancelTask", {"id": task_id})


# ── the stall watchdog (apps/web/src/chat/streamWatchdog.ts) ──────────────────


def stalled_turn_is_terminal(turn: Turn, get_task, *, idle_s: float = 45.0, now: float | None = None) -> dict | None:
    """Consult the durable task after ``idle_s`` without a frame. Returns the task when it
    is TERMINAL (the stream tail was lost; finalize from the task), else None. Never
    fabricates a completion; a paused task keeps waiting."""
    now = time.monotonic() if now is None else now
    if turn.done or not turn.task_id or now - turn.last_frame_at < idle_s:
        return None
    try:
        task = get_task(turn.task_id)
    except Exception:  # noqa: BLE001 — unknown yet; re-arm
        return None
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    state = norm_state(status.get("state"))
    if not state or is_terminal(state):
        return task
    return None
