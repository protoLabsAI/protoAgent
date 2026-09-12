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
from deck.data import Backend, MemberDetail, Snapshot, display_name, presence_of, slug_of

POLL_S = 3.0
LOG_POLL_S = 2.0
_GLYPH = deckdata.PRESENCE_GLYPH


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
        Binding("enter", "detail", "detail", show=True),
        Binding("i", "detail", "detail", show=False),
        Binding("s", "start", "start", show=True),
        Binding("x", "stop", "stop", show=True),
        Binding("r", "restart", "restart", show=True),
        Binding("l", "logs", "logs", show=True),
        Binding("o", "open_console", "console", show=True),
        Binding("slash", "filter", "filter", show=True, key_display="/"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("R", "refresh", "refresh", show=False),
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
        table.add_columns("", "MEMBER", "STATE", "PORT", "VER", "PID", "SPEND/24H", "BUNDLE")
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
        keep = self._selected_slug
        table.clear()
        self._rows = rows
        for a in rows:
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
            table.add_row(glyph, display_name(a), pres, port, ver_cell, pid, _fmt_cost(snap.rollups.get(slug_of(a)), pres), bundle, key=slug_of(a))
        if rows:
            idx = next((i for i, a in enumerate(rows) if slug_of(a) == keep), 0)
            table.move_cursor(row=idx)
            self._selected_slug = slug_of(rows[idx])
        else:
            self._selected_slug = None
        online = sum(1 for a in snap.roster if presence_of(a) in ("online", "remote"))
        stopped = sum(1 for a in snap.roster if presence_of(a) == "stopped")
        parts = [f"{online} online · {stopped} stopped"]
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
        # the screen's), so "enter = detail" arrives as this message, not as our action.
        self.action_detail()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Only the keys that apply to the selected row show in the footer."""
        a = self.selected()
        app: FleetDeck = self.app  # type: ignore[assignment]
        offline = app.backend.mode == "offline"
        if action in ("detail", "logs", "open_console"):
            return bool(a) and not offline
        if a is None:
            return action not in ("start", "stop", "restart")
        pres = presence_of(a)
        if action == "start":
            return pres == "stopped"
        if action == "stop":
            return pres == "online"
        if action == "restart":
            return pres == "online"
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
        if a is not None:
            self.app.lifecycle("restart", a)  # type: ignore[attr-defined]

    def action_open_console(self) -> None:
        a = self.selected()
        if a is None:
            return
        href = self.app.backend.console_href(a)  # type: ignore[attr-defined]
        if not href:
            self.notify("no console URL in offline mode", severity="warning")
            return
        try:
            webbrowser.open(href)
            self.notify(f"opened {href}")
        except Exception as exc:  # noqa: BLE001 — a browser hand-off must never crash the deck
            self.notify(f"could not open a browser: {exc}", severity="error")

    def action_filter(self) -> None:
        self.app.push_screen(FilterScreen(self._filter), self._set_filter)

    def _set_filter(self, value: str | None) -> None:
        self._filter = (value or "").strip()
        if self.app.snapshot is not None:  # type: ignore[attr-defined]
            self.render_snapshot(self.app.snapshot)  # type: ignore[attr-defined]

    def action_clear_filter(self) -> None:
        if self._filter:
            self._set_filter("")

    def action_refresh(self) -> None:
        self.app.poll()  # type: ignore[attr-defined]

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
        Binding("x", "stop", "stop", show=True),
        Binding("r", "restart", "restart", show=True),
        Binding("l", "toggle_follow", "pause follow", show=True),
        Binding("o", "open_console", "console", show=True),
        Binding("q", "quit", "quit", show=True),
    ]

    def __init__(self, agent: dict, *, focus_logs: bool = False) -> None:
        super().__init__()
        self.agent = agent
        self.slug = slug_of(agent)
        self.following = True
        self._focus_logs = focus_logs
        self._seen_logs = 0
        self._log_note = ""
        self._timer: Any = None

    def compose(self) -> ComposeResult:
        a = self.agent
        pres = presence_of(a)
        head = f"◂ {display_name(a)}   {pres} · :{a.get('port') or '—'}" + (f" · pid {a['pid']}" if a.get("pid") else "") + (f" · v{a['version']}" if a.get("version") else "")
        yield Static(head, id="detail-head")
        with Horizontal(id="detail-body"):
            with VerticalScroll(id="detail-left"):
                yield Static("RUNTIME\n  loading…", id="runtime")
                yield Static("SESSIONS", id="sessions-head")
                sessions: DataTable = DataTable(id="sessions", cursor_type="row")
                sessions.add_columns("SESSION", "STATE", "TURNS", "UPDATED")
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

    def _tick(self) -> None:
        if self.following:
            self.refresh_detail()

    @work(thread=True, exclusive=True, group="detail")
    def refresh_detail(self) -> None:
        app: FleetDeck = self.app  # type: ignore[assignment]
        detail = app.backend.detail(self.agent)
        snap = app.snapshot
        detail.rollup = snap.rollups.get(self.slug) if snap else None
        app.call_from_thread(self.render_detail, detail)

    def render_detail(self, d: MemberDetail) -> None:
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
            for s in d.sessions[:50]:
                sid = str(s.get("context_id") or s.get("session_id") or "")
                short = sid if len(sid) <= 24 else f"{sid[:10]}…{sid[-8:]}"
                table.add_row(short, str(s.get("state") or s.get("last_state") or ""), str(s.get("task_count") or s.get("turn_count") or ""), str(s.get("last_updated") or "")[:16])

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
        # The ring returns the newest N; only write lines we have not shown yet.
        new = d.logs[self._seen_logs :] if len(d.logs) >= self._seen_logs else d.logs
        if len(d.logs) < self._seen_logs:
            log.clear()
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
        self._seen_logs = len(d.logs)
        head.update(self._log_head())

    def action_back(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.app.pop_screen()

    def action_toggle_follow(self) -> None:
        self.following = not self.following
        self.query_one("#log-head", Static).update(self._log_head())

    def _log_head(self) -> str:
        state = "● following" if self.following else "○ paused"
        return f"LOG  diagnostics/logs  {state}  {self._seen_logs} lines" + (f"  ({self._log_note})" if self._log_note else "")

    def action_stop(self) -> None:
        self.app.lifecycle("stop", self.agent)  # type: ignore[attr-defined]

    def action_restart(self) -> None:
        self.app.lifecycle("restart", self.agent)  # type: ignore[attr-defined]

    def action_open_console(self) -> None:
        href = self.app.backend.console_href(self.agent)  # type: ignore[attr-defined]
        if href:
            webbrowser.open(href)
            self.notify(f"opened {href}")


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
    #detail-left { width: 46%; min-width: 30; padding: 0 1; border-right: solid $surface-lighten-2; }
    #detail-right { width: 1fr; padding: 0 1; }
    #runtime, #sessions-head, #telemetry, #log-head { height: auto; margin: 0 0 1 0; }
    #sessions { height: auto; max-height: 12; }
    #log { height: 1fr; }
    """

    def __init__(self, backend: Backend, *, poll_s: float = POLL_S) -> None:
        super().__init__()
        self.backend = backend
        self.snapshot: Snapshot | None = None
        self._poll_s = poll_s

    def on_mount(self) -> None:
        self.push_screen(RosterScreen())
        self.poll()
        if self._poll_s > 0:
            self.set_interval(self._poll_s, self.poll)

    @work(thread=True, exclusive=True, group="poll")
    def poll(self) -> None:
        snap = self.backend.snapshot()
        if snap.error and self.snapshot is not None:
            # keep the last good roster, surface the failure
            keep = self.snapshot
            snap = Snapshot(mode=keep.mode, label=keep.label, roster=keep.roster, host_version=keep.host_version, rollups=keep.rollups, warnings=keep.warnings, error=snap.error)
        self.call_from_thread(self._apply, snap)

    def _apply(self, snap: Snapshot) -> None:
        self.snapshot = snap
        roster = self._roster_screen()
        if roster is not None:
            roster.render_snapshot(snap)

    def _roster_screen(self) -> RosterScreen | None:
        for scr in self.screen_stack:
            if isinstance(scr, RosterScreen):
                return scr
        return None

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
        self.poll()

    def action_quit(self) -> None:
        self.exit(0)

    def on_unmount(self) -> None:
        # Whatever ends the app (q, ctrl+q, an exception in run_test), the hub client is closed.
        self.backend.close()


def run(backend: Backend) -> int:
    """Run the deck to completion and return an exit code."""
    app = FleetDeck(backend)
    code = app.run()
    return int(code or 0)
