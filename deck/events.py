"""Member event streams for the deck (#3470): watch turns nobody in the deck started.

Every member serves its event bus at ``GET /api/events`` as SSE — through the hub at
``/agents/<slug>/api/events`` — framed as ``id: <seq>`` + ``data: {"topic", "data", "seq"}``
with ``: keepalive`` comments (``operator_api/routes.py::_sse_event_stream``, ADR 0039). A
server-to-server caller sends the same Bearer the roster uses; the ``?token=`` dance is for
browsers. On reconnect ``?since=<seq>`` (and ``Last-Event-ID``) replays what the ring still
holds, so a blip loses nothing that was retained.

What the deck listens for:

- ``chat.progress`` — a SERVER-FIRED turn's frames (scheduler, watch, inbox, webhook,
  background, a delegate's result): ``tool_start`` / ``tool_end`` / ``text`` /
  ``room_reply`` / ``steer_consumed``, plus a ``control`` block naming the turn. Republished
  only for those origins (``server/a2a.py::_publish_chat_progress``) — a turn a console or
  the deck streams itself is never on the bus, or it would render twice; those are watched
  by re-attaching to the task instead (``deck.a2a.A2AClient.subscribe``).
- ``turn.started`` / ``turn.finished`` — the same server-fired turns' lifecycle
  (``{session_id, origin, trigger, ok?, task_id?}``).
- ``turn.usage`` — EVERY terminal turn's spend (``{task_id, context_id, state, model,
  input_tokens, output_tokens, cost_usd, duration_ms}``), whoever started it: the roster's
  "last active" and per-turn spend signal.
- ``turn.resumed`` / ``chat.resumed`` — a parked question answered; a server-fired turn
  settled with its text.

:class:`FleetEvents` runs one reader thread per online member and funnels every event into
one queue the UI drains — a merged, time-ordered feed across the fleet. Threads reconnect
with backoff and stop when told. Nothing here imports Textual, ``server``, ``graph``, or
``operator_api``.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from deck import hub as deckhub

TOPICS = ("chat.progress", "turn.started", "turn.finished", "turn.usage", "turn.resumed", "chat.resumed")
_READ_S = 60.0  # keepalives arrive every 15 s; a minute of silence means the socket is dead
_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 20.0)


@dataclass(frozen=True)
class Event:
    """One bus event as the deck sees it."""

    slug: str  # which member's bus
    topic: str
    data: dict
    seq: int | None
    received_at: float = field(default_factory=time.monotonic)


def parse_sse(lines: Iterator[str]) -> Iterator[tuple[int | None, dict]]:
    """``(seq, frame)`` per SSE event: ``id:`` gives the seq (the bus also puts it in the
    payload); ``data:`` may span lines; comments and blanks are skipped."""
    buf: list[str] = []
    seq: int | None = None
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if buf:
                payload = "\n".join(buf)
                buf = []
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    seq = None
                    continue
                if isinstance(frame, dict):
                    yield (seq if seq is not None else _int_or_none(frame.get("seq")), frame)
            seq = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            seq = _int_or_none(line[3:].strip())
        elif line.startswith("data:"):
            buf.append(line[5:].lstrip())


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


class MemberEvents:
    """One member's SSE stream, reconnecting, with ``since`` replay. Blocking; run it in a
    thread and iterate :meth:`events` until :meth:`stop`."""

    def __init__(
        self,
        hub_url: str,
        token: str | None,
        slug: str,
        *,
        transport: httpx.BaseTransport | None = None,
        insecure_http: bool = False,
    ):
        self.base_url = deckhub.normalize_url(hub_url)
        self.slug = slug
        self.path = deckhub.HubClient.member_path(slug, "/api/events")
        self._token = token or None
        deckhub.credential_allowed(self.base_url, self._token, insecure_http=insecure_http)
        self._client = httpx.Client(base_url=self.base_url, transport=transport, follow_redirects=False)
        self._active: httpx.Response | None = None
        self._stop = threading.Event()
        self.last_seq: int | None = None
        self.connected = False
        self.last_error = ""

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "text/event-stream"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self.last_seq is not None:
            h["Last-Event-ID"] = str(self.last_seq)
        return h

    def stop(self) -> None:
        """From another thread: end the stream now. A socket shutdown wakes a read blocked
        in ``recv``; the response itself is closed by the reader as it unwinds — closing it
        (the fd) here, right behind the shutdown, loses the wake-up on macOS and the reader
        sleeps out the read timeout instead (see ``deck.a2a.A2AClient.abort``)."""
        self._stop.set()
        resp = self._active
        if resp is not None:
            try:
                stream = resp.extensions.get("network_stream")
                sock = stream.get_extra_info("socket") if stream is not None else None
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
                else:
                    resp.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.stop()
        self._client.close()

    def _once(self) -> Iterator[Event]:
        params = {"since": self.last_seq} if self.last_seq is not None else None
        with self._client.stream("GET", self.path, headers=self._headers(), params=params, timeout=httpx.Timeout(_READ_S, connect=5.0)) as r:
            self._active = r
            try:
                if r.status_code in (401, 403):
                    raise deckhub.MemberUnauthorized(self.base_url, self.slug, f"member {self.slug!r} rejected the credential the hub attached")
                if r.status_code >= 400:
                    raise deckhub.HubRequestError(self.base_url, r.status_code, (r.text or "")[:200] if r.status_code != 200 else "")
                self.connected = True
                for seq, frame in parse_sse(r.iter_lines()):
                    if self._stop.is_set():
                        return
                    topic = str(frame.get("topic") or "")
                    data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
                    if seq is not None:
                        self.last_seq = seq
                    if topic:
                        yield Event(slug=self.slug, topic=topic, data=data, seq=seq)
            finally:
                self._active = None
                self.connected = False

    def events(self) -> Iterator[Event]:
        """Yield events until :meth:`stop`; reconnects with backoff on any transport error.
        A credential rejection is final — it will not fix itself by retrying."""
        attempt = 0
        while not self._stop.is_set():
            try:
                yield from self._once()
                if self._stop.is_set():
                    return
                attempt = 0  # a clean close (server restart): reconnect promptly
            except deckhub.MemberUnauthorized as exc:
                self.last_error = str(exc)
                return
            except (httpx.TransportError, httpx.StreamError, deckhub.HubError) as exc:
                if self._stop.is_set():
                    return
                self.last_error = str(exc)
            delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
            attempt += 1
            if self._stop.wait(delay):
                return


class FleetEvents:
    """One reader thread per member, one queue for the UI. ``watch(slugs)`` reconciles the
    set of members being followed (start the new, stop the gone)."""

    def __init__(self, hub_client: deckhub.HubClient, *, insecure_http: bool = False, transport: httpx.BaseTransport | None = None):
        self._hub = hub_client
        self._insecure = insecure_http
        self._transport = transport
        self.queue: queue.Queue[Event] = queue.Queue()
        self._readers: dict[str, tuple[MemberEvents, threading.Thread]] = {}
        self._lock = threading.Lock()

    def _spawn(self, slug: str) -> None:
        reader = MemberEvents(self._hub.url, self._hub._token, slug, transport=self._transport, insecure_http=self._insecure)

        def run() -> None:
            try:
                for ev in reader.events():
                    self.queue.put(ev)
            finally:
                reader.close()

        t = threading.Thread(target=run, name=f"deck-events-{slug}", daemon=True)
        self._readers[slug] = (reader, t)
        t.start()

    def watch(self, slugs: list[str]) -> None:
        with self._lock:
            want = set(slugs)
            for slug in list(self._readers):
                if slug not in want:
                    reader, _ = self._readers.pop(slug)
                    reader.stop()
            for slug in slugs:
                if slug not in self._readers:
                    self._spawn(slug)

    def status(self) -> dict[str, dict]:
        with self._lock:
            return {slug: {"connected": r.connected, "last_seq": r.last_seq, "error": r.last_error} for slug, (r, _) in self._readers.items()}

    def drain(self, limit: int = 500) -> list[Event]:
        out: list[Event] = []
        while len(out) < limit:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self) -> None:
        with self._lock:
            readers = list(self._readers.values())
            self._readers.clear()
        for reader, _ in readers:
            reader.stop()
        for reader, t in readers:
            t.join(2.0)


# ── the progress vocabulary (apps/web/src/app/serverTurnProgress.ts::parseProgress) ──


@dataclass
class Progress:
    session: str
    task_id: str
    kind: str  # text | tool | room | ask | steer
    text: str = ""
    tool_id: str = ""
    name: str = ""
    done: bool = False
    output: str = ""
    error: bool = False
    author: str = ""
    addressed_to: str = ""
    ok: bool = True
    items: list[dict] = field(default_factory=list)
    control: dict | None = None


def parse_progress(data: dict) -> Progress | None:
    """A ``chat.progress`` payload → a typed frame, or None when malformed. Mirrors the
    console's parser: a tool frame without an id can't be paired start→end and is dropped;
    a room frame with an ``addressed_to`` and no author is the lead's outgoing ask."""
    session = str(data.get("session_id") or "")
    if not session:
        return None
    task_id = str(data.get("task_id") or "")
    phase = str(data.get("phase") or "")
    control = data.get("control") if isinstance(data.get("control"), dict) else None
    if phase == "text":
        text = str(data.get("text") or "")
        return Progress(session, task_id, "text", text=text, control=control) if text else None
    if phase in ("tool_start", "tool_end"):
        tool_id = str(data.get("tool_call_id") or "")
        if not tool_id:
            return None
        return Progress(
            session,
            task_id,
            "tool",
            tool_id=tool_id,
            name=str(data.get("tool") or ""),
            done=phase == "tool_end",
            output=str(data.get("output") or ""),
            error=bool(data.get("error")),
            control=control,
        )
    if phase == "room_reply":
        mid = str(data.get("message_id") or "")
        author = str(data.get("author") or "")
        addressed = str(data.get("addressed_to") or "")
        if mid and not author and addressed:
            return Progress(session, task_id, "ask", tool_id=mid, addressed_to=addressed, text=str(data.get("text") or ""), control=control)
        if not mid or not author:
            return None
        return Progress(session, task_id, "room", tool_id=mid, author=author, text=str(data.get("text") or ""), ok=data.get("ok") is not False, control=control)
    if phase == "steer_consumed":
        items = [
            {"id": str(i["id"]), "text": str(i["text"])}
            for i in (data.get("items") if isinstance(data.get("items"), list) else [])
            if isinstance(i, dict) and i.get("id") and i.get("text")
        ]
        return Progress(session, task_id, "steer", items=items, control=control) if items else None
    return None
