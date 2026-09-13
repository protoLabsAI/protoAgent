"""The fleet deck (#3468, epic #3466) — ``protoagent fleet`` with no arguments.

A Textual application over a :class:`deck.data.Backend`: the roster screen (every member
with presence, version skew, spend, lifecycle keys) and a member detail screen (runtime
status, a following log tail, the session inventory, the telemetry rollup). All I/O runs
in thread workers; the UI loop only ever renders a :class:`deck.data.Snapshot` or a
:class:`deck.data.MemberDetail`.

Design rules carried from the proposal: the console's presence words verbatim; the footer
is a status line that lists only the keys valid for the selected row; the layout holds at
80×24; a failed poll keeps the last good roster and says so; a failed pane on the detail
screen degrades alone.

Imported LAZILY by the fleet dispatcher (``importlib``), so ``protoagent --help`` and every
non-interactive verb never import Textual — and a frozen sidecar that does not bundle it
gets a one-line hint instead of a traceback.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, RichLog, Static

from deck import data as deckdata
from deck import hub as deckhub
from deck.data import Backend, MemberDetail, Snapshot, display_name, presence_of, slug_of
from deck.feed import FEED_CSS, Activity, WorkFeedScreen
from deck.talk import TALK_CSS, ConversationScreen
from deck.hitl import HITL_CSS
from deck.hubs import HUBS_CSS, HubRow, HubTreeScreen
from deck.hubs import enumerate_hubs as _enumerate_hubs
from deck.hubs import instance_roots as _instance_roots
from deck.hubs import probe as _probe_hub
from deck.hubs import reconcile as _reconcile_hubs
from deck.hubs import wait_for_port as _wait_for_port
from deck.manage import MANAGE_CSS, DeleteModal, NewAgentModal, RemoteModal, RenameModal

POLL_S = 3.0
LOG_POLL_S = 2.0
_GLYPH = deckdata.PRESENCE_GLYPH


def _rec_key(rec: dict) -> tuple:
    return (str(rec.get("ts") or ""), str(rec.get("logger") or ""), str(rec.get("message") or ""))


def _rec_seq(rec: dict) -> int | None:
    try:
        return int(rec["seq"]) if rec.get("seq") is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_cost(r: deckdata.Rollup | None, pres: str) -> str:
    """Spend for the row. A stopped member is not "unreachable" — it is stopped; the
    telemetry rollup only marks reachability for members it tried to read."""
    if r is None or pres in ("stopped", "unreachable"):
        return "—"
    if not r.reachable:
        return "unreachable"
    if not r.enabled:
        return "off"
    return f"${r.cost_usd:,.2f}"


# ── roster ────────────────────────────────────────────────────────────────────


class RosterScreen(Screen):
    BINDINGS = [
        Binding("enter", "talk", "talk", show=True),
        Binding("c", "talk", "talk", show=False),
        Binding("i", "detail", "detail", show=True),
        Binding("s", "start", "start", show=True),
        Binding("x", "stop", "stop", show=True),
        Binding("r", "restart", "restart", show=True),
        Binding("l", "logs", "logs", show=True),
        Binding("w", "work", "work", show=True),
        Binding("H", "hubs", "hubs", show=True),
        Binding("o", "open_console", "console", show=True),
        Binding("slash", "filter", "filter", show=True, key_display="/"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("f5", "refresh", "refresh", show=False),
        Binding("n", "new_member", "new", show=True),
        Binding("R", "rename", "rename", show=True),
        Binding("d", "delete", "delete", show=True),
        Binding("a", "add_remote", "add remote", show=True),
        Binding("e", "edit_remote", "edit remote", show=True),
        Binding("J", "move_down", "move ↓", show=False),
        Binding("K", "move_up", "move ↑", show=False),
        Binding("question_mark", "help", "help", show=True, key_display="?"),
        Binding("q", "quit", "quit", show=True),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._filter = ""
        self._selected_slug: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("protoagent fleet · connecting…", id="topbar")
        yield Static("", id="banner")
        table: DataTable = DataTable(id="roster", cursor_type="row", zebra_stripes=False)
        table.add_columns("", "MEMBER", "STATE", "TURN", "PORT", "VER", "PID", "SPEND/24H", "LAST ACTIVE", "BUNDLE")
        yield table
        yield Static("", id="status")
        yield Footer()

    # ── rendering ──

    def render_snapshot(self, snap: Snapshot) -> None:
        app: FleetDeck = self.app  # type: ignore[assignment]
        self.query_one("#topbar", Static).update(f"protoagent fleet · {snap.label}")
        banner = self.query_one("#banner", Static)
        if snap.warnings:
            banner.update("⚠ " + "  ·  ".join(snap.warnings))
            banner.display = True
        else:
            banner.update("")
            banner.display = False
        table = self.query_one("#roster", DataTable)
        rows = [a for a in snap.roster if self._matches(a)]
        # Keep the row under the cursor NOW (a `j` whose RowHighlighted has not landed yet
        # would otherwise be undone by a coincident poll); fall back to the last known slug.
        cur = self.selected()
        keep = slug_of(cur) if cur is not None else self._selected_slug
        table.clear()
        self._rows = rows
        seen_keys: set[str] = set()
        for i, a in enumerate(rows):
            # DataTable keys must be unique and non-empty; a malformed roster (missing id,
            # a duplicated id) must not abort the render on the UI thread.
            key = slug_of(a) or f"row-{i}"
            if key in seen_keys:
                key = f"{key}#{i}"
            seen_keys.add(key)
            pres = presence_of(a)
            ver = str(a.get("version") or "")
            ver_cell = Text(f"v{ver}{' !skew' if snap.skewed(a) else ''}" if ver else "—")
            if snap.skewed(a):
                ver_cell.stylize("bold yellow")
            glyph = Text(_GLYPH[pres], style={"host": "green", "online": "green", "remote": "cyan", "stopped": "dim", "unreachable": "red"}[pres])
            pid = str(a.get("pid")) if a.get("pid") and pres in ("online", "host") else "—"
            port = f":{a['port']}" if a.get("port") else "—"
            bundle = str(a.get("bundle") or "")
            if a.get("remote") and a.get("url"):
                bundle = str(a["url"])
            turn = app.activity.turn_cell(slug_of(a)) if pres in ("online", "host", "remote") else ""
            turn_cell = Text(turn, style="bold yellow" if turn.startswith("⚑") else ("yellow" if turn.startswith("⟳") else "dim"))
            # every operator- or member-authored string is a Text, never markup: a label
            # like "Coach [/]" would otherwise raise on the UI thread
            table.add_row(glyph, Text(display_name(a)), pres, turn_cell, port, ver_cell, pid, _fmt_cost(snap.rollups.get(slug_of(a)), pres), app.activity.last_active_cell(slug_of(a)), Text(bundle) if isinstance(bundle, str) else bundle, key=key)
        if rows:
            idx = next((i for i, a in enumerate(rows) if slug_of(a) == keep), 0)
            table.move_cursor(row=idx)
            self._selected_slug = slug_of(rows[idx])
        else:
            self._selected_slug = None
        online = sum(1 for a in snap.roster if presence_of(a) in ("online", "remote"))
        stopped = sum(1 for a in snap.roster if presence_of(a) == "stopped")
        parts = [f"{online} online · {stopped} stopped"]
        parked = [display_name(a) for a in snap.roster if app.activity.turn_cell(slug_of(a)).startswith("⚑")]
        n_parked = sum(len(app.activity.parked_sessions(slug_of(a))) for a in snap.roster)
        if parked:
            parts.append(f"⚑ {n_parked} turn{'s' if n_parked != 1 else ''} parked on a question ({', '.join(parked)})")
        if app.warm_max is not None:
            parts.append(f"warm cap {app.warm_max or 'unlimited'}")
        if snap.mode == "offline":
            parts.append("offline: only start/stop are available — start a hub for the rest")
        if self._filter:
            parts.append(f"filter: {self._filter!r} (esc clears)")
        if snap.error:
            parts.append(f"⚠ last poll failed: {snap.error} (showing the previous roster)")
        self.query_one("#status", Static).update("  ·  ".join(parts))
        app.snapshot = snap
        self.refresh_bindings()

    def _matches(self, a: dict) -> bool:
        if not self._filter:
            return True
        f = self._filter.lower()
        return f in display_name(a).lower() or f in str(a.get("bundle") or "").lower() or f in presence_of(a)

    # ── selection ──

    def selected(self) -> dict | None:
        table = self.query_one("#roster", DataTable)
        if not self._rows or table.cursor_row is None or table.cursor_row >= len(self._rows):
            return None
        return self._rows[table.cursor_row]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        a = self.selected()
        self._selected_slug = slug_of(a) if a else None
        self.refresh_bindings()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # The focused DataTable consumes Enter itself (its select-cursor binding wins over
        # the screen's), so "enter = talk" arrives as this message, not as our action.
        self.action_talk()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Only the keys that apply to the selected row show in the footer."""
        a = self.selected()
        app: FleetDeck = self.app  # type: ignore[assignment]
        offline = app.backend.mode == "offline"
        if action in ("detail", "logs", "open_console", "talk"):
            return bool(a) and not offline
        if action in ("new_member", "add_remote"):
            return not offline  # live mutations go through the hub
        if action in ("rename", "delete", "edit_remote", "move_down", "move_up"):
            # False HIDES the key and Textual never runs the action: the footer is the
            # only refusal these need
            if a is None or offline:
                return False
            if action in ("move_down", "move_up"):
                return not self._filter  # a move under a filter would swap with a hidden neighbour
            if a.get("host"):
                return False  # the hub's name is its identity; it cannot delete itself
            if action == "edit_remote":
                return bool(a.get("remote"))
            return True
        if a is None:
            return action not in ("start", "stop", "restart")
        pres = presence_of(a)
        if action == "start":
            return pres == "stopped"
        if action == "stop":
            return pres == "online"
        if action == "restart":
            return pres == "online" and not offline  # offline: start/stop only, as documented
        return True

    # ── actions ──

    def action_cursor_down(self) -> None:
        self.query_one("#roster", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#roster", DataTable).action_cursor_up()

    def _detail_allowed(self) -> dict | None:
        a = self.selected()
        if a is None:
            return None
        if self.app.backend.mode == "offline":  # type: ignore[attr-defined]
            self.notify("member detail needs a running hub — offline mode can only start and stop", severity="warning")
            return None
        return a

    def action_detail(self) -> None:
        a = self._detail_allowed()
        if a is not None:
            self.app.push_screen(DetailScreen(a))

    def action_talk(self) -> None:
        a = self.selected()
        if a is None:
            return
        if self.app.backend.mode == "offline":  # type: ignore[attr-defined]
            self.notify("offline — no hub to talk to this member through", severity="warning")
            return
        if presence_of(a) not in ("online", "host", "remote"):
            self.notify(f"{display_name(a)} is {presence_of(a)} — start it first (s)", severity="warning")
            return
        self.app.push_screen(ConversationScreen(a))

    def action_logs(self) -> None:
        a = self._detail_allowed()
        if a is not None:
            self.app.push_screen(DetailScreen(a, focus_logs=True))

    def action_start(self) -> None:
        a = self.selected()
        if a is not None:
            self.app.lifecycle("start", a)  # type: ignore[attr-defined]

    def action_stop(self) -> None:
        a = self.selected()
        if a is not None:
            self.app.lifecycle("stop", a)  # type: ignore[attr-defined]

    def action_restart(self) -> None:
        a = self.selected()
        if a is not None and self.app.backend.mode != "offline":  # type: ignore[attr-defined]
            self.app.lifecycle("restart", a)  # type: ignore[attr-defined]

    def action_open_console(self) -> None:
        a = self.selected()
        if a is None:
            return
        href = self.app.backend.console_href(a)  # type: ignore[attr-defined]
        if not href:
            self.notify("no console URL in offline mode", severity="warning")
            return
        self.app.open_in_browser(href)  # type: ignore[attr-defined]

    # ── manage (#3471) ──

    # (`check_action` above hides each key where it does not apply — offline, the host row,
    # a local row for `e` — and Textual then never runs the action, so none of these needs
    # its own refusal)

    def action_new_member(self) -> None:
        self.app.new_member()  # type: ignore[attr-defined]

    def action_rename(self) -> None:
        a = self.selected()
        if a is None or a.get("host"):
            return
        # a remote is renamed through its own record (PATCH /api/fleet/remotes/<id>); the
        # workspace rename route does not know it
        verb = "remote_update" if a.get("remote") else "rename"
        self.app.push_screen(RenameModal(display_name(a)), lambda name: self.app.manage(verb, a, {"name": name}) if name else None)  # type: ignore[attr-defined]

    def action_delete(self) -> None:
        a = self.selected()
        if a is None or a.get("host"):
            return
        remote = bool(a.get("remote"))
        self.app.push_screen(DeleteModal(display_name(a), remote=remote), lambda res: self.app.manage("remote_remove" if remote else "remove", a, res) if res is not None else None)  # type: ignore[attr-defined]

    def action_add_remote(self) -> None:
        self.app.push_screen(RemoteModal(), lambda res: self.app.manage("remote_add", None, res) if res else None)  # type: ignore[attr-defined]

    def action_edit_remote(self) -> None:
        a = self.selected()
        if a is None or not a.get("remote"):
            return
        self.app.push_screen(RemoteModal(a), lambda res: self.app.manage("remote_update", a, res) if res else None)  # type: ignore[attr-defined]

    def _move(self, delta: int) -> None:
        a = self.selected()
        app: FleetDeck = self.app  # type: ignore[assignment]
        if a is None or app.snapshot is None or self._filter:
            return
        roster = app.snapshot.roster
        ids = [str(r.get("id") or slug_of(r)) for r in roster]  # the hub's order, complete, by immutable id (the host's is its own id, not the `host` slug)
        me = str(a.get("id") or slug_of(a))
        if me not in ids:
            return
        i = ids.index(me)
        j = i + delta
        if j < 0 or j >= len(ids):
            return
        # optimistic: the roster shows the move at once and a second press computes from
        # the NEW order, not the order before the hub answered; the post-PUT poll reconciles
        roster[i], roster[j] = roster[j], roster[i]
        ids[i], ids[j] = ids[j], ids[i]
        self.render_snapshot(app.snapshot)
        app.set_order(ids)

    def action_move_down(self) -> None:
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def action_filter(self) -> None:
        self.app.push_screen(FilterScreen(self._filter), self._set_filter)

    def _set_filter(self, value: str | None) -> None:
        if value is None:  # the prompt was cancelled — keep whatever filter was active
            return
        self._filter = value.strip()
        if self.app.snapshot is not None:  # type: ignore[attr-defined]
            self.render_snapshot(self.app.snapshot)  # type: ignore[attr-defined]

    def action_clear_filter(self) -> None:
        if self._filter:
            self._set_filter("")

    def action_refresh(self) -> None:
        self.app.poll()  # type: ignore[attr-defined]

    def action_hubs(self) -> None:
        self.app.open_hubs()  # type: ignore[attr-defined]

    def action_work(self) -> None:
        if self.app.backend.mode == "offline":  # type: ignore[attr-defined]
            self.notify("the work feed needs a running hub", severity="warning")
            return
        self.app.push_screen(WorkFeedScreen())

    def action_help(self) -> None:
        self.app.action_show_help_panel()


class FilterScreen(Screen[str]):
    """A one-line prompt; Enter applies, Esc keeps the previous filter."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=True)]

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        from textual.widgets import Input

        yield Static("filter members by name, bundle, or state (enter applies, esc cancels)", id="filter-label")
        yield Input(value=self._current, placeholder="e.g. coach, stopped, project-manager", id="filter-input")

    def on_mount(self) -> None:
        from textual.widgets import Input

        self.query_one("#filter-input", Input).focus()

    def on_input_submitted(self, event: Any) -> None:
        self.dismiss(str(event.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── member detail ─────────────────────────────────────────────────────────────


class DetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back", show=True),
        Binding("c", "talk", "talk", show=True),
        Binding("x", "stop", "stop", show=True),
        Binding("r", "restart", "restart", show=True),
        Binding("l", "toggle_follow", "pause follow", show=True),
        Binding("o", "open_console", "console", show=True),
        Binding("q", "quit", "quit", show=True),
    ]

    def action_talk(self) -> None:
        a = self._current()
        if presence_of(a) not in ("online", "host", "remote"):
            self.notify(f"{display_name(a)} is {presence_of(a)} — start it first", severity="warning")
            return
        self.app.push_screen(ConversationScreen(a))

    def __init__(self, agent: dict, *, focus_logs: bool = False) -> None:
        super().__init__()
        self.agent = agent
        self.slug = slug_of(agent)
        self.following = True
        self._focus_logs = focus_logs
        self._seen_logs = 0  # size of the last window (for the header)
        self._rendered = 0  # lines written to the pane so far
        self._rendered_any = False
        self._tail: list[tuple] = []  # identity of the last rendered records (fallback anchor)
        self._last_seq: int | None = None  # the exact anchor when the member stamps seq
        self._log_note = ""
        self._timer: Any = None

    def compose(self) -> ComposeResult:
        yield Static(self._head_text(), id="detail-head")
        with Horizontal(id="detail-body"):
            with VerticalScroll(id="detail-left"):
                yield Static("RUNTIME\n  loading…", id="runtime")
                yield Static("SESSIONS", id="sessions-head")
                sessions: DataTable = DataTable(id="sessions", cursor_type="row")
                sessions.add_columns("SESSION", "STATE", "LAST ACTIVITY")
                yield sessions
                yield Static("", id="telemetry")
            with Vertical(id="detail-right"):
                yield Static("LOG  diagnostics/logs  ● following", id="log-head")
                yield RichLog(id="log", highlight=False, markup=False, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_detail()
        self._timer = self.set_interval(LOG_POLL_S, self._tick)
        if self._focus_logs:
            self.query_one("#log", RichLog).focus()
        self._relayout(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._relayout(event.size.width)

    def _relayout(self, width: int) -> None:
        # Below ~100 columns two side-by-side panes leave the sessions table unreadable;
        # stack them instead (80×24 is a supported size).
        self.query_one("#detail-body").set_class(width < 100, "narrow")

    def _current(self) -> dict:
        """The member's CURRENT roster row (the app polls every 3 s), falling back to the
        row captured at push time — so the head and the x/r keys follow a stop/start."""
        snap = self.app.snapshot  # type: ignore[attr-defined]
        if snap is not None:
            for a in snap.roster:
                if slug_of(a) == self.slug:
                    self.agent = a
                    return a
        return self.agent

    def _head_text(self) -> str:
        a = self._current()
        pres = presence_of(a)
        return f"◂ {display_name(a)}   {pres} · :{a.get('port') or '—'}" + (f" · pid {a['pid']}" if a.get("pid") and pres in ("online", "host") else "") + (f" · v{a['version']}" if a.get("version") else "")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("stop", "restart"):
            a = self._current()
            return presence_of(a) == "online" and not a.get("host") and not a.get("remote")
        return True

    def on_roster_update(self) -> None:
        """The app polled the roster: the head and the x/r keys follow the member's
        current state (a stop from this screen shows here within one poll)."""
        try:
            self.query_one("#detail-head", Static).update(self._head_text())
        except Exception:  # noqa: BLE001 — the screen may be mid-teardown
            return
        self.refresh_bindings()

    def _tick(self) -> None:
        if not self.following:
            return
        # A stalled member (three proxied GETs at up to 5 s each) must not accumulate a new
        # thread every 2 s — each stale one would then re-render older data over newer.
        if any(w.group == "detail" and w.is_running for w in self.workers):
            return
        self.refresh_detail()

    @work(thread=True, exclusive=True, group="detail")
    def refresh_detail(self) -> None:
        app: FleetDeck = self.app  # type: ignore[assignment]
        try:
            detail = app.backend.detail(self.agent)
        except Exception:  # noqa: BLE001 — the hub went away under us (an attach closed its client mid-read); the screen is being popped
            return
        snap = app.snapshot
        detail.rollup = snap.rollups.get(self.slug) if snap else None
        app.call_from_thread(self.render_detail, detail)

    def render_detail(self, d: MemberDetail) -> None:
        self.query_one("#detail-head", Static).update(self._head_text())
        self.refresh_bindings()
        rt = d.runtime
        if d.runtime_error:
            runtime_text = f"RUNTIME\n  ⚠ {d.runtime_error}"
        else:
            model = rt.get("model") or {}
            ident = rt.get("identity") or {}
            lines = [
                "RUNTIME",
                f"  model      {model.get('name') or '—'}" + (f" via {model.get('provider')}" if model.get("provider") else ""),
                f"  identity   {ident.get('name') or d.name}" + (f" · {ident.get('operator')}" if ident.get("operator") else ""),
                f"  runtime    {rt.get('agent_runtime') or 'native'} · v{rt.get('version') or '?'}",
                f"  setup      {'complete' if rt.get('setup_complete') else 'incomplete'} · graph {'loaded' if rt.get('graph_loaded') else 'not loaded'}",
            ]
            warns = [deckdata._warning_text(w) for w in (rt.get("warnings") or []) if w]
            lines.append("  warnings   " + ("; ".join(warns) if warns else "none"))
            runtime_text = "\n".join(lines)
        self.query_one("#runtime", Static).update(runtime_text)

        table = self.query_one("#sessions", DataTable)
        table.clear()
        if d.sessions_error:
            self.query_one("#sessions-head", Static).update(f"SESSIONS  ⚠ {d.sessions_error}")
        else:
            self.query_one("#sessions-head", Static).update(f"SESSIONS  newest first · {len(d.sessions)}")
            # GET /api/diagnostics/sessions rows (#3171): session_id, context_id,
            # latest_task_id, latest_task_state, last_activity, status, malformed.
            for s in d.sessions[:50]:
                sid = str(s.get("session_id") or s.get("context_id") or "")
                short = sid if len(sid) <= 26 else f"{sid[:12]}…{sid[-8:]}"
                state = str(s.get("latest_task_state") or s.get("status") or "").replace("TASK_STATE_", "").lower()
                table.add_row(Text(str(short)), Text(str(state)), str(s.get("last_activity") or "")[:16].replace("T", " "))

        r = d.rollup
        if r is None:
            tele = "TELEMETRY  —"
        elif not r.reachable:
            tele = "TELEMETRY  unreachable"
        elif not r.enabled:
            tele = "TELEMETRY  off"
        else:
            tele = f"TELEMETRY 24H  turns {r.turns} · ok {r.success_rate:.0%} · ${r.cost_usd:,.2f} · cache hit {r.cache_hit_ratio:.0%}"
        self.query_one("#telemetry", Static).update(tele)

        log = self.query_one("#log", RichLog)
        head = self.query_one("#log-head", Static)
        if d.logs_error:
            head.update(f"LOG  ⚠ {d.logs_error}")
            return
        # The ring answers the NEWEST N records, so a count can't say what is new once the
        # window is full (review HIGH-1: the tail froze at 200 while the header said
        # "following"). Anchor on the last rendered record and write what follows it:
        # by `seq` when the member stamps one (monotonic, never repeats), else by the
        # identity of the trailing records (a member on an older build). If the anchor
        # has rotated out of the window, re-render the whole window.
        new = self._new_records(d.logs)
        if new is None:
            log.clear()
            self._rendered = 0
            new = d.logs
        self._log_note = d.logs_note
        for rec in new:
            ts = str(rec.get("ts") or "")[11:19]
            lvl = str(rec.get("level") or "")
            line = Text(f"{ts} {lvl:<5} {rec.get('logger') or ''}  {rec.get('message') or ''}")
            if lvl in ("ERROR", "CRITICAL"):
                line.stylize("red")
            elif lvl == "WARNING":
                line.stylize("yellow")
            log.write(line)
            self._rendered += 1
        self._tail = [_rec_key(r) for r in d.logs[-3:]]
        self._last_seq = _rec_seq(d.logs[-1]) if d.logs else None
        self._seen_logs = len(d.logs)
        self._rendered_any = True
        head.update(self._log_head())

    def _new_records(self, logs: list[dict]) -> list[dict] | None:
        """The records after the last rendered one, ``[]`` when nothing is new, or ``None``
        when the anchor is gone (rotated out / first render / window shrank) and the whole
        window must be re-rendered."""
        if not self._rendered_any:
            return None
        if self._last_seq is not None:
            seqs = [_rec_seq(r) for r in logs]
            if logs and all(q is not None for q in seqs):
                # `seq` is a per-PROCESS counter: a member restart (the `r` key on this very
                # screen) restarts it at 1. So the anchor is "our last seq is in the window
                # AND names the same record"; anything else — rotated out, or a restarted
                # counter reusing our number — re-renders the window rather than filtering
                # against a number that no longer means what it did.
                if self._last_seq in seqs:
                    i = seqs.index(self._last_seq)
                    if self._tail and _rec_key(logs[i]) == self._tail[-1]:
                        return logs[i + 1 :]
                return None
        if not self._tail:
            return None
        keys = [_rec_key(r) for r in logs]
        # Match the longest trailing run of rendered records still inside the window,
        # newest occurrence first: a run beats a single (possibly duplicated) record, and a
        # shorter run is tried when the window advanced past part of the longer one. The
        # newest match can under-count an identical burst — invisible to the reader —
        # where the oldest would re-render lines already shown. `seq` makes this exact.
        for k in range(len(self._tail), 0, -1):
            run = self._tail[-k:]
            for end in range(len(keys), k - 1, -1):
                if keys[end - k : end] == run:
                    return logs[end:]
        return None

    def action_back(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.app.pop_screen()

    def action_toggle_follow(self) -> None:
        self.following = not self.following
        self.query_one("#log-head", Static).update(self._log_head())

    def _log_head(self) -> str:
        state = "● following" if self.following else "○ paused"
        return f"LOG  diagnostics/logs  {state}  {self._rendered} shown · window {self._seen_logs}" + (
            f"  ({self._log_note})" if self._log_note else ""
        )

    def action_stop(self) -> None:
        self.app.lifecycle("stop", self.agent)  # type: ignore[attr-defined]

    def action_restart(self) -> None:
        self.app.lifecycle("restart", self.agent)  # type: ignore[attr-defined]

    def action_open_console(self) -> None:
        href = self.app.backend.console_href(self.agent)  # type: ignore[attr-defined]
        if href:
            self.app.open_in_browser(href)  # type: ignore[attr-defined]


# ── the app ───────────────────────────────────────────────────────────────────


class FleetDeck(App[int]):
    TITLE = "protoagent fleet"
    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 1; padding: 0 1; background: $surface; color: $text; text-style: bold; }
    #banner { height: auto; padding: 0 1; background: $warning 20%; color: $warning; }
    #roster { height: 1fr; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #filter-label { padding: 1 1 0 1; }
    #filter-input { margin: 0 1; }
    #detail-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
    #detail-body { height: 1fr; }
    #detail-body.narrow { layout: vertical; }
    #detail-body.narrow #detail-left { width: 1fr; height: auto; max-height: 45%; border-right: none; border-bottom: solid $surface-lighten-2; }
    #detail-body.narrow #detail-right { width: 1fr; height: 1fr; }
    #detail-left { width: 46%; min-width: 30; padding: 0 1; border-right: solid $surface-lighten-2; }
    #detail-right { width: 1fr; padding: 0 1; }
    #runtime, #sessions-head, #telemetry, #log-head { height: auto; margin: 0 0 1 0; }
    #sessions { height: auto; max-height: 12; }
    #log { height: 1fr; }
    """ + TALK_CSS + FEED_CSS + HITL_CSS + MANAGE_CSS + HUBS_CSS

    def __init__(
        self,
        backend: Backend,
        *,
        poll_s: float = POLL_S,
        events: Any = None,
        peers: Any = None,
        launcher: Any = None,
        token: str | None = None,
        insecure_http: bool = False,
        start_on_hubs: bool = False,
        offline: bool = False,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.snapshot: Snapshot | None = None
        self._poll_s = poll_s
        self._order_lock = threading.Lock()  # roster-order writes go out one at a time…
        self._order_seq = 0  # …numbered per press on the UI thread…
        self._order_sent = 0  # …and a press older than the newest on the hub is dropped
        self.warm_max: int | None = None  # the hub's fleet.warm.max, read once (read-only here)
        # the hub tree (#3472): peers come from the CLI (graph.fleet.discovery runs there),
        # so does the launcher that brings a stopped hub up (`protoagent up` for its root)
        self._peers = peers  # Callable[[], list[dict]] | None
        self._launcher = launcher  # Callable[[HubRow], None] | None
        self._token = token  # an explicit --token, for peers and re-attaches
        self._insecure_http = insecure_http
        self._start_on_hubs = start_on_hubs
        self._offline = offline  # `--offline`: the tree lists what disk says and probes nothing
        self._attach_gen = 0  # the operator's LAST attach wins: an earlier, slower one is dropped when it lands
        self._discover_gen = 0  # likewise a rediscover: `exclusive` cancels the task, not the thread, so an older discovery's rows are dropped when they land
        self.hub_rows: list[HubRow] = []
        self.activity = Activity()
        # The fan-in of every online member's event bus (live mode). Injectable for tests.
        self.events = events
        self._events_pending = events is None and backend.mode == "live" and hasattr(backend, "fleet_events")

    def on_mount(self) -> None:
        self.push_screen(RosterScreen())
        if self._start_on_hubs:
            self.push_screen(HubTreeScreen())
        if self._events_pending:
            try:
                self.events = self.backend.fleet_events()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — the roster works without the feed
                self.notify(f"live activity unavailable: {exc}", severity="warning")
        self.poll()
        if self._poll_s > 0:
            self.set_interval(self._poll_s, self.poll)
        self.set_interval(0.5, self.drain_events)
        if self.backend.mode == "live":
            self._read_warm_max()

    @work(thread=True, group="warm")
    def _read_warm_max(self) -> None:
        backend = self.backend
        try:
            v = backend.warm_max()
        except Exception:  # noqa: BLE001 — a footer figure, never fatal
            return
        self.call_from_thread(self._set_warm_max, v, backend)

    def _set_warm_max(self, v: int | None, backend: Any) -> None:
        if backend is not self.backend:
            return  # the deck attached to another hub while this read was out
        self.warm_max = v
        roster = self._roster_screen()
        if roster is not None and self.snapshot is not None:
            roster.render_snapshot(self.snapshot)  # read on the UI thread: a snapshot captured in the worker could be the "attaching" placeholder the first poll has since replaced

    def drain_events(self) -> None:
        """Fold what the member buses sent since the last tick into the activity model;
        re-render the roster's TURN column when anything changed; ring on a new park."""
        if self.events is None:
            return
        evs = self.events.drain()
        for ev in evs:
            self.activity.apply(ev)
        if not evs:
            return
        for scr in self.screen_stack:
            if isinstance(scr, ConversationScreen):
                scr.on_bus_events(evs)
        self._ring_parks()
        roster = self._roster_screen()
        if roster is not None and self.snapshot is not None:
            roster.render_snapshot(self.snapshot)

    def _ring_parks(self) -> None:
        """Announce the parks that appeared since the last drain — and are STILL parked."""
        for slug, _session, prompt in self.activity.ring_due():
            self.bell()
            self.notify(f"{self.activity.names.get(slug, slug)} needs you: {prompt}", severity="warning", timeout=10)

    def attached_count(self) -> int:
        """Conversations currently streaming or attached to a live turn."""
        return sum(1 for scr in self.screen_stack if isinstance(scr, ConversationScreen) and scr.convo.live is not None)

    def open_member(self, slug: str, session_id: str | None = None) -> None:
        """Open a member's conversation (from the work feed): at the given session, or its
        latest. A stopped member cannot be talked to."""
        if self.snapshot is None:
            return
        agent = next((a for a in self.snapshot.roster if slug_of(a) == slug), None)
        if agent is None:
            self.notify(f"{slug} is not in the roster any more", severity="warning")
            return
        if presence_of(agent) not in ("online", "host", "remote"):
            self.notify(f"{display_name(agent)} is {presence_of(agent)}", severity="warning")
            return
        self.push_screen(ConversationScreen(agent, session_id=session_id))

    @work(thread=True, exclusive=True, group="poll")
    def poll(self) -> None:
        backend = self.backend  # the hub this poll is FOR — `exclusive` cancels the task, not the thread
        snap = backend.snapshot()
        if snap.error and self.snapshot is not None:
            # keep the last good roster, surface the failure
            keep = self.snapshot
            snap = Snapshot(mode=keep.mode, label=keep.label, roster=keep.roster, host_version=keep.host_version, rollups=keep.rollups, warnings=keep.warnings, error=snap.error)
        self.call_from_thread(self._apply, snap, backend)

    def _apply(self, snap: Snapshot, backend: Any = None) -> None:
        if backend is not None and backend is not self.backend:
            return  # a poll of the hub the deck just left: its fleet must not paint the new hub's roster
        self.snapshot = snap
        self.activity.names.update({slug_of(a): display_name(a) for a in snap.roster})
        if self.events is not None:
            self.events.watch([slug_of(a) for a in snap.roster if presence_of(a) in ("online", "host", "remote")])
        if snap.parked is not None:
            for slug, seen in snap.parked.items():  # only the members and sessions the probe actually saw
                self.activity.probe(slug, seen, probed_at=snap.parked_at)
            self._ring_parks()
        roster = self._roster_screen()
        if roster is not None:
            roster.render_snapshot(snap)
        if isinstance(self.screen, DetailScreen):
            self.screen.on_roster_update()

    def _roster_screen(self) -> RosterScreen | None:
        for scr in self.screen_stack:
            if isinstance(scr, RosterScreen):
                return scr
        return None

    # ── the hub tree (#3472): every hub on the box, attach, bring up ──

    @property
    def can_bring_up(self) -> bool:
        return self._launcher is not None

    @property
    def bringing_up(self) -> bool:
        return any(w.group == "bring-up" and w.is_running for w in self.workers)

    def _hubs_screen(self) -> HubTreeScreen | None:
        for scr in self.screen_stack:
            if isinstance(scr, HubTreeScreen):
                return scr
        return None

    def open_hubs(self) -> None:
        if self._hubs_screen() is not None:
            return
        discovering = any(w.group == "hubs" and w.is_running for w in self.workers)
        self.push_screen(HubTreeScreen(self.hub_rows, busy=discovering))  # re-opened mid-discovery: still working
        if not self.hub_rows and not discovering:
            self.discover_hubs()

    def discover_hubs(self) -> None:
        scr = self._hubs_screen()
        if scr is not None:
            scr.busy = True
            scr.render_rows()
        self._discover_gen += 1
        self._discover_hubs(self._discover_gen)

    @work(thread=True, exclusive=True, group="hubs")
    def _discover_hubs(self, gen: int) -> None:
        peers: list[dict] = []
        if self._peers is not None:
            try:
                peers = list(self._peers() or [])
            except Exception as exc:  # noqa: BLE001 — the box's own hubs still show
                self.call_from_thread(self.notify, f"peer discovery failed: {exc}", severity="warning", timeout=8)
        rows = _enumerate_hubs(peers=peers)
        rows = self._keep_starting(rows)
        if self._offline:
            # what disk says, unprobed — as `protoagent fleet --all --offline` prints it
            self.call_from_thread(self._show_discovery, gen, [r for r in rows if r.source != "local"], False)
            return
        self.call_from_thread(self._show_discovery, gen, [r for r in rows if r.source != "local"], True)  # a listener by port is shown once it says what it is
        roots = _instance_roots()
        for r in rows:
            if r.presence in ("running", "unreachable") and r.candidate is not None:
                _probe_hub(r, token=self._token, insecure_http=self._insecure_http, roots=roots)
        rows = self._keep_starting(_reconcile_hubs(rows))
        self.call_from_thread(self._show_discovery, gen, rows, False)

    def _show_discovery(self, gen: int, rows: list[HubRow], busy: bool) -> None:
        if gen != self._discover_gen:
            return  # a rediscover superseded this one: its rows (stale peers, stale state words) must not paint over the newer
        self._show_hubs(rows, busy)

    def _keep_starting(self, rows: list[HubRow]) -> list[HubRow]:
        """A rediscover must not replace the row a bring-up holds: the launcher's outcome
        lands on that object, so it stays in the model (by root) until it settles."""
        starting = {r.root: r for r in self.hub_rows if r.presence == "starting" and r.root is not None}
        if not starting:
            return rows
        return [starting.get(r.root, r) if r.root is not None else r for r in rows]

    def _show_hubs(self, rows: list[HubRow], busy: bool) -> None:
        self.hub_rows = rows
        scr = self._hubs_screen()
        if scr is not None:
            scr.rows = rows
            scr.busy = busy
            scr.render_rows()

    def attach_hub(self, row: HubRow) -> None:
        """Point the deck at another hub: a fresh backend and event fan-in over that hub's
        client (its own credential), the activity model reset — the roster then shows that
        fleet. The previous hub's readers are stopped first."""
        if row.candidate is None or row.presence != "running":
            self.notify(f"{row.name} is {row.presence}", severity="warning")
            return
        self.notify(f"attaching to {row.name}…")
        self._attach_gen += 1
        self._attach_hub(row, self._attach_gen)

    @work(thread=True, exclusive=True, group="attach")
    def _attach_hub(self, row: HubRow, gen: int) -> None:
        from deck.data import LiveBackend

        try:
            conn = deckhub.connect(candidates=[row.candidate], token=row.token or self._token, insecure_http=self._insecure_http)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"could not attach to {row.name}: {exc}", severity="error", timeout=10)
            return
        self.call_from_thread(self._switch_backend, LiveBackend(conn), row, gen)

    def _switch_backend(self, backend: Backend, row: HubRow, gen: int | None = None) -> None:
        if gen is not None and gen != self._attach_gen:
            # `exclusive` cancelled the awaiting task, not this thread's work: a slower attach
            # the operator abandoned for another hub lands here after it — that hub is not
            # the deck's; the one they chose last is
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass
            return
        old_events, old_backend = self.events, self.backend
        self.events = None
        self.backend = backend
        self.snapshot = None
        self.activity = Activity()
        self.warm_max = None
        if old_events is not None:
            try:
                old_events.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            old_backend.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.events = backend.fleet_events()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify(f"live activity unavailable: {exc}", severity="warning")
        # back to the roster, whatever was above it
        while not isinstance(self.screen, RosterScreen) and len(self.screen_stack) > 1:
            self.pop_screen()
        roster = self._roster_screen()
        if roster is not None:
            roster.render_snapshot(Snapshot(mode="live", label=f"attaching to {row.name} · {row.url}", roster=[]))
        self.notify(f"attached to {row.name} ({row.url})")
        self.poll()
        self._read_warm_max()

    def bring_up(self, row: HubRow) -> None:
        if self._launcher is None:
            self.notify("bringing a hub up needs the CLI's launcher — run the deck with `protoagent fleet`", severity="warning")
            return
        if row.root is None or row.presence != "stopped":
            return
        row.presence = "starting"
        row.note = f"protoagent up for {row.root}"
        self._show_hubs(self.hub_rows, True)
        self.notify(f"bringing {row.name} up…")
        self._bring_up(row)

    @work(thread=True, group="bring-up")
    def _bring_up(self, row: HubRow) -> None:
        try:
            self._launcher(row)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            row.presence = "stopped"
            row.note = f"could not start: {exc}"
            self.call_from_thread(self._show_hubs, self.hub_rows, False)
            self.call_from_thread(self.notify, f"{row.name}: {exc}", severity="error", timeout=10)
            return
        if not row.url and row.port:
            row.url = deckhub._loopback(row.port)
        if not row.url or not _wait_for_port(row.url):
            row.presence = "stopped"
            row.note = "started, but its port did not answer in time — see its server.log"
            self.call_from_thread(self._show_hubs, self.hub_rows, False)
            self.call_from_thread(self.notify, f"{row.name}: started, but its port did not answer in time — see {row.root}/server.log", severity="error", timeout=10)
            return
        row.candidate = deckhub.HubCandidate(row.url, "pidfile", instance_root=row.root)
        row.presence = "running"
        row.launcher = "protoagent up"  # that is what the launcher ran; the next discovery reads it from the pidfile
        row.note = ""
        _probe_hub(row, token=self._token, insecure_http=self._insecure_http)
        self.call_from_thread(self._show_hubs, self.hub_rows, False)
        if row.presence == "running":
            self.call_from_thread(self.attach_hub, row)

    # ── manage (#3471): mutations by immutable id, through the hub ──

    def new_member(self) -> None:
        self.notify("reading the archetype catalog…")
        self._new_member()

    @work(thread=True, group="manage")
    def _new_member(self) -> None:
        try:
            archetypes = self.backend.archetypes()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"could not read the archetypes: {exc}", severity="error", timeout=8)
            return
        self.call_from_thread(self.push_screen, NewAgentModal(archetypes), lambda body: self.manage("create", None, body) if body else None)

    def manage(self, verb: str, agent: dict | None, payload: dict | None) -> None:
        who = display_name(agent) if agent else str((payload or {}).get("name") or "")
        self.notify(f"{verb.replace('_', ' ')} {who}…")
        self._manage(verb, agent, payload or {})

    @work(thread=True, group="manage")
    def _manage(self, verb: str, agent: dict | None, payload: dict) -> None:
        who = display_name(agent) if agent else str(payload.get("name") or "")
        try:
            if verb == "create":
                res = self.backend.create(payload)
                a = res.get("agent") or {}
                extra = f" (:{a.get('port')}" + (f", pid {a.get('pid')})" if a.get("pid") else ", not started)")
                if res.get("installed"):
                    extra += f" · installed {', '.join(str(x) for x in res['installed'])}"
                if res.get("warnings"):
                    self.call_from_thread(self.notify, "; ".join(str(w) for w in res["warnings"]), severity="warning", timeout=10)
                done = f"created {who}{extra}"
            elif verb == "rename":
                res = self.backend.rename(agent or {}, str(payload.get("name") or ""))
                done = f"renamed to {res.get('name') or payload.get('name')}"
            elif verb == "remove":
                res = self.backend.remove(agent or {}, purge=bool(payload.get("purge")))
                done = f"deleted {who}" + (" — workspace and data purged" if "workspace" in (res.get("removed") or []) else " — data kept")
            elif verb == "remote_add":
                res = self.backend.remote_add(str(payload.get("name") or ""), str(payload.get("url") or ""), str(payload.get("token") or ""))
                done = f"added remote {who}" + (f" · reachable, v{res.get('version')}" if res.get("reachable") else " · unreachable for now — it will show up when it answers")
            elif verb == "remote_update":
                res = self.backend.remote_update(agent or {}, **payload)
                done = f"updated remote {who}" + (" · reachable" if res.get("reachable") else " · unreachable for now")
            elif verb == "remote_remove":
                res = self.backend.remote_remove(agent or {})
                done = f"removed remote {who} (the agent itself is untouched)"
            else:
                return
        except Exception as exc:  # noqa: BLE001 — surfaced as a toast, the deck stays up
            status = getattr(exc, "status", None)
            if verb == "remove" and status == 409:
                # partial, retryable: the member IS stopped, only its workspace survived (#2583)
                self.call_from_thread(self.notify, f"{who}: stopped, but its workspace survived — repeat the delete to finish", severity="warning", timeout=10)
            else:
                detail = getattr(exc, "detail", None) or str(exc)
                self.call_from_thread(self.notify, f"{verb.replace('_', ' ')} {who}: {detail}", severity="error", timeout=10)
            self.call_from_thread(self.poll)
            return
        if res.get("ok", True):
            self.call_from_thread(self.notify, done)
        else:
            self.call_from_thread(self.notify, f"{verb.replace('_', ' ')} {who}: {res.get('reason') or res.get('error') or 'failed'}", severity="error", timeout=8)
        self.call_from_thread(self.poll)

    def set_order(self, ids: list[str]) -> None:
        """Persist the roster order. Presses are numbered on the UI thread and written one
        at a time: a press older than one already on the hub is dropped, so two quick moves
        can never commit in the wrong order and the next poll cannot revert the roster."""
        self._order_seq += 1
        self._set_order(list(ids), self._order_seq)

    @work(thread=True, group="order")
    def _set_order(self, ids: list[str], seq: int) -> None:
        with self._order_lock:
            if seq <= self._order_sent:
                return  # a newer order is already on the hub — the poll shows it
            try:
                res = self.backend.set_order(ids)
            except Exception as exc:  # noqa: BLE001 — surfaced as a toast, the hub's order wins back on the poll
                detail = getattr(exc, "detail", None) or str(exc)
                self.call_from_thread(self.notify, f"set order: {detail}", severity="error", timeout=10)
                self.call_from_thread(self.poll)
                return
            self._order_sent = seq
        if res.get("ok", True):
            self.call_from_thread(self.notify, "order saved")
        else:
            self.call_from_thread(self.notify, f"set order: {res.get('reason') or res.get('error') or 'failed'}", severity="error", timeout=8)
        self.call_from_thread(self.poll)

    def lifecycle(self, verb: str, agent: dict) -> None:
        if agent.get("host"):
            self.notify("the hub cannot stop or restart itself from the deck — use `protoagent down`", severity="warning")
            return
        if agent.get("remote"):
            self.notify("a remote member is registered, not spawned — it cannot be started or stopped from here", severity="warning")
            return
        self.notify(f"{verb} {display_name(agent)}…")
        self._lifecycle(verb, agent)

    @work(thread=True, group="lifecycle")
    def _lifecycle(self, verb: str, agent: dict) -> None:
        name = str(agent.get("name") or slug_of(agent))
        try:
            if verb == "start":
                res = self.backend.start(name)
            elif verb == "stop":
                res = self.backend.stop(name)
            else:
                res = self.backend.stop(name)
                if res.get("ok", res.get("stopped", True)):
                    res = self.backend.start(name)
        except Exception as exc:  # noqa: BLE001 — surfaced as a toast, the deck stays up
            self.call_from_thread(self.notify, f"{verb} {name}: {exc}", severity="error", timeout=8)
            return
        ok = bool(res.get("ok", res.get("stopped", True)))
        if ok:
            self.call_from_thread(self.notify, f"{verb} {name}: done")
        else:
            self.call_from_thread(self.notify, f"{verb} {name}: {res.get('reason') or res.get('error') or 'failed'}", severity="error", timeout=8)
        self.call_from_thread(self.poll)  # a @work method belongs to the app thread

    @work(thread=True, group="browser")
    def open_in_browser(self, href: str) -> None:
        # webbrowser.open blocks on macOS (it waits for osascript) — keep it off the UI loop.
        try:
            webbrowser.open(href)
        except Exception as exc:  # noqa: BLE001 — a browser hand-off must never crash the deck
            self.call_from_thread(self.notify, f"could not open a browser: {exc}", severity="error")
            return
        self.call_from_thread(self.notify, f"opened {href}")

    def action_quit(self) -> None:
        self.exit(0)

    def on_unmount(self) -> None:
        # Whatever ends the app (q, ctrl+q, an exception in run_test), every reader thread
        # is stopped and the hub client is closed.
        if self.events is not None:
            try:
                self.events.close()
            except Exception:  # noqa: BLE001
                pass
        self.backend.close()


def run(backend: Backend, **kw: Any) -> int:
    """Run the deck to completion and return an exit code. A fatal error inside the app
    (Textual sets ``return_code``) must not read as a clean exit. ``kw`` are
    :class:`FleetDeck`'s keyword options (peers / launcher / token / start_on_hubs…)."""
    app = FleetDeck(backend, **kw)
    code = app.run()
    if app.return_code:
        return int(app.return_code)
    return int(code or 0)
