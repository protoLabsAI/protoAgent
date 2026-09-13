"""The conversation screen (#3469): talk to a member and watch its turn.

Left, the transcript: your messages and the member's answers (Markdown, updated as the
canonical text grows — replace semantics, never append-only, so the terminal frame's full
re-send can never double a line). Right, the WORK pane: every tool call of the current turn
as a tree — a subagent's own calls nested under its ``task`` card — with args/result
previews, durations, and the true result size; ``enter`` on a card opens the full args and
result in a pager. Thinking is folded behind a one-line count (``z`` unfolds). The composer
sends when the member is idle; ``esc`` cancels a running turn (``CancelTask``), else backs
out. ``ctrl+n`` starts a new session (the console's ``chat-`` id shape, so it appears there
too), ``ctrl+s`` opens the member's session list and replays a session's durable turns —
tool cards included.

One reducer for everything (``deck.a2a``): the live stream, a session replay, and the
stall watchdog's ``GetTask`` finalize all fold through :func:`deck.a2a.apply_frame`. All
network I/O runs in thread workers; the UI thread only ever renders a :class:`Turn`.

Acting on a turn (#3470): a parked question / form / approval opens as a modal (``enter`` on
the empty composer, or ``ctrl+r``) and the answer resumes the parked task the way the console
does (``metadata.hitl_resume``); typing while the member works STEERS the running turn (the
message folds in at its next model call — ``up`` on the empty composer pulls the newest
queued one back); ``ctrl+x`` on a running ``task`` card cancels that one delegation. While
this screen is open the session is ATTENDED (``/api/chat/attend``), so a scheduled turn in
it parks on a question instead of auto-answering — and it shows up here: the screen
attaches to a server-fired turn the bus announces (``SubscribeToTask``), and to a turn still
running when a session is opened; an attended server turn takes interjections from the
composer too.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, ListItem, ListView, Markdown, Static, Tree

from textual.css.query import NoMatches

from deck import a2a
from deck import hitl as deckhitl
from deck.data import display_name, presence_of, slug_of

STALL_IDLE_S = 45.0
MAX_RECONNECTS = 10  # 10 silent windows ≈ 10 minutes of a tool call that says nothing
_PREVIEW = 60


def _preview(v: Any, n: int = _PREVIEW) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _pretty(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        try:
            return json.dumps(json.loads(v), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            return v
    return json.dumps(v, indent=2, ensure_ascii=False, default=str)


def _glyph(status: str) -> str:
    return {"running": "⟳", "done": "✓", "error": "✗"}.get(status, "·")


def _ui_safe(fn):
    """A render that lands after the screen was popped (a worker unwinding, a late
    call_from_thread) finds no widgets: that is not an error, just nothing to draw."""

    def wrapper(self, *a, **kw):
        if not self.is_attached:
            return None
        try:
            return fn(self, *a, **kw)
        except NoMatches:
            return None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _cost_line(turn: a2a.Turn) -> str:
    u = turn.usage
    if u is None:
        return ""
    cost = f"${u.cost_usd:,.4f}" if u.cost_usd is not None else "—"
    bits = [cost, f"{u.input_tokens:,} in", f"{u.output_tokens:,} out"]
    if u.cache_read_tokens:
        bits.append(f"cache {u.cache_read_tokens:,}")
    if u.duration_ms:
        bits.append(f"{u.duration_ms / 1000:.1f}s")
    return " · ".join(bits)


@dataclass
class Exchange:
    """One user → assistant pair on the transcript."""

    user: str
    turn: a2a.Turn
    live: bool = False
    widget_id: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    cancel_requested: bool = False
    client: Any = None  # the A2AClient holding this exchange's stream (abort handle)
    lock: threading.Lock = field(default_factory=threading.Lock)  # two workers, one Turn
    last_body: str | None = None  # last markdown rendered, to skip no-op re-parses
    last_work_sig: tuple | None = None  # last tool-tree signature, to skip no-op rebuilds
    reported: set = field(default_factory=set)  # (tool_id, status) already told to the activity feed
    attached: bool = False  # a turn somebody else started (scheduler, inbox…) that we subscribed to
    detached: bool = False  # we stopped watching an attached turn; the member goes on with it
    finished: bool = False  # _finish ran for this life of the exchange (the stall probe and the reader both reach it)
    generation: int = 0  # bumped each time the exchange lives again (a resume, a re-attach): stale async results are dropped
    submitting: bool = False  # a plugin form's answers are on their way to the member; the prompt must not reopen meanwhile
    origin: str = ""  # who started an attached turn
    controllable: bool = False  # an attached server turn that takes interjections


@dataclass
class Steer:
    """A message queued into the running turn, until the member folds it in."""

    id: str
    text: str
    consumed: bool = False
    interjection: bool = False  # into a server-fired turn (a different route, same queue)


@dataclass
class Conversation:
    """The screen's model: a session and its exchanges, newest last."""

    session_id: str
    exchanges: list[Exchange] = field(default_factory=list)
    steers: list[Steer] = field(default_factory=list)

    @property
    def parked(self) -> Exchange | None:
        """The exchange waiting on the operator (input-required), if any."""
        if self.live is not None:
            return None
        ex = self.latest
        return ex if ex is not None and ex.turn.hitl and not ex.turn.done else None

    @property
    def queued(self) -> list[Steer]:
        return [st for st in self.steers if not st.consumed]

    @property
    def live(self) -> Exchange | None:
        return next((e for e in reversed(self.exchanges) if e.live), None)

    @property
    def latest(self) -> Exchange | None:
        return self.exchanges[-1] if self.exchanges else None


# ── screens ───────────────────────────────────────────────────────────────────


class PagerScreen(Screen):
    """Full args and result of one tool call."""

    BINDINGS = [Binding("escape", "back", "back", show=True), Binding("q", "back", "back", show=False)]

    def __init__(self, call: a2a.ToolCall) -> None:
        super().__init__()
        self.call = call

    def compose(self) -> ComposeResult:
        c = self.call
        head = f"{_glyph(c.status)} {c.name}   {c.status}" + (f" · {c.duration_ms} ms" if c.duration_ms else "") + (f" · {c.output_chars:,} chars" if c.output_chars else "")
        yield Static(head, id="pager-head")
        with VerticalScroll(id="pager-body"):
            yield Static("ARGS", classes="pager-label")
            yield Static(_pretty(c.input) or "—", classes="pager-block")
            yield Static("RESULT", classes="pager-label")
            yield Static(_pretty(c.output) or "—", classes="pager-block")
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()


class SessionPicker(Screen[str | None]):
    """The member's console sessions, newest first; enter opens, esc keeps the current one."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=True), Binding("ctrl+n", "fresh", "new session", show=True)]

    def __init__(self, name: str, sessions: list[dict], current: str) -> None:
        super().__init__()
        self._name, self._sessions, self._current = name, sessions, current

    def compose(self) -> ComposeResult:
        yield Static(f"{self._name} · sessions (newest first) · enter opens · ctrl+n new", id="picker-head")
        items = []
        for s in self._sessions:
            sid = str(s.get("session_id"))
            mark = "▸ " if sid == self._current else "  "
            items.append(ListItem(Static(f"{mark}{sid}   {s.get('turn_count', '?')} turns   {str(s.get('last_updated') or '')[:16].replace('T', ' ')}"), name=sid))
        if not items:
            items.append(ListItem(Static("  (no console sessions on this member yet — ctrl+n starts one)"), name=""))
        yield ListView(*items, id="picker")
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_fresh(self) -> None:
        self.dismiss("__new__")


class ConversationScreen(Screen):
    BINDINGS = [
        Binding("escape", "esc", "stop / back", show=True, priority=True),
        Binding("tab", "cycle", "transcript ↔ work ↔ composer", show=True, priority=True),
        Binding("ctrl+n", "new_session", "new session", show=True, priority=True),
        Binding("ctrl+s", "sessions", "sessions", show=True, priority=True),
        Binding("ctrl+z", "fold", "thinking", show=True, priority=True),
        Binding("ctrl+r", "respond", "answer", show=True, priority=True),
        Binding("ctrl+x", "cancel_delegation", "cancel delegation", show=True),  # the composer's ctrl+x (cut) wins while it has focus
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, agent: dict, *, session_id: str | None = None) -> None:
        super().__init__()
        self.agent = agent
        self.slug = slug_of(agent)
        self.member_name = display_name(agent)
        self.convo = Conversation(session_id=session_id or a2a.new_session_id())
        self.show_reasoning = False
        self._seq = 0
        self._load_seq = 0  # stale session loads (a slow member, then a faster pick) are ignored
        self._watchdog: Any = None
        self.reconnects = 0  # SubscribeToTask re-attachments after a silent stretch
        self._attendance: Any = None  # the open /api/chat/attend stream for this session
        self._attend_warned = False
        self._steer_seq = 0

    # ── layout ──

    def compose(self) -> ComposeResult:
        yield Static("", id="talk-head")
        with Horizontal(id="talk-body"):
            yield VerticalScroll(id="transcript")
            with Vertical(id="work"):
                yield Static("WORK", id="work-head")
                tree: Tree = Tree("turn", id="work-tree")
                tree.show_root = False
                tree.guide_depth = 3
                yield tree
                yield Static("", id="cost")
        yield Static("", id="talk-status")
        yield Input(placeholder=f"message {self.member_name}… (enter sends, esc stops/backs out)", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self._render_head()
        self.query_one("#composer", Input).focus()
        self._watchdog = self.set_interval(5.0, self._check_stall)
        self.load_session(self.convo.session_id)
        self._attend(self.convo.session_id)

    # ── attendance ──

    def _attend(self, session_id: str) -> None:
        """Hold the session attended while this screen shows it (a server-fired turn in it
        then parks for us instead of auto-answering, and takes interjections)."""
        self._unattend()
        self._attend_warned = False
        try:
            self._attendance = self.app.backend.attend(self.agent, session_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — presence is best effort
            self._attendance = None
            self.notify(f"could not mark the session attended: {exc}", severity="warning")

    def _unattend(self) -> None:
        h, self._attendance = self._attendance, None
        if h is not None:
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass

    @_ui_safe
    def _render_head(self) -> None:
        pres = presence_of(self.agent)
        n = len(self.convo.exchanges)
        self.query_one("#talk-head", Static).update(f"◂ {self.member_name} · {self.convo.session_id} · {n} turn{'s' if n != 1 else ''}   {pres} · :{self.agent.get('port') or '—'}")

    # ── sessions ──

    def _loading(self) -> bool:
        return any(w.group == "talk-load" and w.is_running for w in self.workers)

    def load_session(self, session_id: str) -> None:
        self._load_seq += 1
        self._load_session(session_id, self._load_seq)

    @work(thread=True, exclusive=True, group="talk-load")
    def _load_session(self, session_id: str, seq: int) -> None:
        backend = self.app.backend  # type: ignore[attr-defined]
        try:
            rows = backend.turns(self.agent, session_id)
        except Exception as exc:  # noqa: BLE001 — surfaced, never fatal
            self.app.call_from_thread(self.notify, f"could not load {session_id}: {exc}", severity="error", timeout=8)
            rows = []
        exchanges = [Exchange(user=a2a.user_text_from_durable(r), turn=a2a.turn_from_durable(session_id, r)) for r in rows]
        self.app.call_from_thread(self._apply_session, session_id, exchanges, seq)

    @_ui_safe
    def _apply_session(self, session_id: str, exchanges: list[Exchange], seq: int | None = None) -> None:
        if seq is not None and seq != self._load_seq:
            return  # a newer load superseded this one (exclusive cannot stop a thread)
        if self.convo.live is not None:
            # A load landing while a turn streams must not orphan the live exchange (the
            # status would read "idle" and a second stream could start on the same session).
            return
        if session_id != self.convo.session_id or self._attendance is None:
            self._attend(session_id)
        steers = list(self.convo.steers) if session_id == self.convo.session_id else []  # a reload of the same session keeps what is queued
        self.convo = Conversation(session_id=session_id, exchanges=exchanges, steers=steers)
        self._rebuild_transcript()
        self._render_work(self.convo.latest.turn if self.convo.latest else None)
        self._render_status()
        self._render_head()
        latest = self.convo.latest
        act = getattr(self.app, "activity", None)
        server = act.server_turn(self.slug, session_id) if act is not None else None
        if server is not None:
            # the bus has shown a server-fired turn running in this session: watch it (its
            # durable row, if the turns read already has it, is the exchange to continue)
            same = latest if latest is not None and latest.turn.task_id == server["task_id"] else None
            self._attach_turn(server["task_id"], origin=server.get("origin") or "server", trigger=server.get("trigger") or "", controllable=bool(server.get("controllable")), replace=same)
        elif latest is not None and latest.turn.task_id and not latest.turn.done and latest.turn.state in ("working", "submitted"):
            # a turn still running when the session was opened (the console, a schedule…)
            self._attach_turn(latest.turn.task_id, origin="in flight", replace=latest)

    def action_new_session(self) -> None:
        if self.convo.live:
            self.notify("a turn is running — esc stops it first", severity="warning")
            return
        self._load_seq += 1  # an in-flight load must not land over the fresh session
        self._apply_session(a2a.new_session_id(), [])
        self.notify("new session")

    def action_sessions(self) -> None:
        self._open_picker()

    @work(thread=True, exclusive=True, group="talk-sessions")
    def _open_picker(self) -> None:
        try:
            sessions = self.app.backend.sessions(self.agent)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, f"could not list sessions: {exc}", severity="error", timeout=8)
            return
        if not self.is_attached:
            return  # esc won the race: nothing to pick for
        self.app.call_from_thread(self.app.push_screen, SessionPicker(self.member_name, sessions, self.convo.session_id), self._picked)

    def _picked(self, sid: str | None) -> None:
        if sid is None:
            return
        if sid == "__new__":
            self.action_new_session()
            return
        if self.convo.live:
            self.notify("a turn is running — esc stops it first", severity="warning")
            return
        self.load_session(sid)

    # ── sending / streaming ──

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if self.app.backend.mode == "offline":  # type: ignore[attr-defined]
            self.notify("offline — no hub to talk to this member through", severity="error")
            return
        parked = self.convo.parked
        if parked is not None:
            # the member is waiting on us: a plain question takes the typed text as the
            # answer; a form or an approval needs its modal (typed text stays in the composer)
            if text and deckhitl.kind_of(parked.turn.hitl) == "question":
                event.input.value = ""
                self._answer(parked, text)
            else:
                self.action_respond()
            return
        if not text:
            return
        live = self.convo.live
        if live is not None:
            event.input.value = ""
            if live.attached:
                if live.controllable:
                    self._queue_steer(text, interjection=True, task_id=live.turn.task_id)
                else:
                    self.notify(f"this turn was started by the {live.origin or 'server'} and is not taking messages", severity="warning")
                    event.input.value = text
            else:
                self._queue_steer(text)
            return
        if self._loading():
            self.notify("still loading this session — one moment", severity="warning")
            return
        event.input.value = ""
        self._send(text)

    def _send(self, text: str) -> None:
        if self.convo.live is not None:
            self.notify("a turn is running — the message was queued instead", severity="warning")
            self._queue_steer(text)
            return
        turn = a2a.Turn(context_id=self.convo.session_id)
        ex = Exchange(user=text, turn=turn, live=True)
        self.convo.exchanges.append(ex)
        self._append_exchange(ex)
        self._render_work(turn)
        self._render_status()
        self._render_head()
        self._stream(ex, text=text)

    # ── answering a parked turn ──

    def action_respond(self) -> None:
        parked = self.convo.parked
        if parked is None:
            self.notify("nothing is waiting for you here", severity="warning")
            return
        if parked.submitting:
            self.notify("the form is being submitted — one moment", severity="warning")
            return
        draft = ""
        try:
            draft = self.query_one("#composer", Input).value.strip()
        except NoMatches:
            pass
        opened = dict(parked.turn.hitl or {})  # the prompt this modal answers; a different one by the time it closes is refused
        deckhitl.open_prompt(self.app, self.member_name, opened, lambda result: self._answered(parked, result, opened=opened), draft=draft)

    def _current(self, ex: Exchange) -> Exchange | None:
        """The exchange as this conversation holds it NOW — a modal's callback captured an
        object that a session switch or reload may have replaced (same task → the new one)."""
        if ex in self.convo.exchanges:
            return ex
        if ex.turn.task_id:
            return next((e for e in self.convo.exchanges if e.turn.task_id == ex.turn.task_id), None)
        return None

    def _answered(self, parked: Exchange, result: Any, *, opened: dict | None = None) -> None:
        if result is None:
            return  # closed — still parked
        current = self._current(parked)
        if current is None:
            self.notify("that question belongs to a session that is no longer open here", severity="warning")
            return
        parked = current
        hitl = parked.turn.hitl
        if not hitl or parked.turn.done or parked.live:
            # answered elsewhere (the console, another deck) while the modal was open — the
            # bus told us and the exchange moved on; this answer would be an ordinary turn
            self.notify("that prompt was already answered elsewhere — the member has moved on", severity="warning", timeout=8)
            if isinstance(result, str) and result != "__dismiss__":
                self._set_composer(result)
            return
        if opened is not None and (opened.get("plugin_callback_id") or "") != (hitl.get("plugin_callback_id") or ""):
            self.notify("the form changed while you were answering — open it again (ctrl+r)", severity="warning", timeout=8)
            return
        self._set_composer("")
        if hitl.get("plugin_callback_id"):
            if result == "__dismiss__":
                parked.turn.hitl = None  # a plugin form has no parked graph: just close it
                self._settle_form(parked)
                self._render_live(parked)
                self._render_status()
                return
            parked.submitting = True
            self._render_status()
            self._submit_plugin_form(parked, str(hitl["plugin_callback_id"]), result if isinstance(result, dict) else {})
            return
        if result == "__dismiss__":
            self._resume(parked, a2a.DISMISS_SENTINEL, hidden=True)
            return
        kind = deckhitl.kind_of(hitl)
        self._resume(parked, deckhitl.answer_text(kind, result), hidden=kind == "approval")

    def _answer(self, parked: Exchange, text: str) -> None:
        self._resume(parked, text, hidden=False)

    def _resume(self, parked: Exchange, text: str, *, hidden: bool) -> None:
        """Resume the parked task with the answer (``metadata.hitl_resume``). The exchange
        continues — the resumed stream replays the task snapshot, so a second exchange would
        show the pre-park text twice. An approval or a dismissal is silent (its outcome shows
        on the tool card); a question's or form's answer is conversation and gets a line."""
        task_id = parked.turn.task_id
        if self.convo.live is not None:
            self.notify("a turn is running", severity="warning")
            return
        ex = parked
        ex.turn.hitl = None
        ex.turn.done = False
        ex.error = ""
        ex.live = True
        ex.finished = False
        ex.generation += 1
        ex.cancel_requested = False
        ex.started_at = time.monotonic()
        ex.turn.last_frame_at = ex.started_at
        if not hidden:
            self._mount_answer(ex, text)
        self._render_live(ex)
        self._render_work(ex.turn)
        self._render_status()
        self._render_head()
        self._stream(ex, text=text, task_id=task_id, metadata={"hitl_resume": True, **({"hidden": True} if hidden else {})})

    @_ui_safe
    def _mount_answer(self, ex: Exchange, text: str) -> None:
        tr = self.query_one("#transcript", VerticalScroll)
        try:
            md = self.query_one(f"#{ex.widget_id}-md", Markdown)
        except NoMatches:
            return
        tr.mount(Static(Text(f"you ›  {_preview(text, 200)}", style="bold"), classes="answer-msg"), before=md)

    @work(thread=True, group="talk-form")
    def _submit_plugin_form(self, parked: Exchange, callback_id: str, answers: dict) -> None:
        app = self.app
        try:
            out = app.backend.submit_form(self.agent, self.convo.session_id, callback_id, answers)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            parked.submitting = False
            app.call_from_thread(self._render_status)
            app.call_from_thread(self.notify, f"form submit failed: {exc}", severity="error", timeout=8)
            return
        form = out.get("form") if isinstance(out.get("form"), dict) else None
        if form is not None:
            nxt = {**form, "plugin_callback_id": out.get("callback_id") or callback_id}
            app.call_from_thread(self._next_form_step, parked, nxt)
            return
        app.call_from_thread(self._form_landed, parked, str(out.get("reply") or ""))

    @_ui_safe
    def _form_landed(self, parked: Exchange, reply: str) -> None:
        current = self._current(parked) or parked
        with current.lock:
            current.turn.hitl = None
        current.submitting = False
        self._settle_form(current)
        self._render_live(current)
        self._render_status()
        if reply:
            self.notify(reply, timeout=8)

    def _settle_form(self, ex: Exchange) -> None:
        """A redeemed (or closed) plugin form is over for good — but a member that does not
        complete the form's task keeps reporting it parked; the roster must not re-park it."""
        act = getattr(self.app, "activity", None)
        if act is not None:
            act.unpark(self.slug, ex.turn.context_id, settle_task=ex.turn.task_id)

    def _next_form_step(self, parked: Exchange, nxt: dict) -> None:
        """A wizard's next step, on the exchange this conversation holds now."""
        current = self._current(parked) or parked
        with current.lock:
            current.turn.hitl = nxt
        current.submitting = False
        self._render_status()
        self.action_respond()

    # ── attaching to a turn somebody else started ──

    def _attach_turn(self, task_id: str, *, origin: str, trigger: str = "", controllable: bool = False, replace: Exchange | None = None) -> None:
        """Watch a running turn we did not start: subscribe to its task (a snapshot, then
        the live frames) and render it like our own."""
        if self.convo.live is not None:
            return
        label = f"({origin}{f' · {trigger}' if trigger else ''} turn)"
        if replace is not None:
            ex = replace
            ex.turn = a2a.Turn(context_id=self.convo.session_id, task_id=task_id)  # the snapshot replays it all; the durable copy would double it
            ex.last_body = ex.last_work_sig = None
            ex.live = True
            ex.finished = False
            ex.generation += 1
            ex.attached = True
            ex.origin = origin
            ex.controllable = controllable
            ex.started_at = time.monotonic()
        else:
            ex = Exchange(user=label, turn=a2a.Turn(context_id=self.convo.session_id, task_id=task_id), live=True, attached=True, origin=origin, controllable=controllable)
            self.convo.exchanges.append(ex)
            self._append_exchange(ex)
        self._render_work(ex.turn)
        self._render_status()
        self._render_head()
        self._stream(ex, subscribe=task_id)

    def on_bus_events(self, evs: list) -> None:
        """Bus events the deck drained (all members): a server-fired turn running in THIS
        session is attached — from its ``chat.progress`` frames, which carry the task id
        and the control block (the scheduler's ``turn.started`` names only the session);
        a park landing here while nothing is attached reloads the durable turns so the
        prompt shows."""
        sid = self.convo.session_id
        for ev in evs:
            if ev.slug != self.slug:
                continue
            d = ev.data
            if ev.topic == "chat.progress" and str(d.get("session_id") or "") == sid:
                tid = str(d.get("task_id") or "")
                live = self.convo.live
                if tid and live is None and self.convo.parked is None:
                    ctl = d.get("control") if isinstance(d.get("control"), dict) else {}
                    self._attach_turn(tid, origin=str(d.get("origin") or ctl.get("origin") or "server"), trigger=str(d.get("trigger") or ctl.get("trigger") or ""), controllable=bool(ctl.get("operator_controllable")))
                elif live is not None and live.attached and live.turn.task_id == tid:
                    ctl = d.get("control") if isinstance(d.get("control"), dict) else None
                    if ctl is not None:
                        live.controllable = bool(ctl.get("operator_controllable"))
                        self._render_status()
            elif ev.topic == "turn.input_required" and str(d.get("context_id") or "") == sid:
                tid = str(d.get("task_id") or "")
                known = any(e.turn.task_id == tid for e in self.convo.exchanges) if tid else False
                if self.convo.live is None and self.convo.parked is None and not known and not self._loading() and self.app.screen is self:
                    self.load_session(sid)  # a park we did not watch happen: show it
            elif ev.topic in ("turn.resumed", "turn.usage", "turn.finished", "chat.resumed"):
                # a park WE hold, answered or ended elsewhere (the console, another deck): the
                # prompt is gone — follow the continued turn, or show how it ended
                parked = self.convo.parked
                tid = str(d.get("task_id") or "")
                ev_sid = str(d.get("context_id") or d.get("session_id") or "")
                if parked is None or ev_sid != sid or not tid or parked.turn.task_id != tid:
                    continue
                if ev.topic == "turn.usage" and str(d.get("state") or "").replace("TASK_STATE_", "").lower().replace("_", "-") == "input-required":
                    continue  # the park leg's own spend line: still parked
                parked.turn.hitl = None
                if ev.topic == "turn.resumed":
                    self._render_live(parked)
                    self._render_status()
                    if self.app.screen is self:
                        self.notify("answered elsewhere — following the turn", timeout=6)
                    self._attach_turn(tid, origin="answered elsewhere", replace=parked)
                else:
                    parked.turn.done = True
                    self._render_live(parked)
                    self._render_status()
                    if not self._loading() and self.app.screen is self:
                        self.load_session(sid)  # the durable record has how it ended

    # ── steering a running turn ──

    def _queue_steer(self, text: str, *, interjection: bool = False, task_id: str = "") -> None:
        self._steer_seq += 1
        st = Steer(id=f"{uuid.uuid4().hex}", text=text, interjection=interjection)
        self.convo.steers.append(st)
        self._mount_steer(st)
        self._post_steer(st, task_id)

    @_ui_safe
    def _mount_steer(self, st: Steer) -> None:
        tr = self.query_one("#transcript", VerticalScroll)
        tr.mount(Static(self._steer_text(st), id=f"steer-{st.id}", classes="steer-msg"))
        tr.scroll_end(animate=False)
        self._render_status()

    @staticmethod
    def _steer_text(st: Steer) -> Text:
        tag = "folded in" if st.consumed else ("queued — interjection" if st.interjection else "queued — folds in at the next model call · up to take it back")
        t = Text(f"you ›  {st.text}", style="" if st.consumed else "dim")
        t.append(f"   ({tag})", style="dim italic")
        return t

    @_ui_safe
    def _render_steer(self, st: Steer) -> None:
        try:
            self.query_one(f"#steer-{st.id}", Static).update(self._steer_text(st))
        except NoMatches:
            pass

    @_ui_safe
    def _drop_steer(self, st: Steer) -> None:
        if st in self.convo.steers:
            self.convo.steers.remove(st)
        try:
            self.query_one(f"#steer-{st.id}", Static).remove()
        except NoMatches:
            pass
        self._render_status()

    @work(thread=True, group="talk-steer")
    def _post_steer(self, st: Steer, task_id: str) -> None:
        app = self.app
        try:
            if st.interjection:
                out = app.backend.interject(self.agent, self.convo.session_id, task_id, st.id, st.text)  # type: ignore[attr-defined]
                if not out.get("ok", True):
                    raise RuntimeError(str(out.get("reason") or "refused"))
            else:
                app.backend.steer(self.agent, self.convo.session_id, st.id, st.text)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self._drop_steer, st)
            app.call_from_thread(self.notify, f"could not queue the message: {exc}", severity="error", timeout=8)

    def on_key(self, event) -> None:
        # up on the EMPTY composer: pull the newest queued steer back into it to edit
        if event.key != "up":
            return
        try:
            comp = self.query_one("#composer", Input)
        except NoMatches:
            return
        if self.focused is not comp or comp.value:
            return
        queued = self.convo.queued
        if not queued:
            return
        event.stop()
        self._unqueue_steer(queued[-1])

    @work(thread=True, group="talk-steer")
    def _unqueue_steer(self, st: Steer) -> None:
        app = self.app
        try:
            removed = app.backend.steer_cancel(self.agent, self.convo.session_id, st.id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self.notify, f"could not take the message back: {exc}", severity="error", timeout=8)
            return
        if removed:
            app.call_from_thread(self._drop_steer, st)
            app.call_from_thread(self._set_composer, st.text)
        else:
            st.consumed = True
            app.call_from_thread(self._render_steer, st)
            app.call_from_thread(self.notify, "too late — the member already read it (it is in the composer to send again if you want)", severity="warning", timeout=8)
            app.call_from_thread(self._set_composer, st.text)

    @_ui_safe
    def _set_composer(self, text: str) -> None:
        comp = self.query_one("#composer", Input)
        comp.value = text
        comp.cursor_position = len(text)
        comp.focus()

    @work(thread=True, group="talk-steer-reconcile")
    def _reconcile_steers(self, ex: Exchange) -> None:
        """At turn end: the steers the member folded in settle; the ones still queued
        arrived after its last model call. Ours are re-sent as a fresh turn — unless the
        turn parked, in which case the server keeps holding them for after the answer;
        interjections into a server-fired turn are simply reported as unread (this screen
        never starts a turn in a session on the server's behalf)."""
        app = self.app
        convo, gen = self.convo, ex.generation  # what this reconcile is FOR; both may have moved on when the answer lands
        queued = [st for st in convo.steers if not st.consumed]
        if not queued:
            return
        try:
            pending = {r["id"] for r in app.backend.steer_pending(self.agent, convo.session_id)}  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — cannot tell consumed from not: leave them queued
            return
        app.call_from_thread(self._reconcile_landed, convo, ex, gen, queued, pending)

    @_ui_safe
    def _reconcile_landed(self, convo: Conversation, ex: Exchange, gen: int, queued: list[Steer], pending: set[str]) -> None:
        """On the UI thread, against the state that exists NOW: the session may have been
        switched, an answer may have resumed the turn (its own end reconciles again), a
        new turn may be streaming."""
        if convo is not self.convo or ex.generation != gen:
            return  # a different session, or the turn lived again since we asked: this answer is stale
        for st in queued:
            if st.id not in pending:
                st.consumed = True
                self._render_steer(st)
        left = [st for st in queued if st.id in pending]
        if not left:
            return
        if ex.turn.hitl or self.convo.parked is not None or self.convo.live is not None:
            return  # the server keeps holding them (a park), or a turn is running again: they fold in there
        for st in left:
            self._drop_steer(st)
        own = [st for st in left if not st.interjection]
        if len(own) < len(left):
            self.notify(f"{len(left) - len(own)} interjection(s) arrived after the turn's last model call and were not read", severity="warning", timeout=8)
        if own:
            self._send("\n\n".join(st.text for st in own))

    # ── cancelling one delegation ──

    def action_cancel_delegation(self) -> None:
        """The selected running ``task`` card — or the only one, when nothing is selected."""
        live = self.convo.live
        if live is None:
            self.notify("no turn is running", severity="warning")
            return
        try:
            node = self.query_one("#work-tree", Tree).cursor_node
        except NoMatches:
            node = None
        call = node.data if node is not None else None
        if not (isinstance(call, a2a.ToolCall) and call.name == "task" and call.status == "running"):
            running = [c for c in live.turn.tool_calls if c.name == "task" and c.status == "running"]
            if node is not None or len(running) != 1:
                # a selected card that is not a running delegation is an explicit choice: refuse
                self.notify("select the running task card to cancel in the WORK pane (tab, ↓)" if running else "no delegation is running", severity="warning")
                return
            call = running[0]
        self._cancel_delegation(call)

    @work(thread=True, group="talk-delegation")
    def _cancel_delegation(self, call: a2a.ToolCall) -> None:
        app = self.app
        try:
            cancelled = app.backend.delegation_cancel(self.agent, self.convo.session_id, call.id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self.notify, f"cancel failed: {exc}", severity="error", timeout=8)
            return
        app.call_from_thread(self.notify, f"delegation {_preview(call.input, 30) or call.id} cancelled — the lead continues" if cancelled else "too late — that delegation already finished", severity="information" if cancelled else "warning")

    @work(thread=True, group="talk-stream")
    def _stream(self, ex: Exchange, *, text: str | None = None, task_id: str | None = None, metadata: dict | None = None, subscribe: str | None = None) -> None:
        app = self.app
        try:
            client = app.backend.a2a(self.agent)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self._finish, ex, str(exc))
            return
        ex.client = client
        frames = client.subscribe(subscribe) if subscribe else client.stream(text or "", context_id=self.convo.session_id, task_id=task_id, metadata=metadata)
        try:
            for attempt in range(MAX_RECONNECTS + 1):
                try:
                    for frame in frames:
                        refused: a2a.TurnError | None = None
                        with ex.lock:
                            if ex.turn.done:
                                break  # the stall probe already finalized this turn
                            try:
                                a2a.apply_frame(ex.turn, frame)
                            except a2a.TurnError as exc:
                                refused = exc
                        if refused is not None:
                            # The server refused: for a task we know, ask the durable record
                            # first — "already in a terminal state" on a (re)subscribe means
                            # it finished, not that it failed. (Outside the lock: the
                            # finalizer takes it.)
                            task = self._task_or_none(ex) if ex.turn.task_id else None
                            state = a2a.norm_state(((task or {}).get("status") or {}).get("state")) if task else ""
                            if task and a2a.is_terminal(state):
                                self._finalize_from(ex, task, app, note="")
                                return
                            app.call_from_thread(self._finish, ex, str(refused))
                            return
                        app.call_from_thread(self._render_live, ex)
                        if ex.turn.done:
                            break
                    break  # the stream closed (or the turn is done)
                except a2a.StreamStalled:
                    # No frame in the read window. The server may have finished and lost
                    # the tail, or it may be deep in a silent tool call. Ask the durable
                    # task: terminal → finalize from it; still working → re-attach with
                    # SubscribeToTask (a snapshot, then the live frames) and carry on.
                    task = self._task_or_none(ex)
                    state = a2a.norm_state(((task or {}).get("status") or {}).get("state")) if task is not None else ""
                    if task is not None and (not state or a2a.is_terminal(state)):
                        self._finalize_from(ex, task, app)
                        return
                    if not ex.turn.task_id or attempt >= MAX_RECONNECTS:
                        app.call_from_thread(self._finish, ex, "stream timed out — the member sent nothing and could not be re-attached")
                        return
                    self.reconnects += 1
                    app.call_from_thread(self.notify, f"quiet for {a2a.STREAM_READ_S:g}s — re-attached to the running turn", severity="warning")
                    frames = client.subscribe(ex.turn.task_id)
        except Exception as exc:  # noqa: BLE001 — a broken stream is reported, the deck stays up
            if ex.detached:
                app.call_from_thread(self._finish, ex, "")
                return
            if not ex.turn.done and not ex.error:
                app.call_from_thread(self._finish, ex, str(exc))
                return
        finally:
            client.close()
        app.call_from_thread(self._finish, ex, "")

    def _task_or_none(self, ex: Exchange) -> dict | None:
        if not ex.turn.task_id or ex.client is None:
            return None
        try:
            return ex.client.get_task(ex.turn.task_id)
        except Exception:  # noqa: BLE001 — unknown; treat as not finished
            return None

    def _finalize_from(self, ex: Exchange, task: dict, app, note: str = "stream stalled — finalized from the durable task") -> None:
        """From a worker: the server finished but the stream tail was lost — finalize
        the exchange from the durable task. Never fabricates a completion."""
        with ex.lock:
            try:
                a2a.apply_frame(ex.turn, {"result": {"task": task}})
            except a2a.TurnError:
                pass
            ex.turn.done = True
        app.call_from_thread(self._finish, ex, "")
        if note:
            app.call_from_thread(self.notify, note, severity="warning")

    def _finish(self, ex: Exchange, error: str) -> None:
        # State first, always — even after the screen was popped (abandon / quit), so
        # `convo.live` can never stay truthy on an exchange whose reader has unwound.
        if ex.finished:
            return  # the stall probe finalized it and the reader then unwound: once is enough
        ex.finished = True
        ex.live = False
        if error and not ex.turn.done:
            ex.error = error
        act = getattr(self.app, "activity", None) if self.is_attached else None
        if act is not None:
            act.note_live(self.slug, ex.turn.task_id, False)
        self._finish_render(ex, error)
        if self.is_attached and self.convo.queued:
            self._reconcile_steers(ex)

    @_ui_safe
    def _finish_render(self, ex: Exchange, error: str) -> None:
        if ex.error and error:
            self.notify(f"turn failed: {error}", severity="error", timeout=8)
        self._render_live(ex)
        self._render_work(ex.turn)  # the final state, whatever the last frame carried
        self._render_status()
        self._render_head()

    def _check_stall(self) -> None:
        h = self._attendance
        if h is not None and getattr(h, "gave_up", False) and not self._attend_warned:
            self._attend_warned = True
            self.notify(f"this session is NOT attended — a scheduled turn here will answer itself: {getattr(h, 'last_error', '')}", severity="warning", timeout=10)
        ex = self.convo.live
        if ex is None:
            return
        idle = time.monotonic() - max(ex.turn.last_frame_at, ex.started_at)
        if idle < STALL_IDLE_S:
            return
        if not ex.turn.task_id:
            # Not even the initial Task frame in the whole window: the member accepted the
            # POST and produced nothing. Abort the reader; the worker fails the exchange.
            if ex.client is not None:
                ex.client.abort()
            return
        self._stall_probe(ex)

    @work(thread=True, exclusive=True, group="talk-stall")
    def _stall_probe(self, ex: Exchange) -> None:
        app = self.app  # captured on the worker's way in: a popped screen has no app
        if ex.client is None:
            return
        task = a2a.stalled_turn_is_terminal(ex.turn, ex.client.get_task, idle_s=STALL_IDLE_S)
        if task is None:
            return
        # The server finished but the stream tail was lost. Mark the turn done from the
        # durable task FIRST (under the lock), THEN wake the reader: whichever thread runs
        # next, the reader finds `done` and unwinds without failing the exchange.
        with ex.lock:
            if ex.turn.done:
                return  # the stream finished on its own while we asked
            try:
                a2a.apply_frame(ex.turn, {"result": {"task": task}})
            except a2a.TurnError:
                pass
            ex.turn.done = True
        ex.client.abort()
        app.call_from_thread(self._finish, ex, "")
        app.call_from_thread(self.notify, "stream stalled — finalized from the durable task", severity="warning")

    def action_esc(self) -> None:
        ex = self.convo.live
        if ex is not None:
            if ex.attached:
                # Somebody else's turn: stop WATCHING it — never CancelTask a schedule's work
                ex.detached = True
                ex.live = False
                if ex.client is not None:
                    ex.client.abort()
                self._leave()
                return
            if ex.cancel_requested:
                # Second esc (or the cancel failed): abandon locally — abort the reader,
                # settle the exchange, and leave. The server task is the server's.
                self._abandon(ex)
                return
            ex.cancel_requested = True
            self._render_status()
            self._cancel(ex)
            return
        self._leave()

    def _leave(self) -> None:
        if self._watchdog is not None:
            self._watchdog.stop()
        self._unattend()
        self.app.pop_screen()

    def _abandon(self, ex: Exchange) -> None:
        if ex.client is not None:
            ex.client.abort()
        ex.live = False
        if not ex.turn.done:
            ex.error = "abandoned — the server may still be working on it"
        self._render_live(ex)
        self._render_status()
        self._leave()

    def on_unmount(self) -> None:
        # Whatever pops this screen, no reader thread is left blocked on a socket.
        for ex in self.convo.exchanges:
            if ex.live and ex.client is not None:
                ex.client.abort()
        self._unattend()

    @work(thread=True, group="talk-cancel")
    def _cancel(self, ex: Exchange) -> None:
        app = self.app
        if not ex.turn.task_id or ex.client is None:
            app.call_from_thread(self.notify, "nothing to cancel yet — esc again to abandon", severity="warning")
            return
        try:
            ex.client.cancel(ex.turn.task_id)
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self.notify, f"cancel failed: {exc} — esc again to abandon", severity="error")
            return
        app.call_from_thread(self.notify, "cancel requested")

    # ── rendering ──

    @_ui_safe
    def _rebuild_transcript(self) -> None:
        tr = self.query_one("#transcript", VerticalScroll)
        tr.remove_children()
        for ex in self.convo.exchanges:
            self._append_exchange(ex)
        for st in self.convo.steers:
            self._mount_steer(st)

    @_ui_safe
    def _append_exchange(self, ex: Exchange) -> None:
        tr = self.query_one("#transcript", VerticalScroll)
        self._seq += 1
        ex.widget_id = f"ex-{self._seq}"
        tr.mount(Static(Text(f"{'' if ex.attached else 'you  '}{ex.user}", style="dim italic" if ex.attached else "bold"), classes="user-msg"))
        tr.mount(Static("", id=f"{ex.widget_id}-meta", classes="turn-meta"))
        tr.mount(Markdown(ex.turn.content or "", id=f"{ex.widget_id}-md", classes="assistant-msg"))
        self._render_live(ex)
        tr.scroll_end(animate=False)

    def _report(self, ex: Exchange) -> None:
        """Tell the fleet activity model about this (deck-originated) turn: the bus never
        republishes it, so the roster's TURN column and the work feed rely on this."""
        act = getattr(self.app, "activity", None)
        if act is None:
            return
        t = ex.turn
        if ex.attached and ex.origin != "in flight":
            # a server-fired turn is already on the bus (its own frames feed the model);
            # only the park is ours to say, below
            self._report_park(act, ex)
            return
        act.note_live(self.slug, t.task_id, ex.live and not t.done)
        for c in t.tool_calls:
            key = (c.id, c.status)
            if key in ex.reported:
                continue
            ex.reported.add(key)
            if c.status == "running":
                act.note_tool(self.slug, t.context_id, t.task_id, c.id, c.name, done=False)
            else:
                act.note_tool(self.slug, t.context_id, t.task_id, c.id, c.name, done=True, output=str(c.output or "")[:120], error=c.status == "error")
        self._report_park(act, ex)

    def _report_park(self, act, ex: Exchange) -> None:
        t = ex.turn
        if ex is self.convo.latest:  # only the newest exchange speaks for the session's park
            if t.hitl and not t.done:
                act.park(self.slug, t.context_id, deckhitl.prompt_of(t.hitl), task_id=t.task_id, source="deck")
            else:
                act.unpark(self.slug, t.context_id)

    @_ui_safe
    def _render_live(self, ex: Exchange) -> None:
        if not self.is_attached:
            return
        self._report(ex)
        t = ex.turn
        try:
            meta = self.query_one(f"#{ex.widget_id}-meta", Static)
            md = self.query_one(f"#{ex.widget_id}-md", Markdown)
        except Exception:  # noqa: BLE001 — the transcript was rebuilt underneath
            return
        bits = [f"{self.member_name}"]
        if ex.attached and ex.origin:
            bits.append(f"◇ {ex.origin}" + (" · detached, still running on the member" if ex.detached and not t.done else ""))
        if t.reasoning:
            bits.append(f"▸ thinking · {len(t.reasoning):,} chars" if not self.show_reasoning else "▾ thinking")
        n_tools = len(t.top_level_tools())
        if n_tools:
            bits.append(f"⚙ {n_tools} tool call{'s' if n_tools != 1 else ''}")
        if ex.live and not t.done:
            bits.append(f"⟳ {t.status_text or 'working'}")
        elif t.failed or ex.error:
            bits.append(f"✗ {t.failed or ex.error}")
        elif t.hitl:
            bits.append("⚑ waiting for you")
        meta.update("  ".join(bits))
        body = t.content
        if self.show_reasoning and t.reasoning:
            body = f"> {t.reasoning.replace(chr(10), chr(10) + '> ')}\n\n{body}"
        if body != ex.last_body:  # Markdown.update re-parses the whole document — only on change
            ex.last_body = body
            md.update(body)
        if t.consumed_steers:
            ids = {str(s.get("id")) for s in t.consumed_steers}
            for st in self.convo.steers:
                if not st.consumed and st.id in ids:
                    st.consumed = True
                    self._render_steer(st)
        if ex.live:
            sig = tuple((c.id, c.status) for c in t.tool_calls)
            if sig != ex.last_work_sig:  # rebuild the tree on tool frames only
                ex.last_work_sig = sig
                self._render_work(t)
            else:
                self.query_one("#cost", Static).update(_cost_line(t))  # the cost lands on a text frame
            self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)
            self._render_status()

    @_ui_safe
    def _render_work(self, t: a2a.Turn | None) -> None:
        tree = self.query_one("#work-tree", Tree)
        tree.clear()
        head = self.query_one("#work-head", Static)
        if t is None:
            head.update("WORK")
            self.query_one("#cost", Static).update("")
            return
        n = len(t.tool_calls)
        head.update(f"WORK  {n} call{'s' if n != 1 else ''}" + ("  ⟳" if not t.done and self.convo.live and self.convo.live.turn is t else ""))

        def add(parent, c: a2a.ToolCall) -> None:
            label = Text(f"{_glyph(c.status)} {c.name}  ", style={"running": "yellow", "done": "green", "error": "red"}.get(c.status, ""))
            label.append(_preview(c.input, 40), style="dim")
            if c.duration_ms:
                label.append(f"  {c.duration_ms / 1000:.1f}s", style="dim")
            node = parent.add(label, data=c, expand=True)
            for child in t.children_of(c.id):
                add(node, child)

        for c in t.top_level_tools():
            add(tree.root, c)
        self.query_one("#cost", Static).update(_cost_line(t))

    @_ui_safe
    def _render_status(self) -> None:
        live = self.convo.live
        st = self.query_one("#talk-status", Static)
        n_q = len(self.convo.queued)
        queued = f"  ·  {n_q} queued" if n_q else ""
        if live is not None:
            t = live.turn
            if live.cancel_requested:
                st.update("⟳ cancelling…  ·  esc again abandons the turn locally")
            elif live.attached:
                who = f"{live.origin} turn" if live.origin else "attached turn"
                take = "type to interject" if live.controllable else "not taking messages"
                st.update(f"⟳ {who} · {t.status_text or 'working'}  ·  {take}{queued}  ·  esc detaches")
            else:
                st.update(f"⟳ {t.status_text or 'working'}  ·  type to steer{queued}  ·  esc stops")
            return
        parked = self.convo.parked
        if parked is not None:
            # the stream closed on input-required: the turn is PARKED, not over
            if parked.submitting:
                st.update(f"⟳ submitting the form to {self.member_name}…{queued}")
                return
            kind = deckhitl.kind_of(parked.turn.hitl)
            how = "type the answer, or ctrl+r" if kind == "question" else "enter / ctrl+r opens it"
            st.update(f"⚑ {self.member_name} needs you ({kind}): {deckhitl.prompt_of(parked.turn.hitl)}  ·  {how}{queued}")
            return
        latest = self.convo.latest
        st.update("idle" + (f"  ·  last turn {_cost_line(latest.turn)}" if latest and latest.turn.usage else "") + queued)

    # ── keys ──

    def action_cycle(self) -> None:
        order = ["#composer", "#work-tree", "#transcript"]
        focused = self.focused
        idx = next((i for i, sel in enumerate(order) if focused is not None and focused.id == sel[1:]), -1)
        self.query_one(order[(idx + 1) % len(order)]).focus()

    def action_fold(self) -> None:
        self.show_reasoning = not self.show_reasoning
        for ex in self.convo.exchanges:
            self._render_live(ex)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        call = event.node.data
        if isinstance(call, a2a.ToolCall):
            self.app.push_screen(PagerScreen(call))


TALK_CSS = """
#talk-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
#talk-body { height: 1fr; }
#transcript { width: 1fr; padding: 0 1; }
#work { width: 44%; min-width: 28; padding: 0 1; border-left: solid $surface-lighten-2; }
#work-head, #cost { height: 1; color: $text-muted; }
#work-tree { height: 1fr; }
#talk-status { height: 1; padding: 0 1; color: $text-muted; }
#composer { margin: 0 1; }
.user-msg { margin: 1 0 0 0; }
.steer-msg { margin: 1 0 0 0; }
.answer-msg { margin: 1 0 0 0; }
.turn-meta { color: $text-muted; }
.assistant-msg { margin: 0 0 1 0; }
#pager-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
#pager-body { padding: 0 1; }
.pager-label { color: $text-muted; margin: 1 0 0 0; }
.pager-block { }
#picker-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
"""
