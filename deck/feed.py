"""The fleet-wide work feed (#3470): every tool call across the fleet, as it happens.

The model (:class:`Activity`) folds the fan-in's bus events into two things the UI reads:

- a bounded, time-ordered list of :class:`Row` — one per tool start/end, turn start/finish,
  room reply, spend line, park, resume — across every watched member;
- per-member :class:`TurnState` — is a turn running, is one parked on a question (per
  session), when was the member last active, what did the last turn cost — which drives
  the roster's TURN column and the bell.

Three sources feed it, and they MERGE rather than overwrite. Server-fired turns
(scheduler, watch, inbox, webhook, background, a delegate's result) publish
``chat.progress`` + ``turn.started``/``turn.finished`` on the member's bus, and EVERY turn
publishes ``turn.usage`` at its end and ``turn.input_required`` when it parks. Turns a
console or the deck streams itself are not republished (they would render twice there),
so the deck's own conversations report into the same model (``note_live`` / ``note_tool`` /
``park``). And a low-frequency probe of each member's session inventory
(``diagnostics/sessions``) catches the parks the bus could not show us — a park from before
the deck connected, or lost in a reconnect gap: it only speaks for the sessions it saw.

Bookkeeping never rings the bell: a park is announced only if it is STILL parked when the
UI drains. Nothing on the bus is trusted to arrive: a running turn whose end was lost ages
out (:data:`TURN_TTL_S`), an open tool without its end likewise (:data:`RUNNING_TTL_S`).
Replayed events (a reconnect's ``?since=`` catch-up) carry no wall-clock time unless the
member stamps one, so they never pretend to be "now".

Pure Python; the screen lives at the bottom and only renders this model.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from deck import events as deckevents

MAX_ROWS = 500
RUNNING_TTL_S = 15 * 60  # a `tool_start` with no end in this long is assumed dead
TURN_TTL_S = 60 * 60  # a turn with no frame at all in this long is assumed over (its turn.finished was lost)


@dataclass
class Row:
    ts: float  # monotonic, arrival order
    slug: str
    member: str
    kind: str  # tool | turn | usage | room | resume | needs-you | fleet
    glyph: str  # ⟳ ✓ ✗ ⚑ $ ·
    label: str
    detail: str = ""
    session: str = ""
    task_id: str = ""
    tool_id: str = ""
    error: bool = False
    controllable: bool = False  # a server-fired turn the operator may interject into
    at: float | None = None  # wall clock (epoch) when known; None for a replayed event without a stamp


@dataclass
class Park:
    prompt: str
    task_id: str = ""
    source: str = "bus"  # bus | probe | deck
    since: float = field(default_factory=time.monotonic)


@dataclass
class TurnState:
    running: dict[str, float] = field(default_factory=dict)  # task id (or session id) → last frame heard, monotonic
    open_tools: dict[str, tuple[str, float, str]] = field(default_factory=dict)  # tool_id → (name, started, task_id)
    parked: dict[str, Park] = field(default_factory=dict)  # session → the turn waiting on the operator there
    unparked: dict[str, float] = field(default_factory=dict)  # session → when the bus/deck last cleared it (monotonic)
    settled_tasks: set[str] = field(default_factory=set)  # tasks the deck answered whose park the member's store may still show (a redeemed plugin form on an older member)
    server_turns: dict[str, dict] = field(default_factory=dict)  # session → {task_id, origin, trigger, controllable}: a live server-fired turn
    last_active: float | None = None  # epoch
    last_cost_usd: float | None = None
    last_model: str = ""

    @property
    def is_running(self) -> bool:
        now = time.monotonic()
        if any(now - heard < TURN_TTL_S for heard in self.running.values()):
            return True
        return any(now - started < RUNNING_TTL_S for _, started, _ in self.open_tools.values())


class Activity:
    def __init__(self, names: dict[str, str] | None = None):
        self.rows: deque[Row] = deque(maxlen=MAX_ROWS)
        self.state: dict[str, TurnState] = {}
        self.names: dict[str, str] = dict(names or {})  # slug → display name
        self._new_parks: list[tuple[str, str]] = []  # (slug, session) parked since the last ring_due()

    def _st(self, slug: str) -> TurnState:
        return self.state.setdefault(slug, TurnState())

    def _name(self, slug: str) -> str:
        return self.names.get(slug, slug)

    def _add(self, row: Row) -> None:
        self.rows.append(row)
        st = self._st(row.slug)
        if row.kind != "needs-you" and row.at is not None:
            st.last_active = max(st.last_active or 0.0, row.at)

    # ── bus events ──

    def apply(self, ev: deckevents.Event) -> None:
        slug, d, now = ev.slug, ev.data, ev.received_at
        at = ev.ts if ev.ts is not None else (None if ev.replayed else time.time())
        st = self._st(slug)
        name = self._name(slug)
        if ev.topic == "chat.progress":
            # the control block and the task id ride EVERY frame (the console lifts them
            # off before parsing too): the first frame of a server turn is `turn_started`,
            # which carries nothing else worth a row
            sid, tid = str(d.get("session_id") or ""), str(d.get("task_id") or "")
            ctl = d.get("control") if isinstance(d.get("control"), dict) else None
            if tid:
                st.running[tid] = now
            if ctl and tid and sid:
                st.server_turns[sid] = {"task_id": tid, "origin": str(ctl.get("origin") or ""), "trigger": str(ctl.get("trigger") or ""), "controllable": bool(ctl.get("operator_controllable"))}
            p = deckevents.parse_progress(d)
            if p is None:
                return
            controllable = bool((ctl or {}).get("operator_controllable"))
            if p.kind == "tool":
                if p.done:
                    started = st.open_tools.pop(p.tool_id, (p.name, None, ""))[1]
                    dur = f"  {now - started:.1f}s" if started else ""
                    self._add(Row(now, slug, name, "tool", "✗" if p.error else "✓", p.name or "tool", (_clip(p.output) + dur).strip(), p.session, p.task_id, p.tool_id, p.error, controllable, at))
                else:
                    st.open_tools[p.tool_id] = (p.name, now, p.task_id)
                    self._add(Row(now, slug, name, "tool", "⟳", p.name or "tool", "", p.session, p.task_id, p.tool_id, False, controllable, at))
            elif p.kind == "room":
                self._add(Row(now, slug, name, "room", "✓" if p.ok else "✗", f"@{p.author} replied", _clip(p.text), p.session, p.task_id, at=at))
            elif p.kind == "ask":
                self._add(Row(now, slug, name, "room", "⟳", f"asked @{p.addressed_to}", _clip(p.text), p.session, p.task_id, at=at))
            elif p.kind == "steer":
                self._add(Row(now, slug, name, "turn", "·", "steer folded in", _clip("; ".join(i["text"] for i in p.items)), p.session, p.task_id, at=at))
            # text frames are the transcript's business, not the feed's
        elif ev.topic == "turn.started":
            sid = str(d.get("session_id") or "")
            st.running[sid] = now  # the scheduler's own event: session-keyed, no task id yet
            self._add(Row(now, slug, name, "turn", "⟳", "turn started", f"{d.get('origin', '')} · {d.get('trigger', '')}".strip(" ·"), sid, at=at))
        elif ev.topic == "turn.finished":
            sid, tid = str(d.get("session_id") or ""), str(d.get("task_id") or "")
            st.running.pop(sid, None)
            st.running.pop(tid, None)
            st.server_turns.pop(sid, None)
            self.unpark(slug, sid)
            ok = d.get("ok")
            self._add(Row(now, slug, name, "turn", "✓" if ok is not False else "✗", "turn finished", f"{d.get('origin', '')}".strip(), sid, tid, error=ok is False, at=at))
        elif ev.topic == "turn.usage":
            tid, sid = str(d.get("task_id") or ""), str(d.get("context_id") or "")
            st.running.pop(tid, None)
            st.running.pop(sid, None)  # turn.started keyed the session; its turn.finished may have been lost
            for tool_id in [k for k, v in st.open_tools.items() if v[2] == tid]:
                st.open_tools.pop(tool_id, None)  # a terminal turn ends every tool IT had open
            for k in [k for k, v in st.server_turns.items() if v.get("task_id") == tid]:
                st.server_turns.pop(k, None)
            cost = _num(d.get("cost_usd"))
            st.last_cost_usd = cost
            st.last_model = str(d.get("model") or "")
            state = _state(d.get("state"))
            if sid and state != "input-required":
                self.unpark(slug, sid)
            self._add(Row(now, slug, name, "usage", "$", f"turn {state or 'done'}", f"${cost:,.4f} · {int(_num(d.get('input_tokens'))):,} in · {int(_num(d.get('output_tokens'))):,} out" + (f" · {st.last_model}" if st.last_model else ""), sid, tid, error=state == "failed", at=at))
        elif ev.topic == "turn.input_required":
            sid, tid = str(d.get("context_id") or ""), str(d.get("task_id") or "")
            st.running.pop(tid, None)
            st.running.pop(sid, None)
            st.server_turns.pop(sid, None)  # a parked server turn is no longer addressable
            self.park(slug, sid, str(d.get("prompt") or "input required"), task_id=tid, source="bus", at=at)
        elif ev.topic == "turn.resumed":
            sid, tid = str(d.get("context_id") or ""), str(d.get("task_id") or "")
            self.unpark(slug, sid)
            if tid:
                st.running[tid] = now  # answered: the turn is running again
            self._add(Row(now, slug, name, "resume", "⚑", "question answered", "", sid, tid, at=at))
        elif ev.topic == "chat.resumed":
            sid = str(d.get("session_id") or "")
            st.running.pop(sid, None)
            self._add(Row(now, slug, name, "turn", "✗" if d.get("error") else "✓", "settled", _clip(str(d.get("text") or d.get("error") or "")), sid, str(d.get("task_id") or ""), error=bool(d.get("error")), at=at))

    # ── the deck's own conversations ──

    def note_live(self, slug: str, task_id: str, running: bool) -> None:
        st = self._st(slug)
        if running and task_id:
            st.running[task_id] = time.monotonic()
        elif task_id:
            st.running.pop(task_id, None)
        if running:
            st.last_active = time.time()

    def note_tool(self, slug: str, session: str, task_id: str, tool_id: str, name: str, *, done: bool, output: str = "", error: bool = False) -> None:
        now = time.monotonic()
        st = self._st(slug)
        if done:
            started = st.open_tools.pop(tool_id, (name, None, task_id))[1]
            dur = f"  {now - started:.1f}s" if started else ""
            self._add(Row(now, slug, self._name(slug), "tool", "✗" if error else "✓", name, (_clip(output) + dur).strip(), session, task_id, tool_id, error, at=time.time()))
        else:
            st.open_tools[tool_id] = (name, now, task_id)
            self._add(Row(now, slug, self._name(slug), "tool", "⟳", name, "", session, task_id, tool_id, at=time.time()))

    # ── parks: one per (member, session), three writers that merge ──

    def park(self, slug: str, session: str, prompt: str, *, task_id: str = "", source: str = "deck", at: float | None = None, probed_at: float | None = None) -> None:
        """A turn waits on the operator in ``session``. A probe result older than the
        bus's or the deck's own clearing of that session is stale and ignored, and so is
        a probe naming a task the deck itself settled."""
        st = self._st(slug)
        # >= : a clearing stamped in the same clock tick as the probe's read (Windows'
        # monotonic clock ticks every ~16 ms) is the newer information, not the older
        if probed_at is not None and (st.unparked.get(session, -1.0) >= probed_at or (task_id and task_id in st.settled_tasks)):
            return
        was = st.parked.get(session)
        st.parked[session] = Park(prompt, task_id or (was.task_id if was else ""), source, was.since if was else time.monotonic())
        if was is None:
            self._new_parks.append((slug, session))
            self._add(Row(time.monotonic(), slug, self._name(slug), "needs-you", "⚑", "needs you", _clip(prompt), session, task_id, at=at if at is not None else time.time()))

    def unpark(self, slug: str, session: str, *, probed_at: float | None = None, settle_task: str = "") -> None:
        """The turn in ``session`` no longer waits. A probe result older than the park it
        would clear is stale and ignored. ``settle_task`` names a task whose park the
        member's store may keep reporting although it is over for good (a redeemed plugin
        form on a member that does not complete the task): probes naming it are ignored."""
        st = self._st(slug)
        if settle_task:
            st.settled_tasks.add(settle_task)
        park = st.parked.get(session)
        if park is not None and probed_at is not None and park.since >= probed_at:
            return  # parked in the same tick the probe read, or after: the probe did not see it
        if probed_at is None:
            st.unparked[session] = time.monotonic()  # stamp even when already clear: a probe that read before now must not re-park
        st.parked.pop(session, None)

    def probe(self, slug: str, seen: dict[str, tuple[str, str]], *, probed_at: float) -> None:
        """Fold one member's session-inventory probe: for every session it SAW, park
        (reason, latest task) or clear (""); sessions outside its window are left alone."""
        for session, (reason, task_id) in seen.items():
            if reason:
                self.park(slug, session, reason, task_id=task_id, source="probe", probed_at=probed_at)
            else:
                self.unpark(slug, session, probed_at=probed_at)

    def ring_due(self) -> list[tuple[str, str, str]]:
        """``(slug, session, prompt)`` for parks that appeared since the last call AND are
        still parked now — a park answered within the same drain never rings."""
        due = []
        for slug, session in self._new_parks:
            park = self.state.get(slug, TurnState()).parked.get(session)
            if park is not None and (slug, session) not in [(a, b) for a, b, _ in due]:
                due.append((slug, session, park.prompt))
        self._new_parks.clear()
        return due

    def server_turn(self, slug: str, session_id: str) -> dict | None:
        """The live server-fired turn in this session, if the bus has shown one."""
        st = self.state.get(slug)
        return dict(st.server_turns[session_id]) if st is not None and session_id in st.server_turns else None

    def note_fleet(self, slug: str, text: str) -> None:
        self._add(Row(time.monotonic(), slug, self._name(slug), "fleet", "·", "fleet", text, at=time.time()))

    # ── roster helpers ──

    def turn_cell(self, slug: str) -> str:
        st = self.state.get(slug)
        if st is None:
            return "idle"  # nothing heard yet — the roster only asks for members that are up
        if st.parked:
            return "⚑ needs you"
        if st.is_running:
            return "⟳ running"
        return "idle"

    def parked_sessions(self, slug: str) -> list[str]:
        st = self.state.get(slug)
        return list(st.parked) if st is not None else []

    def last_active_cell(self, slug: str, now: float | None = None) -> str:
        st = self.state.get(slug)
        if st is None or st.last_active is None:
            return ""
        now = time.time() if now is None else now
        s = max(0, int(now - st.last_active))
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        return f"{s // 3600}h ago"


def _clip(s: str, n: int = 72) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def _state(v) -> str:
    s = str(v or "")
    if s.startswith("TASK_STATE_"):
        s = s[len("TASK_STATE_") :]
    return s.lower().replace("_", "-")


# ── the screen ────────────────────────────────────────────────────────────────


class WorkFeedScreen(Screen):
    """Every tool call across the fleet, newest last; `enter` opens the member's
    conversation at that session; `f` filters by member or tool; `p` pauses."""

    BINDINGS = [
        Binding("escape", "back", "back", show=True),
        Binding("enter", "open", "open in member", show=True),
        Binding("f", "filter", "filter", show=True),
        Binding("p", "pause", "pause", show=True),
        Binding("q", "quit", "quit", show=False),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.paused = False
        self.filter_text = ""
        self._shown: list[Row] = []

    def compose(self) -> ComposeResult:
        yield Static("work · all members", id="feed-head")
        table: DataTable = DataTable(id="feed", cursor_type="row")
        table.add_columns("TIME", "MEMBER", "", "WHAT", "DETAIL")
        yield table
        yield Input(placeholder="filter by member or tool (enter applies, esc clears)", id="feed-filter")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#feed-filter", Input).display = False
        self.query_one("#feed", DataTable).focus()
        self.render_rows()
        self.set_interval(0.5, self.render_rows)

    def render_rows(self) -> None:
        if self.paused:
            return
        app = self.app
        act: Activity = app.activity  # type: ignore[attr-defined]
        feed = app.events  # type: ignore[attr-defined]
        st = feed.status() if feed is not None else {}
        connected = sum(1 for v in st.values() if v.get("connected"))
        attached = app.attached_count() if hasattr(app, "attached_count") else 0  # type: ignore[attr-defined]
        self.query_one("#feed-head", Static).update(
            f"work · all members · sse ● {connected} of {len(st)} members" + (f" · attached to {attached} turn{'s' if attached != 1 else ''}" if attached else "") + (f" · filter {self.filter_text!r}" if self.filter_text else "")
        )
        rows = [r for r in act.rows if self._matches(r)]
        table = self.query_one("#feed", DataTable)
        if [id(r) for r in rows] == [id(r) for r in self._shown]:
            return
        self._shown = rows
        at_end = table.cursor_row is None or table.cursor_row >= table.row_count - 1
        table.clear()
        for r in rows:
            wall = time.strftime("%H:%M:%S", time.localtime(r.at)) if r.at is not None else "  —  ·  "  # a replayed event without a stamp
            glyph = Text(r.glyph, style={"⟳": "yellow", "✓": "green", "✗": "red", "⚑": "bold yellow", "$": "cyan"}.get(r.glyph, "dim"))
            what = Text(r.label, style="red" if r.error else "")
            table.add_row(wall, r.member, glyph, what, r.detail, key=str(id(r)))
        if rows and at_end:
            table.move_cursor(row=len(rows) - 1)

    def _matches(self, r: Row) -> bool:
        if not self.filter_text:
            return True
        f = self.filter_text.lower()
        return f in r.member.lower() or f in r.label.lower() or f in r.slug.lower()

    def selected(self) -> Row | None:
        table = self.query_one("#feed", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._shown):
            return None
        return self._shown[table.cursor_row]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def action_open(self) -> None:
        r = self.selected()
        if r is None:
            return
        self.app.open_member(r.slug, r.session or None)  # type: ignore[attr-defined]

    def action_filter(self) -> None:
        inp = self.query_one("#feed-filter", Input)
        inp.display = True
        inp.value = self.filter_text
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.filter_text = event.value.strip()
        event.input.display = False
        self.query_one("#feed", DataTable).focus()
        self._shown = []
        self.render_rows()

    def action_pause(self) -> None:
        self.paused = not self.paused
        head = self.query_one("#feed-head", Static)
        if self.paused:
            head.update(str(head.content) + "  ·  ○ paused")

    def action_down(self) -> None:
        self.query_one("#feed", DataTable).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#feed", DataTable).action_cursor_up()

    def action_back(self) -> None:
        # esc clears an active (or half-typed) filter first; a second esc leaves
        inp = self.query_one("#feed-filter", Input)
        if inp.display or self.filter_text:
            inp.display = False
            inp.value = ""
            self.filter_text = ""
            self._shown = []
            self.query_one("#feed", DataTable).focus()
            self.render_rows()
            return
        self.app.pop_screen()


FEED_CSS = """
#feed-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
#feed { height: 1fr; }
#feed-filter { margin: 0 1; }
"""
