"""Live progress of a running delegation, for the console's delegation card (#3979).

Delegate-type agnostic. A delegate that reports while it works — an ACP coder
(claude-agent-acp, codex-acp, protoCLI…) over ``session/update``, or a streaming A2A
peer over its task's SSE status/artifact/tool-call frames (``plugins/delegates/
a2a_progress.py``) — is normalized into ONE small model the card
renders, whatever the transport:

* ``plan``          — the delegate's own todo list: ``[{content, status}]``, latest wins
  (``status`` pending / in_progress / completed);
* ``current_tool``  — what it is doing right now: ``{id, name, kind, status, locations}``
  (``kind`` is ACP's read/edit/execute/search/…; ``locations`` are ``{path, line}``);
* ``recent_tools``  — the last few of those, newest last, capped;
* ``text``          — the tail of its NARRATION: text it said before a later tool call
  (see "Narration, never the answer" below);
* ``tool_count`` / ``done`` / ``ok`` — how much, and whether it is over and how.

Before this a delegation's card showed a spinner and a clock for the whole run (the ACP
adapter awaited ``client.prompt()`` with no callbacks). The Project Board plugin's
per-card drawer was the one consumer, through its own ``_GenBuffer`` — the same bounded
shape kept here.

**The transport seam** is the tracker's three feeds, all normalized:

* ``on_plan(entries)``   — a whole plan, ``[{content, status, …}]``;
* ``on_tool(event)``     — ``{phase: start|update|end, id, name, kind?, status?, locations?}``
  (``update`` refines an open call's name/kind/locations in place; ``end`` settles it);
* ``on_text(delta)``     — streamed reply text.

**Narration, never the answer.** A delegate's reply text arrives on the same feed whether
it is narration between tool calls ("I'll look at calc.py first.") or the final answer —
no transport marks the difference while it streams. The answer is rendered by the chat the
moment the delegation returns, so a card that typed it out first showed it twice (the
answer streaming into the card, then appearing in the chat). So ``on_text`` only BUFFERS:
the buffer is committed to ``text`` when a later tool call starts — proof it was
intermediate — and is dropped when the run settles, because text with no tool call after
it is the answer. Neither a live snapshot nor the ``done`` one ever carries it.

An adapter maps its wire onto those and nothing else — ``acp_prompt_callbacks()`` is the
ACP mapping (``AcpClient`` already emits exactly these shapes); an A2A, an OpenAI
streaming tool-call or a plugin transport writes its own and the card needs no change. A
transport that reports nothing (a non-streaming peer, a plain completion endpoint) simply
never creates a tracker, and the card keeps today's spinner and final result.

**Who owns a card** binds the sink, joined to the adapter by a ContextVar so no dispatch
signature has to change: the ``@`` short-circuit in ``server.chat_dispatch`` (its mention
card), the foreground ``delegate_to`` tool (the delegation's ask row, via a LangChain
custom event), and a background job (``background.progress``). An adapter reads
``current_sink()`` in the DISPATCHING task and only builds a tracker when one is bound —
a transport's own reader task (a pooled ACP client's) has a context that predates the
turn, which is why the sink is captured up front rather than looked up per update.

**Writes feed the code pane.** A settled tool call that changed files (an ACP ``edit`` /
``delete`` / ``move``, or a write-named tool from a kindless transport) and whose locations
fall inside a registered project is also published as ``fs.changed`` (``graph.fs_changes``),
so the console's Diff tab and open file refresh — and follow mode moves — without a manual
Refresh (ADR 0112).

**The reply's own timeline.** The card is live-only, but the delegate's REPLY is what stays
in the chat, and a coder's reply text is its narration with every action cut out of it
("Now executing the edits:" … "The tests pass."). So the tracker also keeps the run as an
ordered ``timeline`` — text runs and tool calls, as they happened — and the ``done``
snapshot carries it. It is NOT a card field: the turn drivers lift it off the done snapshot
(``take_timeline``) and hand it, checked against the reply text (``reply_timeline``), to the
reply bubble, which then renders text ↔ tool cards in order like a native turn.

Bounded on purpose: list sizes and string lengths are capped, and emission is throttled
(at most one snapshot per ``min_interval`` seconds, with a trailing flush so the last
change always lands, and a final ``done`` snapshot). A snapshot is a whole state, never a
delta, so a consumer that misses one loses nothing but latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator

log = logging.getLogger("protoagent.delegate_progress")

Sink = Callable[[dict], Awaitable[None]]

#: Caps — a snapshot rides every progress frame, so it must stay small.
PLAN_MAX = 20
PLAN_CONTENT_MAX = 140
RECENT_TOOLS_MAX = 6
TOOL_NAME_MAX = 120
LOCATIONS_MAX = 3
TEXT_TAIL_MAX = 400
#: Paths per tool call, and open calls, remembered for the code pane's change signal.
WRITE_PATHS_MAX = 20
OPEN_WRITES_MAX = 64
#: ``fs.changed`` per tracker at most every this many seconds (≤ 2/s): a delegate's writes
#: inside the window are coalesced into the next one (trailing), never dropped.
WRITE_EVENT_INTERVAL_S = 0.5
#: Distinct written paths held between two ``fs.changed`` flushes.
PENDING_WRITES_MAX = 200
#: The reply timeline (``timeline``): entries, and per-call input/output text. Past the
#: entry cap the timeline is abandoned (``None``) rather than cut — a partial one would put
#: text and tools out of order, and the reply then renders as plain text, as it always did.
TIMELINE_MAX = 300
#: Per call, the same preview a native tool card gets (``_TOOL_PREVIEW_CHARS``) — and across
#: the whole run a budget, past which calls keep their name and status but no input/output:
#: the timeline is stored in task history and the console's transcript, once per reply.
TIMELINE_TOOL_IO_MAX = 800
TIMELINE_IO_BUDGET = 48_000
#: Reply text the timeline holds across the run; past it the timeline is abandoned like the
#: entry cap, so a delegate that streams without end can't grow the snapshot without bound.
TIMELINE_TEXT_BUDGET = 200_000
#: Seconds between snapshots. A coder can fire dozens of updates a second while it
#: streams text; the card needs a few frames a second at most.
MIN_INTERVAL_S = 0.75

_SINK: contextvars.ContextVar[Sink | None] = contextvars.ContextVar("delegate_progress_sink", default=None)


@contextlib.contextmanager
def progress_sink(sink: Sink | None) -> Iterator[None]:
    """Bind ``sink`` for every delegation dispatched inside this block (and the tasks it
    creates, which copy the context). ``None`` explicitly UNBINDS — a background job
    spawned from inside a foreground delegation must not report into its parent's card."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


async def _no_card(_snapshot: dict) -> None:
    """The sink for a delegation no card owns: its snapshots still reach the ledger's live
    record (``delegation.progress``), they just have no chat card to update."""


def is_card_sink(sink: Sink | None) -> bool:
    """True for a sink a CARD bound — false for none, and for the ledger-only no-op sink.
    An adapter gives a card-less run only what is free: a coder's existing stream feeds
    the ledger, but nothing a card would add (``fs.changed`` for the code pane, an extra
    A2A watch connection) is spent on a run nobody is watching in chat."""
    return sink is not None and sink is not _no_card


def current_sink() -> Sink | None:
    """The bound card sink — or, when none is bound but the dispatch is inside an open
    ledger edge, a no-op one, so every delegation reports its live work to the ledger
    (the org chart's drill-in), not only the ones a chat card is watching. An explicit
    ``progress_sink(None)`` unbinds the CARD only; the ledger edge is still the edge."""
    sink = _SINK.get()
    if sink is not None:
        return sink
    try:
        from graph.ledger import current_edge_id

        return _no_card if current_edge_id() else None
    except Exception:  # noqa: BLE001
        return None


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _raw_paths(raw: object) -> list[str]:
    """The UNCLIPPED paths of a tool event's ``locations`` — for mapping a write onto a
    registered project (``graph.fs_changes``), where a clipped path would name nothing."""
    if not isinstance(raw, list):
        return []
    return [
        loc["path"]
        for loc in raw[:WRITE_PATHS_MAX]
        if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]
    ]


def _locations(raw: object) -> list[dict]:
    out: list[dict] = []
    if isinstance(raw, list):
        for loc in raw[:LOCATIONS_MAX]:
            if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]:
                entry: dict = {"path": loc["path"][-300:]}
                if isinstance(loc.get("line"), int):
                    entry["line"] = loc["line"]
                out.append(entry)
    return out


class DelegateProgress:
    """One delegation's live state, emitted to a sink as throttled whole snapshots."""

    def __init__(
        self,
        target: str,
        sink: Sink,
        *,
        min_interval: float = MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        workdir: str | None = None,
        announce_writes: bool = True,
        write_interval: float = WRITE_EVENT_INTERVAL_S,
    ) -> None:
        self.target = target
        # The delegate's cwd — what a RELATIVE location in its tool calls is relative to.
        self.workdir = workdir
        # Whether this delegate's reported writes may become ``fs.changed`` at all. Only a
        # delegate that writes to THIS machine's filesystem (a local ACP subprocess, a
        # loopback A2A peer) — a remote peer's ``args.path`` names a file on ITS disk, and
        # trusting it would let any peer drive refetches + Follow jumps in every console.
        self.announce_writes = announce_writes
        self._write_interval = write_interval
        self._pending_writes: list[str] = []
        self._last_write_emit: float | None = None
        self._write_flush: asyncio.Task | None = None
        self._sink = sink
        self._min_interval = min_interval
        self._clock = clock
        self.plan: list[dict] | None = None
        self.current_tool: dict | None = None
        self.recent_tools: deque[dict] = deque(maxlen=RECENT_TOOLS_MAX)
        self.tool_count = 0
        self.text = ""
        # Reply text not yet proven to be narration (no tool call after it yet) — never
        # emitted; committed to ``text`` by the next tool start, dropped at the end.
        self._pending = ""
        # Work happened since the last text: the next narration starts a new paragraph,
        # as the delegate's own reply does (#3408), instead of "…the plan.Plan created."
        self._paragraph = False
        self.done = False
        self.ok = True
        self._last_emit: float | None = None
        self._dirty = False
        self._trailing: asyncio.Task | None = None
        # Raw (unclipped) paths an open call named, by call id: an ACP coder often sends a
        # call's locations on start/update and settles it with a bare status.
        self._open_paths: dict[str, list[str]] = {}
        # The run in order, for the reply bubble (see the module doc) — ``None`` once over cap.
        self._timeline: list[dict] | None = []
        self._timeline_tools: dict[str, dict] = {}
        self._timeline_io_left = TIMELINE_IO_BUDGET
        self._timeline_text_left = TIMELINE_TEXT_BUDGET
        # The ledger edge this delegation runs inside (graph/ledger.py) — captured NOW, in
        # the dispatching task: the feeds arrive on a transport's reader task.
        try:
            from graph.ledger import current_edge_id

            self._edge_id = current_edge_id()
        except Exception:  # noqa: BLE001
            self._edge_id = None

    # -- the transport seam: normalized feeds (may run on a transport's reader task) --

    def acp_prompt_callbacks(self) -> dict:
        """The ACP mapping: ``AcpClient.prompt`` keyword callbacks that feed this tracker
        (the client already emits the normalized tool/text/plan shapes)."""
        return {"tool_callback": self.on_tool, "text_callback": self.on_text, "plan_callback": self.on_plan}

    async def on_plan(self, entries: list) -> None:
        # Latest-wins: an ACP plan update carries the ENTIRE plan every time.
        self.plan = [
            {"content": _clip(e.get("content"), PLAN_CONTENT_MAX), "status": str(e.get("status") or "")[:16]}
            for e in (entries or [])[:PLAN_MAX]
            if isinstance(e, dict)
        ]
        self._paragraph = True
        await self._changed(urgent=True)

    async def on_text(self, delta: str) -> None:
        # Buffered, not shown: until a tool call follows, this may be the final answer.
        # Nothing visible changed, so nothing is emitted.
        if not delta or self.done:
            return
        self._timeline_text(delta)
        self._pending = (self._pending + delta)[-TEXT_TAIL_MAX:]

    def _commit_text(self) -> None:
        """A tool call started: the text before it was narration — show it."""
        delta, self._pending = self._pending, ""
        if not delta.strip():
            return
        if self._paragraph and self.text and not self.text.endswith("\n"):
            delta = "\n\n" + delta
        self._paragraph = False
        self.text = (self.text + delta)[-TEXT_TAIL_MAX:]

    async def on_tool(self, event: dict) -> None:
        phase = str(event.get("phase") or "")
        tid = str(event.get("id") or event.get("name") or "")
        name = _clip(event.get("name") or "tool", TOOL_NAME_MAX)
        kind = str(event.get("kind") or "")[:32]
        locs = _locations(event.get("locations"))
        raw_paths = _raw_paths(event.get("locations"))
        if raw_paths and phase in ("start", "update"):
            self._open_paths.pop(tid, None)
            self._open_paths[tid] = raw_paths
            while len(self._open_paths) > OPEN_WRITES_MAX:
                self._open_paths.pop(next(iter(self._open_paths)))
        cur = self.current_tool
        if phase == "start":
            self._commit_text()
            self._paragraph = True
            self.tool_count += 1
            self.current_tool = {"id": tid, "name": name, "kind": kind, "status": "running", "locations": locs}
            self.recent_tools.append(dict(self.current_tool))
        elif phase == "update":
            # A refinement of an open call (claude-agent-acp names a tool after opening it
            # with a placeholder, #3691) — rename it in place, never a new row.
            for row in [cur, *self.recent_tools]:
                if row and row.get("id") == tid:
                    row["name"] = name
                    if kind:
                        row["kind"] = kind
                    if locs:
                        row["locations"] = locs
        elif phase == "end":
            status = "failed" if str(event.get("status") or "") == "failed" else "completed"
            matched = False
            for row in [cur, *self.recent_tools]:
                if row and row.get("id") == tid:
                    row["status"] = status
                    matched = True
            if not matched:
                # Missed start (a call that arrived already terminal is started first by
                # the client, so this is rare) — still worth a row.
                self.tool_count += 1
                self.recent_tools.append({"id": tid, "name": name, "kind": kind, "status": status, "locations": locs})
            self._note_write(tid, event, status, raw_paths)
        else:
            return
        self._timeline_tool(phase, tid, name, kind, event)
        await self._changed(urgent=phase != "update")

    # -- the reply timeline ----------------------------------------------------

    def _timeline_add(self, entry: dict) -> bool:
        if self._timeline is None:
            return False
        if len(self._timeline) >= TIMELINE_MAX:
            log.debug("[delegate-progress] %r: timeline over %d entries — dropped", self.target, TIMELINE_MAX)
            self._timeline, self._timeline_tools = None, {}
            return False
        self._timeline.append(entry)
        return True

    def _timeline_text(self, delta: str) -> None:
        if self._timeline is None:
            return
        self._timeline_text_left -= len(delta)
        if self._timeline_text_left < 0:
            log.debug("[delegate-progress] %r: timeline text over %d chars — dropped", self.target, TIMELINE_TEXT_BUDGET)
            self._timeline, self._timeline_tools = None, {}
            return
        last = self._timeline[-1] if self._timeline else None
        if last is not None and last["type"] == "text":
            last["text"] += delta
        else:
            self._timeline_add({"type": "text", "text": delta})

    def _timeline_tool(self, phase: str, tid: str, name: str, kind: str, event: dict) -> None:
        if self._timeline is None:
            return
        row = self._timeline_tools.get(tid)
        # A coder titles a shell call with the command line itself — `curl -H "Authorization:
        # Bearer …"` — so the name is redacted like the input it repeats.
        name = _redacted(name)
        if phase == "start" or (phase == "end" and row is None):
            row = {"type": "tool", "id": tid, "name": name, "kind": kind, "status": "running", "input": "", "output": ""}
            if not self._timeline_add(row):
                return
            self._timeline_tools[tid] = row
        if row is None:
            return  # an update for a call this run never opened
        if phase == "update":
            row["name"] = name
            if kind:
                row["kind"] = kind
        if event.get("input") not in (None, ""):
            row["input"] = self._spend(row["input"], _timeline_io(event.get("input")))
        if phase == "end":
            row["status"] = "failed" if str(event.get("status") or "") == "failed" else "completed"
            row["output"] = self._spend(row["output"], _timeline_io(event.get("output")))

    def _spend(self, old: str, new: str) -> str:
        """``new`` if the run's input/output budget still covers it (``old`` is refunded —
        a refinement replaces the text it refines), else nothing."""
        left = self._timeline_io_left + len(old)
        if len(new) > left:
            self._timeline_io_left = left
            return ""
        self._timeline_io_left = left - len(new)
        return new

    def _note_write(self, tid: str, event: dict, status: str, raw_paths: list[str]) -> None:
        """A settled call that WROTE files inside a registered project → ``fs.changed`` on
        the bus, so the console's code pane refreshes (and follows) without a manual
        Refresh (ADR 0112). Kind and name come from the call as the card knows it — the end
        frame itself is often bare. Never raises: the card and the delegation come first."""
        paths = raw_paths or self._open_paths.get(tid) or []
        self._open_paths.pop(tid, None)
        if not self.announce_writes or status != "completed" or not paths:
            return
        row = next((r for r in [self.current_tool, *reversed(self.recent_tools)] if r and r.get("id") == tid), None)
        kind = str(event.get("kind") or (row or {}).get("kind") or "")
        name = str((row or {}).get("name") or event.get("name") or "")
        try:
            from graph.fs_changes import is_write_tool

            if not is_write_tool(kind, name):
                return
        except Exception:  # noqa: BLE001 — a live view must never cost the delegation
            return
        for p in paths:
            if p not in self._pending_writes and len(self._pending_writes) < PENDING_WRITES_MAX:
                self._pending_writes.append(p)
        # Throttled: at most one flush per write_interval, trailing — a burst of edits (or a
        # flood of frames) becomes one event, not one per call.
        now = self._clock()
        if self._last_write_emit is None or now - self._last_write_emit >= self._write_interval:
            self._flush_writes()
        elif self._write_flush is None or self._write_flush.done():
            wait = self._write_interval - (now - self._last_write_emit)
            self._write_flush = asyncio.get_running_loop().create_task(self._flush_writes_after(wait))

    def _flush_writes(self) -> None:
        """Publish the pending writes (one ``fs.changed`` per project they touch)."""
        if not self._pending_writes:
            return
        paths, self._pending_writes = self._pending_writes, []
        self._last_write_emit = self._clock()
        try:
            from graph.fs_changes import notify_paths_changed

            notify_paths_changed(paths, source="delegate", target=self.target, workdir=self.workdir)
        except Exception:  # noqa: BLE001 — a live view must never cost the delegation
            log.debug("[delegate-progress] fs change notify failed", exc_info=True)

    async def _flush_writes_after(self, wait: float) -> None:
        await asyncio.sleep(max(0.0, wait))
        self._flush_writes()

    # -- emission --------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "target": self.target,
            "plan": [dict(e) for e in self.plan] if self.plan else None,
            "current_tool": dict(self.current_tool) if self.current_tool else None,
            "recent_tools": [dict(r) for r in self.recent_tools],
            "tool_count": self.tool_count,
            "text": self.text,
            "done": self.done,
            "ok": self.ok,
        }

    async def _emit(self, *, final: bool = False) -> None:
        self._dirty = False
        self._last_emit = self._clock()
        snapshot = self.snapshot()
        if self._edge_id:
            from graph.ledger import note_progress

            try:
                note_progress(self._edge_id, snapshot)
            except Exception:  # noqa: BLE001 — a live view must never cost the delegation
                log.warning("[delegate-progress] ledger note for %r raised", self.target, exc_info=True)
        if final and self._timeline:
            # Only the done snapshot, and only for the turn driver — see ``take_timeline``.
            snapshot["timeline"] = [dict(e) for e in self._timeline]
        try:
            await self._sink(snapshot)
        except Exception:  # noqa: BLE001 — a live view must never cost the delegation
            log.warning("[delegate-progress] sink for %r raised", self.target, exc_info=True)

    async def _changed(self, *, urgent: bool = False) -> None:
        """Note a change; emit now if the throttle allows, else leave it to the trailing
        flush. ``urgent`` (a plan or a tool boundary) is still throttled — a coder firing
        tool calls back to back would otherwise emit one frame per call."""
        if self.done:
            return
        self._dirty = True
        now = self._clock()
        if self._last_emit is None or now - self._last_emit >= self._min_interval:
            await self._emit()
            return
        if self._trailing is None or self._trailing.done():
            wait = self._min_interval - (now - self._last_emit)
            self._trailing = asyncio.get_running_loop().create_task(self._flush_after(wait))

    async def _flush_after(self, wait: float) -> None:
        await asyncio.sleep(max(0.0, wait))
        if self._dirty and not self.done:
            await self._emit()

    async def finish(self, *, ok: bool = True) -> None:
        """The run is over: one final ``done`` snapshot, whatever the throttle says — and
        the last coalesced writes, whatever the write throttle says."""
        self._flush_writes()
        self.close()
        if self.done:
            return
        self._pending = ""  # no tool call followed it: that text is the answer
        self.done, self.ok = True, ok
        if self.current_tool and self.current_tool.get("status") == "running":
            self.current_tool["status"] = "completed" if ok else "failed"
        for row in self._timeline_tools.values():
            if row["status"] == "running":  # never settled: the run ended under it
                row["status"] = "completed" if ok else "failed"
        await self._emit(final=True)

    def close(self) -> None:
        """Drop a pending trailing flush (synchronous — safe mid-cancellation)."""
        if self._trailing is not None and not self._trailing.done():
            self._trailing.cancel()
        self._trailing = None
        if self._write_flush is not None and not self._write_flush.done():
            self._write_flush.cancel()
        self._write_flush = None


def _redacted(text: str) -> str:
    """``text`` with credentials scrubbed — or "" if redaction itself fails (fail closed:
    no text rather than unredacted text in the chat transcript)."""
    try:
        from graph.middleware.redaction import redact

        return str(redact(text))
    except Exception:  # noqa: BLE001
        return ""


def _timeline_io(value: object) -> str:
    """A tool call's input/output as card text: redacted (a coder running ``env`` or
    ``cat .env`` must not land the values in the chat transcript), then capped."""
    text = _redacted(value if isinstance(value, str) else str(value or ""))
    return text if len(text) <= TIMELINE_TOOL_IO_MAX else text[: TIMELINE_TOOL_IO_MAX - 1] + "…"


def take_timeline(snapshot: dict) -> tuple[dict, list[dict] | None]:
    """Split a sink snapshot into ``(card snapshot, timeline)``. The timeline rides only
    the ``done`` snapshot and belongs to the REPLY, so a turn driver lifts it off before
    the snapshot goes to the card (it would only bloat every frame that carries it)."""
    if not isinstance(snapshot, dict) or "timeline" not in snapshot:
        return snapshot, None
    card = dict(snapshot)
    timeline = card.pop("timeline")
    return card, timeline if isinstance(timeline, list) else None


def _ink(text: str) -> str:
    """``text`` without whitespace — what a reply and its stream must agree on (the reply
    adds paragraph breaks between runs, a renderer strips edges; neither changes the ink)."""
    return "".join(text.split())


def reply_timeline(timeline: list[dict] | None, reply: str) -> list[dict] | None:
    """The timeline to render ``reply`` with, or ``None`` to render the reply as plain text.

    The reply text is authoritative: the transport may clean it after streaming (the ACP
    client collapses a doubled reply, the adapter appends an incomplete-turn marker or a
    managed-git PR line). So the timeline's text, joined, must be the reply's opening —
    compared ignoring whitespace, because the reply puts paragraph breaks between runs
    that the stream never sent — and whatever the reply has beyond it becomes a final text
    run. Anything else (cleaned mid-text, no streamed text, or no tool calls at all) is
    ``None``: a timeline that disagrees with the reply must never render instead of it.
    """
    if not timeline or not reply or not any(isinstance(e, dict) and e.get("type") == "tool" for e in timeline):
        return None
    out: list[dict] = []
    streamed = ""
    for entry in timeline:
        if not isinstance(entry, dict):
            return None
        if entry.get("type") == "text":
            text = str(entry.get("text") or "")
            streamed += text
            if text.strip():
                out.append({"type": "text", "text": text.strip()})
        elif entry.get("type") == "tool":
            out.append(dict(entry))
    want, have = _ink(reply), _ink(streamed)
    # No streamed text at all proves nothing about WHICH reply this run wrote — every reply
    # starts with "" — so it is not trusted to sit under one.
    if not have or not want.startswith(have):
        return None
    extra = len(want) - len(have)
    if extra:
        out.append({"type": "text", "text": _tail_with_ink(reply, extra)})
    return out


def _tail_with_ink(text: str, count: int) -> str:
    """The suffix of ``text`` holding its last ``count`` non-whitespace characters."""
    seen = 0
    for i in range(len(text) - 1, -1, -1):
        if not text[i].isspace():
            seen += 1
            if seen == count:
                return text[i:].strip()
    return text.strip()
