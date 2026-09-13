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

A parked question (``input-required``) is surfaced — the prompt in the status line and a
toast — but answering it, steering a running turn, and cancelling one delegation are S3
(#3470).
"""

from __future__ import annotations

import json
import threading
import time
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


@dataclass
class Conversation:
    """The screen's model: a session and its exchanges, newest last."""

    session_id: str
    exchanges: list[Exchange] = field(default_factory=list)

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
        self.convo = Conversation(session_id=session_id, exchanges=exchanges)
        self._rebuild_transcript()
        self._render_work(self.convo.latest.turn if self.convo.latest else None)
        self._render_status()
        self._render_head()

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
        if not text:
            return
        if self.convo.live:
            # Steering a running turn is S3 (#3470); until then a second message waits.
            self.notify("a turn is running — wait for it, or esc to stop (steering arrives in S3)", severity="warning")
            return
        if self.app.backend.mode == "offline":  # type: ignore[attr-defined]
            self.notify("offline — no hub to talk to this member through", severity="error")
            return
        if self._loading():
            self.notify("still loading this session — one moment", severity="warning")
            return
        event.input.value = ""
        turn = a2a.Turn(context_id=self.convo.session_id)
        ex = Exchange(user=text, turn=turn, live=True)
        self.convo.exchanges.append(ex)
        self._append_exchange(ex)
        self._render_work(turn)
        self._render_status()
        self._render_head()
        self._stream(text, ex)

    @work(thread=True, group="talk-stream")
    def _stream(self, text: str, ex: Exchange) -> None:
        app = self.app
        try:
            client = app.backend.a2a(self.agent)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(self._finish, ex, str(exc))
            return
        ex.client = client
        frames = client.stream(text, context_id=self.convo.session_id)
        try:
            for attempt in range(MAX_RECONNECTS + 1):
                try:
                    for frame in frames:
                        with ex.lock:
                            if ex.turn.done:
                                break  # the stall probe already finalized this turn
                            try:
                                a2a.apply_frame(ex.turn, frame)
                            except a2a.TurnError as exc:
                                app.call_from_thread(self._finish, ex, str(exc))
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

    def _finalize_from(self, ex: Exchange, task: dict, app) -> None:
        """From a worker: the server finished but the stream tail was lost — finalize
        the exchange from the durable task. Never fabricates a completion."""
        with ex.lock:
            try:
                a2a.apply_frame(ex.turn, {"result": {"task": task}})
            except a2a.TurnError:
                pass
            ex.turn.done = True
        app.call_from_thread(self._finish, ex, "")
        app.call_from_thread(self.notify, "stream stalled — finalized from the durable task", severity="warning")

    def _finish(self, ex: Exchange, error: str) -> None:
        # State first, always — even after the screen was popped (abandon / quit), so
        # `convo.live` can never stay truthy on an exchange whose reader has unwound.
        ex.live = False
        if error and not ex.turn.done:
            ex.error = error
        act = getattr(self.app, "activity", None) if self.is_attached else None
        if act is not None:
            act.note_live(self.slug, ex.turn.task_id, False)
        self._finish_render(ex, error)

    @_ui_safe
    def _finish_render(self, ex: Exchange, error: str) -> None:
        if ex.error and error:
            self.notify(f"turn failed: {error}", severity="error", timeout=8)
        self._render_live(ex)
        self._render_work(ex.turn)  # the final state, whatever the last frame carried
        self._render_status()
        self._render_head()

    def _check_stall(self) -> None:
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
        # The server finished but the stream tail was lost: unblock the reader FIRST (it
        # exits through its except path, finding the turn already done), then finalize
        # from the durable task under the exchange lock.
        ex.client.abort()
        self._finalize_from(ex, task, app)

    def action_esc(self) -> None:
        ex = self.convo.live
        if ex is not None:
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

    @_ui_safe
    def _append_exchange(self, ex: Exchange) -> None:
        tr = self.query_one("#transcript", VerticalScroll)
        self._seq += 1
        ex.widget_id = f"ex-{self._seq}"
        tr.mount(Static(Text(f"you  {ex.user}", style="bold"), classes="user-msg"))
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
        if t.hitl and not t.done:
            act.set_parked(self.slug, str(t.hitl.get("question") or t.hitl.get("title") or "input required"))
        elif t.done:
            act.set_parked(self.slug, "")

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
        if live is not None:
            t = live.turn
            if t.hitl:
                q = t.hitl.get("question") or t.hitl.get("title") or "input required"
                st.update(f"⚑ {self.member_name} needs you: {q}  (answering arrives in S3 — use the console for now)")
            elif live.cancel_requested:
                st.update("⟳ cancelling…  ·  esc again abandons the turn locally")
            else:
                st.update(f"⟳ {t.status_text or 'working'}  ·  esc stops")
        else:
            latest = self.convo.latest
            if latest is not None and latest.turn.hitl and not latest.turn.done:
                # the stream closed on input-required: the turn is PARKED, not over
                q = latest.turn.hitl.get("question") or latest.turn.hitl.get("title") or "input required"
                st.update(f"⚑ {self.member_name} needs you: {q}  (answering arrives in S3 — use the console for now)")
                return
            st.update("idle" + (f"  ·  last turn {_cost_line(latest.turn)}" if latest and latest.turn.usage else ""))

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
.turn-meta { color: $text-muted; }
.assistant-msg { margin: 0 0 1 0; }
#pager-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
#pager-body { padding: 0 1; }
.pager-label { color: $text-muted; margin: 1 0 0 0; }
.pager-block { }
#picker-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
"""
