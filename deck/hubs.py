"""The hub tree (#3472): every hub on the box as a screen — attach, or bring a stopped hub up.

The discovery and probing this screen renders live in ``deck.discovery`` (Textual-free, shared
with ``protoagent fleet --all``); this module only adds the Textual screen. The names below are
re-exported so callers and tests may keep reaching them as ``deck.hubs.<name>``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static
from deck.discovery import (  # noqa: F401 — re-exported for callers and tests
    HubRow,
    PRESENCE_GLYPH,
    _classify_failure,
    _is_loose_box_root,
    _ports_on_disk,
    count_members,
    enumerate_hubs,
    instance_roots,
    launcher_of,
    plain,
    probe,
    reconcile,
    row_text,
    wait_for_port,
)

__all__ = ["HubRow", "PRESENCE_GLYPH", "count_members", "enumerate_hubs", "instance_roots", "launcher_of", "plain", "probe", "reconcile", "row_text", "wait_for_port", "HubTreeScreen", "HUBS_CSS"]  # the public surface; the underscore helpers above stay reachable as attributes

class HubTreeScreen(Screen):
    """Every hub on the box, one row each; ``enter`` attaches the deck to that hub,
    ``u`` brings a stopped one up, ``r`` re-discovers."""

    BINDINGS = [
        Binding("escape", "back", "back", show=True),
        Binding("enter", "attach", "attach", show=True),
        Binding("u", "bring_up", "bring up", show=True),
        Binding("r", "refresh", "rediscover", show=True),
        Binding("q", "quit", "quit", show=False),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
    ]

    def __init__(self, rows: list[HubRow] | None = None, *, busy: bool = False) -> None:
        super().__init__()
        self.rows: list[HubRow] = list(rows or [])
        self.busy = busy  # a discovery still probing (the screen was re-opened mid-way)

    def compose(self) -> ComposeResult:
        yield Static("hubs on this box · discovering…", id="hubs-head")
        table: DataTable = DataTable(id="hubs", cursor_type="row")
        table.add_columns("", "HUB", "STATE", "LAUNCHER", "PORT", "VER", "MEMBERS", "NOTE / ROOT")
        yield table
        yield Static("enter attaches the deck to a running hub · u brings a stopped hub up · r rediscovers", id="hubs-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hubs", DataTable).focus()
        self.render_rows()
        if not self.rows:
            self.action_refresh()

    def render_rows(self) -> None:
        try:
            table = self.query_one("#hubs", DataTable)
        except NoMatches:
            return  # a discovery result landing before the screen composed: on_mount renders
        cur = table.cursor_row
        table.clear()
        for i, r in enumerate(self.rows):
            glyph = Text(PRESENCE_GLYPH.get(r.presence, "·"), style={"running": "green", "unauthorized": "yellow", "unreachable": "red", "starting": "yellow"}.get(r.presence, "dim"))
            members, note = row_text(r)
            # every str cell is a Text: a plain str is parsed for console markup, and the
            # name, version and note come from the hub (a peer's are self-reported)
            table.add_row(glyph, Text(r.name), Text(r.presence), Text(r.launcher), Text(str(r.port or "—")), Text(r.version or "—"), Text(members), Text(note, style="yellow" if r.note else "dim"), key=f"{r.key}#{i}")
        n_run = sum(1 for r in self.rows if r.presence == "running")
        self.query_one("#hubs-head", Static).update(f"hubs on this box · {len(self.rows)} found · {n_run} running" + (" · working…" if self.busy else ""))
        if self.rows and cur is not None and cur < len(self.rows):
            table.move_cursor(row=cur)
        self.refresh_bindings()  # the footer offers attach / bring up for THIS row, and only then

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.refresh_bindings()

    def selected(self) -> HubRow | None:
        table = self.query_one("#hubs", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        r = self.selected()
        if action == "attach":
            return r is not None and r.presence == "running"
        if action == "bring_up":
            return r is not None and r.presence == "stopped" and r.root is not None and self.app.can_bring_up  # type: ignore[attr-defined]
        if action == "refresh":
            return not self.app.bringing_up  # type: ignore[attr-defined]  # a rediscover mid-launch would lose the row being brought up
        return True

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_attach()

    def action_attach(self) -> None:
        r = self.selected()
        if r is None or r.presence != "running":
            return
        self.app.attach_hub(r)  # type: ignore[attr-defined]

    def action_bring_up(self) -> None:
        r = self.selected()
        if r is None or r.presence != "stopped" or r.root is None:
            return
        self.app.bring_up(r)  # type: ignore[attr-defined]

    def action_refresh(self) -> None:
        self.app.discover_hubs()  # type: ignore[attr-defined]

    def action_down(self) -> None:
        self.query_one("#hubs", DataTable).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#hubs", DataTable).action_cursor_up()

    def action_back(self) -> None:
        self.app.pop_screen()


HUBS_CSS = """
#hubs-head { height: 1; padding: 0 1; text-style: bold; background: $surface; }
#hubs { height: 1fr; }
#hubs-status { height: 1; padding: 0 1; color: $text-muted; }
"""


Launcher = Callable[[HubRow], Any]
