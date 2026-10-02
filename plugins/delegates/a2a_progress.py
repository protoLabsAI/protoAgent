"""Live progress of an ``a2a`` delegation — the A2A transport for ``graph.delegate_progress``.

The ACP adapter feeds the delegation card from an ACP coder's ``session/update``s
(#3979). This is the same card fed from an A2A peer: when the peer's agent card advertises
``capabilities.streaming``, the adapter subscribes to the task it just handed over
(``SubscribeToTask`` — the A2A SSE stream of that task's ``TaskStatusUpdateEvent`` /
``TaskArtifactUpdateEvent`` frames) and translates each frame into the tracker's three
normalized feeds:

* a status message's **tool-call-v1** extension (protoAgent peers emit one per tool
  start/end, keyed by ``toolCallId``)          → ``on_tool`` start / end;
* a status message's text, while WORKING        → ``on_text`` (which shows it only once a
  later tool call proves it was narration — ``graph.delegate_progress``);
* a peer's own **delegate-progress-v1** DataPart (the peer is itself delegating to a
  coder) — its plan                             → ``on_plan``;
* a non-text artifact                           → one finished "produced artifact" activity;
* a terminal / input-required state             → ``settled`` (wakes the result poll).

Reasoning, cost-v1 and worldstate-delta frames are deliberately ignored: none of them is
"what is it doing now". So is an artifact's TEXT: that is the reply itself (a protoAgent
peer streams its whole reply — narration and answer alike — into one answer artifact), and
the chat renders it the moment the delegation returns. Fed to the card it typed the answer
out there first, then again in the chat.

**Why subscribe rather than send over ``SendStreamingMessage``.** The adapter's send →
``GetTask`` poll path owns everything a delegation's RESULT depends on — conversation
continuity, HITL parks and resumes, the no-progress and hard deadlines, late collection of
a turn it gave up on (#3360, #3362, #3700). The stream here is an OBSERVER of the same
task: it never decides the answer, so a peer whose stream breaks, stalls or never opens
costs the card its live view and nothing else — the poll still lands the reply. What the
stream does add to the poll is latency: a terminal frame sets ``settled``, and the poll
wakes for its final ``GetTask`` immediately instead of after its backoff (up to 5 s).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

log = logging.getLogger("protoagent.plugins.delegates.a2a_progress")

TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1"
DELEGATE_PROGRESS_MIME = "application/vnd.protolabs.delegate-progress-v1+json"
_TEXT_MAX = 2000  # one frame's text, before the tracker keeps only its own tail

_TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "completed",
    "failed",
    "canceled",
    "rejected",
}
_PARKED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED", "input-required", "auth-required"}


def peer_streams(card: dict | None) -> bool:
    """Whether an agent card advertises the A2A streaming capability."""
    caps = (card or {}).get("capabilities") if isinstance(card, dict) else None
    return isinstance(caps, dict) and caps.get("streaming") is True


def _locations_from_args(args: object) -> list[dict]:
    """A file a tool's arguments name (``path`` / ``file_path`` / ``file``), as a location."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return []
    if not isinstance(args, dict):
        return []
    for key in ("path", "file_path", "file", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return [{"path": value.strip()}]
    return []


def _parts_text(parts: object) -> str:
    out = []
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def _data_by_mime(parts: object, mime: str) -> dict | None:
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, dict) and (part.get("metadata") or {}).get("mimeType") == mime:
            data = part.get("data")
            return data if isinstance(data, dict) else None
    return None


class A2AProgressFeed:
    """Translate A2A stream frames (the ``result`` of each SSE event) into a tracker's feeds."""

    def __init__(self, tracker) -> None:
        self.tracker = tracker
        self.settled = asyncio.Event()
        self._tools_seen: set[str] = set()

    async def frame(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        task = result.get("task")
        if isinstance(task, dict):
            # The subscription opens with a snapshot of the task so far: replay its history
            # (the tool calls made before we subscribed) and note its state.
            for message in task.get("history") or []:
                if isinstance(message, dict) and "AGENT" in str(message.get("role") or "").upper():
                    await self._message(message, working=True)
            status = task.get("status") or {}
            # …and the frame it is on right now (the latest status message is not in history).
            if isinstance(status.get("message"), dict):
                await self._message(status["message"], working=str(status.get("state") or "").upper().endswith("WORKING"))
            self._state(status.get("state"))
        status_update = result.get("statusUpdate")
        if isinstance(status_update, dict):
            status = status_update.get("status") or {}
            state = status.get("state")
            message = status.get("message")
            if isinstance(message, dict):
                await self._message(message, working=str(state or "").upper().endswith("WORKING"))
            self._state(state)
        artifact_update = result.get("artifactUpdate")
        if isinstance(artifact_update, dict):
            await self._artifact(artifact_update)

    def _state(self, state: object) -> None:
        if str(state or "") in _TERMINAL or str(state or "") in _PARKED:
            self.settled.set()

    async def _message(self, message: dict, *, working: bool) -> None:
        meta = message.get("metadata") or {}
        call = meta.get(TOOL_CALL_EXT_URI) if isinstance(meta, dict) else None
        if isinstance(call, dict) and call.get("name"):
            tid = str(call.get("toolCallId") or call.get("name"))
            phase = str(call.get("phase") or "")
            if phase == "started":
                event = {"phase": "update" if tid in self._tools_seen else "start", "id": tid, "name": call["name"]}
                locs = _locations_from_args(call.get("args"))
                if locs:
                    event["locations"] = locs
                self._tools_seen.add(tid)
                await self.tracker.on_tool(event)
            elif phase in ("completed", "failed"):
                await self.tracker.on_tool(
                    {"phase": "end", "id": tid, "name": call["name"], "status": "failed" if phase == "failed" else "completed"}
                )
        parts = message.get("parts")
        nested = _data_by_mime(parts, DELEGATE_PROGRESS_MIME)
        if nested and isinstance(nested.get("plan"), list):
            await self.tracker.on_plan(nested["plan"])
        if working:
            text = _parts_text(parts)
            if text.strip():
                await self.tracker.on_text(text[:_TEXT_MAX])

    async def _artifact(self, update: dict) -> None:
        artifact = update.get("artifact") or {}
        parts = artifact.get("parts")
        if _parts_text(parts):
            # The reply streaming in — the chat's to render, never the card's.
            return
        name = str(artifact.get("name") or "").strip()
        if name and any(isinstance(p, dict) and ("data" in p or "file" in p or "url" in p) for p in parts or []):
            aid = f"artifact:{artifact.get('artifactId') or name}"
            await self.tracker.on_tool({"phase": "start", "id": aid, "name": f"produced {name}", "kind": "artifact"})
            await self.tracker.on_tool({"phase": "end", "id": aid, "name": f"produced {name}", "status": "completed"})


async def observe(client, url: str, headers: dict, task_id: str, feed: A2AProgressFeed) -> None:
    """Follow ``task_id`` over ``SubscribeToTask`` (SSE), feeding ``feed`` until the stream
    ends. Best-effort end to end: any failure just ends the live view — the caller's poll
    owns the result."""
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SubscribeToTask", "params": {"id": task_id}}
    try:
        async with client.stream(
            "POST", url, json=body, headers={**headers, "Accept": "text/event-stream"}
        ) as response:
            if response.status_code >= 400:
                log.debug("[a2a-progress] subscribe to %s refused: HTTP %s", task_id, response.status_code)
                return
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    envelope = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if not isinstance(envelope, dict) or envelope.get("error"):
                    return
                await feed.frame(envelope.get("result") or {})
                if feed.settled.is_set():
                    return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the live view is a courtesy; the poll lands the answer
        log.debug("[a2a-progress] following task %s ended", task_id, exc_info=True)


class LiveView:
    """One a2a dispatch's live view: a tracker (when a card owner bound a sink and the
    peer streams), its stream observer, and the ``settled`` signal the poll waits on."""

    def __init__(self, tracker) -> None:
        self.tracker = tracker
        self.feed = A2AProgressFeed(tracker)
        self._task: asyncio.Task | None = None
        self._client = None

    @property
    def settled(self) -> asyncio.Event:
        return self.feed.settled

    def start(self, url: str, headers: dict, task_id: str) -> None:
        """Open the subscription on its own client (the poll keeps the dispatch's)."""
        if self._task is not None or not task_id:
            return
        import httpx

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
        self._task = asyncio.get_running_loop().create_task(observe(self._client, url, headers, task_id, self.feed))

    async def wait_settled(self, timeout: float) -> None:
        """Sleep up to ``timeout``, waking early when the stream saw the task settle."""
        if timeout <= 0:
            return
        try:
            await asyncio.wait_for(self.settled.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    def _stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._client is not None:
            client, self._client = self._client, None
            # Closed off to the side: abort() runs mid-cancellation and must not await.
            asyncio.get_running_loop().create_task(client.aclose())

    async def close(self, *, ok: bool) -> None:
        """The dispatch returned or failed: stop following, land the final snapshot."""
        self._stop()
        await self.tracker.finish(ok=ok)

    def abort(self) -> None:
        """The dispatch was cancelled: stop following, no awaits."""
        self._stop()
        self.tracker.close()
