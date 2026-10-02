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
* ``text``          — the tail of what it has said so far;
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


def current_sink() -> Sink | None:
    return _SINK.get()


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
    ) -> None:
        self.target = target
        # The delegate's cwd — what a RELATIVE location in its tool calls is relative to.
        self.workdir = workdir
        self._sink = sink
        self._min_interval = min_interval
        self._clock = clock
        self.plan: list[dict] | None = None
        self.current_tool: dict | None = None
        self.recent_tools: deque[dict] = deque(maxlen=RECENT_TOOLS_MAX)
        self.tool_count = 0
        self.text = ""
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
        if not delta:
            return
        if self._paragraph and self.text and not self.text.endswith("\n"):
            delta = "\n\n" + delta
        self._paragraph = False
        self.text = (self.text + delta)[-TEXT_TAIL_MAX:]
        await self._changed()

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
        await self._changed(urgent=phase != "update")

    def _note_write(self, tid: str, event: dict, status: str, raw_paths: list[str]) -> None:
        """A settled call that WROTE files inside a registered project → ``fs.changed`` on
        the bus, so the console's code pane refreshes (and follows) without a manual
        Refresh (ADR 0112). Kind and name come from the call as the card knows it — the end
        frame itself is often bare. Never raises: the card and the delegation come first."""
        paths = raw_paths or self._open_paths.get(tid) or []
        self._open_paths.pop(tid, None)
        if status != "completed" or not paths:
            return
        row = next((r for r in [self.current_tool, *reversed(self.recent_tools)] if r and r.get("id") == tid), None)
        kind = str(event.get("kind") or (row or {}).get("kind") or "")
        name = str((row or {}).get("name") or event.get("name") or "")
        try:
            from graph.fs_changes import is_write_tool, notify_paths_changed

            if is_write_tool(kind, name):
                notify_paths_changed(paths, source="delegate", target=self.target, workdir=self.workdir)
        except Exception:  # noqa: BLE001 — a live view must never cost the delegation
            log.debug("[delegate-progress] fs change notify failed", exc_info=True)

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

    async def _emit(self) -> None:
        self._dirty = False
        self._last_emit = self._clock()
        try:
            await self._sink(self.snapshot())
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
        """The run is over: one final ``done`` snapshot, whatever the throttle says."""
        self.close()
        if self.done:
            return
        self.done, self.ok = True, ok
        if self.current_tool and self.current_tool.get("status") == "running":
            self.current_tool["status"] = "completed" if ok else "failed"
        await self._emit()

    def close(self) -> None:
        """Drop a pending trailing flush (synchronous — safe mid-cancellation)."""
        if self._trailing is not None and not self._trailing.done():
            self._trailing.cancel()
        self._trailing = None
