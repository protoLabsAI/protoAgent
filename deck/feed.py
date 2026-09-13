"""The fleet-wide work feed (#3470): every tool call across the fleet, as it happens.

The model (:class:`Activity`) folds the fan-in's bus events into two things the UI reads:

- a bounded, time-ordered list of :class:`Row` — one per tool start/end, turn start/finish,
  room reply, spend line, resume — across every watched member;
- per-member :class:`TurnState` — is a turn running, is one parked on a question, when
  was the member last active, what did the last turn cost — which drives the roster's
  TURN column and the bell.

Two sources feed it. Server-fired turns (scheduler, watch, inbox, webhook, background, a
delegate's result) publish ``chat.progress`` + ``turn.started``/``turn.finished`` on the
member's bus. Turns a console or the deck streams itself are NOT republished (they would
render twice there), so for those the deck's own conversations report into the same model
(``Activity.note_live`` / ``note_tool``), and ``turn.usage`` — published for EVERY terminal
turn — closes them. A parked question is found by a low-frequency probe of each online
member's session inventory (``diagnostics/sessions``: ``latest_task_state`` of
``input-required``), because the executor's pause is not republished on the bus.

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
RUNNING_TTL_S = 15 * 60  # a `tool_start` with no end in this long is assumed dead (a lost turn.finished)


@dataclass
class Row:
    ts: float
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


@dataclass
class TurnState:
    running: set[str] = field(default_factory=set)  # task ids (or session ids) in flight
    open_tools: dict[str, tuple[str, float]] = field(default_factory=dict)  # tool_id → (name, started)
    parked: str = ""  # the question / title when a turn waits on the operator
    last_active: float | None = None  # monotonic
    last_cost_usd: float | None = None
    last_model: str = ""

    @property
    def is_running(self) -> bool:
        if self.running:
            return True
        now = time.monotonic()
        return any(now - started < RUNNING_TTL_S for _, started in self.open_tools.values())


class Activity:
    def __init__(self, names: dict[str, str] | None = None):
        self.rows: deque[Row] = deque(maxlen=MAX_ROWS)
        self.state: dict[str, TurnState] = {}
        self.names: dict[str, str] = dict(names or {})  # slug → display name
        self.newly_parked: list[str] = []  # slugs that became parked since the last drain (bell)

    def _st(self, slug: str) -> TurnState:
        return self.state.setdefault(slug, TurnState())

    def _name(self, slug: str) -> str:
        return self.names.get(slug, slug)

    def _add(self, row: Row) -> None:
        self.rows.append(row)
        st = self._st(row.slug)
        if row.kind != "needs-you":
            st.last_active = row.ts

    # ── bus events ──

    def apply(self, ev: deckevents.Event) -> None:
        slug, d, now = ev.slug, ev.data, ev.received_at
        st = self._st(slug)
        if ev.topic == "chat.progress":
            p = deckevents.parse_progress(d)
            if p is None:
                return
            controllable = bool((p.control or {}).get("operator_controllable"))
            if p.task_id:
                st.running.add(p.task_id)
            if p.kind == "tool":
                if p.done:
                    started = st.open_tools.pop(p.tool_id, (p.name, None))[1]
                    dur = f"  {now - started:.1f}s" if started else ""
                    self._add(Row(now, slug, self._name(slug), "tool", "✗" if p.error else "✓", p.name or "tool", (_clip(p.output) + dur).strip(), p.session, p.task_id, p.tool_id, p.error, controllable))
                else:
                    st.open_tools[p.tool_id] = (p.name, now)
                    self._add(Row(now, slug, self._name(slug), "tool", "⟳", p.name or "tool", "", p.session, p.task_id, p.tool_id, False, controllable))
            elif p.kind == "room":
                self._add(Row(now, slug, self._name(slug), "room", "✓" if p.ok else "✗", f"@{p.author} replied", _clip(p.text), p.session, p.task_id))
            elif p.kind == "ask":
                self._add(Row(now, slug, self._name(slug), "room", "⟳", f"asked @{p.addressed_to}", _clip(p.text), p.session, p.task_id))
            elif p.kind == "steer":
                self._add(Row(now, slug, self._name(slug), "turn", "·", "steer folded in", _clip("; ".join(i["text"] for i in p.items)), p.session, p.task_id))
            # text frames are the transcript's business, not the feed's
        elif ev.topic == "turn.started":
            sid = str(d.get("session_id") or "")
            st.running.add(sid)
            self._add(Row(now, slug, self._name(slug), "turn", "⟳", "turn started", f"{d.get('origin', '')} · {d.get('trigger', '')}".strip(" ·"), sid, str(d.get("task_id") or "")))
        elif ev.topic == "turn.finished":
            sid = str(d.get("session_id") or "")
            st.running.discard(sid)
            if d.get("task_id"):
                st.running.discard(str(d["task_id"]))
            ok = d.get("ok")
            self._add(Row(now, slug, self._name(slug), "turn", "✓" if ok is not False else "✗", "turn finished", f"{d.get('origin', '')}".strip(), sid, str(d.get("task_id") or ""), error=ok is False))
        elif ev.topic == "turn.usage":
            tid = str(d.get("task_id") or "")
            st.running.discard(tid)
            st.open_tools.clear()  # a terminal turn ends every tool it had open
            cost = _num(d.get("cost_usd"))
            st.last_cost_usd = cost
            st.last_model = str(d.get("model") or "")
            if st.parked and _state(d.get("state")) != "input-required":
                st.parked = ""
            state = _state(d.get("state"))
            self._add(Row(now, slug, self._name(slug), "usage", "$", f"turn {state or 'done'}", f"${cost:,.4f} · {int(_num(d.get('input_tokens'))):,} in · {int(_num(d.get('output_tokens'))):,} out" + (f" · {st.last_model}" if st.last_model else ""), str(d.get("context_id") or ""), tid, error=state == "failed"))
        elif ev.topic == "turn.resumed":
            st.parked = ""
            self._add(Row(now, slug, self._name(slug), "resume", "⚑", "question answered", "", str(d.get("context_id") or ""), str(d.get("task_id") or "")))
        elif ev.topic == "chat.resumed":
            sid = str(d.get("session_id") or "")
            st.running.discard(sid)
            self._add(Row(now, slug, self._name(slug), "turn", "✗" if d.get("error") else "✓", "settled", _clip(str(d.get("text") or d.get("error") or "")), sid, str(d.get("task_id") or ""), error=bool(d.get("error"))))

    # ── the deck's own conversations ──

    def note_live(self, slug: str, task_id: str, running: bool) -> None:
        st = self._st(slug)
        if running and task_id:
            st.running.add(task_id)
        elif task_id:
            st.running.discard(task_id)
        if running:
            st.last_active = time.monotonic()

    def note_tool(self, slug: str, session: str, task_id: str, tool_id: str, name: str, *, done: bool, output: str = "", error: bool = False) -> None:
        now = time.monotonic()
        st = self._st(slug)
        if done:
            started = st.open_tools.pop(tool_id, (name, None))[1]
            dur = f"  {now - started:.1f}s" if started else ""
            self._add(Row(now, slug, self._name(slug), "tool", "✗" if error else "✓", name, (_clip(output) + dur).strip(), session, task_id, tool_id, error))
        else:
            st.open_tools[tool_id] = (name, now)
            self._add(Row(now, slug, self._name(slug), "tool", "⟳", name, "", session, task_id, tool_id))

    def set_parked(self, slug: str, question: str) -> None:
        st = self._st(slug)
        was = st.parked
        st.parked = question
        if question and not was:
            self.newly_parked.append(slug)
            self._add(Row(time.monotonic(), slug, self._name(slug), "needs-you", "⚑", "needs you", _clip(question)))

    def note_fleet(self, slug: str, text: str) -> None:
        self._add(Row(time.monotonic(), slug, self._name(slug), "fleet", "·", "fleet", text))

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

    def last_active_cell(self, slug: str, now: float | None = None) -> str:
        st = self.state.get(slug)
        if st is None or st.last_active is None:
            return ""
        now = time.monotonic() if now is None else now
        s = int(now - st.last_active)
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
        base = time.time() - time.monotonic()
        for r in rows:
            wall = time.strftime("%H:%M:%S", time.localtime(base + r.ts))
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
