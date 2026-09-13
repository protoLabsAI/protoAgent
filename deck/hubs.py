"""Every hub on the box (#3472): discovery, the hub tree, bringing a stopped hub up.

The console is per-hub by design. One machine runs several hubs at once — the desktop
app's, ``~/.protoagent/dev``, other scoped instances — each with its own fleet, plus
peers on the LAN or the tailnet. This module finds them all and says what each one is:

- **running** hubs from the ``.instances/<pid>.json`` heartbeats under every known box
  root (``deck.hub.discover_hubs``: heartbeats of MEMBERS are skipped — a member is a row
  under its hub, never a hub row);
- **stopped** hubs from every instance root that carries ``workspaces/fleet.json``: each
  known box root itself, and each of its child instance roots (``~/.protoagent/<name>``);
  a root with ``workspace.yaml`` is a member's and is skipped;
- **peers** handed in by the caller (``graph.fleet.discovery.discover`` runs in the CLI —
  this package never imports ``graph``).

Nothing here depends on the shell's ``PROTOAGENT_*`` environment beyond adding this
shell's own instance root to the search (the S0 finding: what a shell "sees" must not be
what it inherited). A running hub is probed for its version and member counts with that
hub's OWN fleet token (``<root>/workspaces/.fleet-token``); one that answers but refuses
every credential reads ``unauthorized``, one that does not answer ``unreachable`` — two
different problems, two words. Bringing a hub up runs ``protoagent up`` for THAT instance
root (an injected launcher — the CLI supplies it); stopping a hub is not a deck action.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from deck import hub as deckhub

FLEET_JSON = "fleet.json"
REMOTES_JSON = "remotes.json"
PRESENCE_GLYPH = {"running": "●", "unauthorized": "◐", "unreachable": "◌", "stopped": "○", "starting": "◍"}


@dataclass
class HubRow:
    """One hub as the tree shows it."""

    name: str  # identity when known, else the instance root's name
    root: Path | None  # instance root on this box; None for a peer
    url: str | None  # where it answers (or would)
    port: int | None
    presence: str  # running | unauthorized | unreachable | stopped | starting
    launcher: str = ""  # desktop app | protoagent up | foreground | peer | ""
    version: str = ""
    pid: int | None = None
    source: str = ""  # heartbeat | pidfile | root | peer | flag
    members: int | None = None  # local members (workspaces); None when unknown
    running: int | None = None  # of which running; None when the hub could not be read
    remotes: int | None = None
    note: str = ""  # a conflict or an error, one line
    candidate: deckhub.HubCandidate | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)  # the credential that opened it (never rendered)
    seen_root: Path | None = field(default=None, repr=False)  # the instance root a probed listener said it runs from
    drop: bool = field(default=False, repr=False)  # a listener that is not a hub row (a member, a non-hub service)

    @property
    def key(self) -> str:
        return str(self.root) if self.root is not None else (self.url or self.name)


def _resolve(p: Path | None) -> Path | None:
    if p is None:
        return None
    try:
        return p.expanduser().resolve()
    except OSError:
        return p


def _is_hub_root(root: Path) -> bool:
    try:
        return (root / "workspaces" / FLEET_JSON).is_file() and not deckhub.is_member_root(root)
    except OSError:
        return False


def _server_pid_record(root: Path) -> dict:
    try:
        rec = json.loads((root / "server.pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def count_members(root: Path) -> tuple[int, int, int]:
    """``(local, running, remotes)`` from the hub's own files, read-only: one member per
    ``workspaces/<id>/workspace.yaml``, running ones from ``fleet.json`` (pids the OS
    still has), remotes from ``remotes.json``."""
    ws = root / "workspaces"
    local = 0
    try:
        for d in ws.iterdir():
            if d.is_dir() and (d / deckhub.WORKSPACE_MARKER).is_file():
                local += 1
    except OSError:
        pass
    running = 0
    try:
        fleet = json.loads((ws / FLEET_JSON).read_text(encoding="utf-8"))
        if isinstance(fleet, dict):
            for rec in fleet.values():
                if isinstance(rec, dict):
                    try:
                        if deckhub.pid_alive(int(rec.get("pid") or 0)):
                            running += 1
                    except (TypeError, ValueError):
                        pass
    except (OSError, ValueError):
        pass
    remotes = 0
    try:
        rem = json.loads((ws / REMOTES_JSON).read_text(encoding="utf-8"))
        if isinstance(rem, dict):
            remotes = len(rem)
    except (OSError, ValueError):
        pass
    return local, running, remotes


def launcher_of(root: Path | None, pid: int | None) -> str:
    """How a hub was started: the desktop app owns its box root; ``protoagent up`` leaves a
    ``server.pid`` naming the pid; anything else with a heartbeat is a foreground server."""
    if root is None:
        return "peer"
    rroot = _resolve(root)
    for d in deckhub.desktop_box_roots():
        if rroot == _resolve(d):
            return "desktop app"
    rec = _server_pid_record(root)
    try:
        if pid is not None and int(rec.get("pid") or 0) == pid:
            return "protoagent up"
    except (TypeError, ValueError):
        pass
    return "foreground" if pid is not None else ""


def _root_name(root: Path) -> str:
    """The data home is the default instance; everything else is named by its directory."""
    try:
        if _resolve(root) == _resolve(deckhub.data_home()):
            return "default"
    except OSError:
        pass
    return root.name


def instance_roots() -> list[Path]:
    """Every instance root on this box that is (or was) a hub: each known box root and
    the plain data home themselves, plus their child instance roots — existing, deduped."""
    bases: list[Path] = []
    for b in [*deckhub.known_box_roots(), deckhub.data_home(), deckhub.instance_paths().instance_root]:
        rb = _resolve(b)
        if rb is not None and rb.is_dir() and rb not in bases:
            bases.append(rb)
    out: list[Path] = []
    for b in bases:
        cands = [b]
        try:
            cands.extend(sorted(p for p in b.iterdir() if p.is_dir() and not p.name.startswith(".")))
        except OSError:
            pass
        for c in cands:
            rc = _resolve(c)
            if rc is not None and rc not in out and _is_hub_root(rc):
                out.append(rc)
    return out


def enumerate_hubs(*, peers: list[dict] | None = None) -> list[HubRow]:
    """The hub tree, unprobed: running hubs (heartbeats / this shell's pidfile), stopped
    hubs (instance roots with a fleet), then peers. Ports are box-global: two rows
    claiming one port both say so."""
    rows: list[HubRow] = []
    seen_roots: set[Path] = set()
    seen_urls: set[str] = set()
    for c in deckhub.discover_hubs():
        if c.source == "default":
            continue  # a guess, not evidence — the probe of the default port belongs to `connect`
        root = _resolve(c.instance_root)
        if root is not None:
            seen_roots.add(root)
        seen_urls.add(c.url)
        port = None
        try:
            port = int(c.url.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            pass
        local, running, remotes = count_members(root) if root is not None and _is_hub_root(root) else (None, None, None)
        rows.append(HubRow(name=c.identity or (root.name if root is not None else f"hub :{port}"), root=root, url=c.url, port=port, presence="running", launcher=launcher_of(root, c.pid), pid=c.pid, source=c.source, members=local, running=running, remotes=remotes, candidate=c))
    for root in instance_roots():
        if root in seen_roots:
            continue
        rec = _server_pid_record(root)
        port = None
        try:
            port = int(rec.get("port") or 0) or None
        except (TypeError, ValueError):
            pass
        local, running, remotes = count_members(root)
        rows.append(HubRow(name=_root_name(root), root=root, url=deckhub._loopback(port) if port else None, port=port, presence="stopped", launcher="", version=str(rec.get("version") or ""), source="root", members=local, running=running, remotes=remotes))
    for p in peers or []:
        if not isinstance(p, dict) or not p.get("url"):
            continue
        try:
            url = deckhub.normalize_url(str(p["url"]))
        except ValueError:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            port = int(p.get("port") or url.rsplit(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            port = None
        local = deckhub.is_loopback(url)
        # a listener on THIS box found by port (the desktop hub whose heartbeat is missing,
        # a foreground server) is a hub row only once it says which root it runs from —
        # `reconcile` folds it into that root's row; a member answers as a fleet of itself
        # and is dropped there
        rows.append(HubRow(name=str(p.get("name") or p.get("host") or url), root=None, url=url, port=port, presence="unreachable", launcher="" if local else "peer", source="local" if local else "peer", candidate=deckhub.HubCandidate(url, "peer")))
    by_port: dict[int, list[HubRow]] = {}
    for r in rows:
        if r.port and r.root is not None:
            by_port.setdefault(r.port, []).append(r)
    for port, group in by_port.items():
        if len(group) > 1:
            for r in group:
                others = ", ".join(o.name for o in group if o is not r)
                r.note = f"port {port} also claimed by {others}"
    return rows


def probe(row: HubRow, *, token: str | None = None, insecure_http: bool = False) -> HubRow:
    """Ask a running hub or a peer what it is: version and member counts through its own
    fleet token (a peer only through ``token``). Sets ``presence`` to ``running`` /
    ``unauthorized`` / ``unreachable`` and keeps the connection's credential for a later
    attach. Never raises."""
    if row.candidate is None:
        return row
    try:
        conn = deckhub.connect(candidates=[row.candidate], token=token, insecure_http=insecure_http)
    except deckhub.NoHub as exc:
        if exc.unauthorized:
            row.presence = "unauthorized"
            row.note = row.note or "answers, but every credential was refused — pass --token"
        elif exc.failed:
            # answered, but not as a hub (no fleet route: another service on the port, an
            # older protoAgent): nothing to attach to
            row.presence = "unreachable"
            row.note = row.note or f"not a hub: {next(iter(exc.failed.values()), '')}"
            row.drop = row.source in ("local", "peer")
        elif exc.members:
            row.presence = "running"
            row.note = row.note or "answers as a member (a fleet of itself), not a hub"
            row.drop = True  # a member is a row under its hub, never a hub row
        else:
            row.presence = "unreachable" if row.source in ("peer", "local") else "stopped"
            row.note = row.note or ("no answer" if row.source in ("peer", "local") else "its heartbeat is here but it does not answer")
            row.drop = row.source == "local"
        return row
    except Exception as exc:  # noqa: BLE001 — a probe never takes the tree down
        row.presence = "unreachable"
        row.note = row.note or str(exc)
        return row
    try:
        roster = conn.roster
        host = next((a for a in roster if a.get("host")), {})
        row.version = str(host.get("version") or conn.card.get("version") or row.version or "")
        if host.get("identity") or host.get("name"):
            row.name = str(host.get("label") or host.get("name") or row.name)
        members = [a for a in roster if not a.get("host")]
        row.members = sum(1 for a in members if not a.get("remote"))
        row.running = sum(1 for a in members if not a.get("remote") and a.get("running"))
        row.remotes = sum(1 for a in members if a.get("remote"))
        row.presence = "running"
        row.token = conn.client._token
        row.url = conn.client.url
        if row.root is None and deckhub.is_loopback(row.url):
            seen = conn.client.instance_root()
            row.seen_root = _resolve(Path(seen)) if seen else None
    finally:
        conn.client.close()
    return row


def reconcile(rows: list[HubRow]) -> list[HubRow]:
    """After probing: fold a listener found by port into the root row it said it runs
    from (the desktop hub whose heartbeat is missing becomes that root's RUNNING row), drop
    the listeners that turned out to be members or not hubs, keep the rest."""
    by_root: dict[Path, HubRow] = {r.root: r for r in rows if r.root is not None}
    out: list[HubRow] = []
    for r in rows:
        if r.drop:
            continue
        if r.root is None and r.seen_root is not None:
            target = by_root.get(r.seen_root)
            if target is not None:
                if target.presence != "running":
                    target.presence, target.url, target.port = r.presence, r.url, r.port
                    target.launcher = target.launcher or launcher_of(target.root, None) or ("desktop app" if any(_resolve(target.root) == _resolve(d) for d in deckhub.desktop_box_roots()) else "foreground")
                    target.version, target.members, target.running, target.remotes = r.version, r.members, r.running, r.remotes
                    target.candidate, target.token, target.note = r.candidate, r.token, ""
                    if r.name and r.name != r.url:
                        target.name = r.name
                continue  # folded in (or the root already reads running)
            r.launcher = r.launcher or "foreground"
            if r.seen_root is not None and r.source == "local":
                r.root = r.seen_root  # a hub on this box whose root we did not list (an unusual location)
        out.append(r)
    return out


def wait_for_port(url: str, *, timeout_s: float = 30.0, every_s: float = 0.5) -> bool:
    """Poll the agent card until it answers (a hub that was just brought up)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with deckhub.HubClient(url, None) as c:
                if c.agent_card() is not None:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(every_s)
    return False


# ── the screen ────────────────────────────────────────────────────────────────


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
            members = "—" if r.members is None else (f"{r.running}/{r.members} up" if r.running is not None else f"{r.members}") + (f" · {r.remotes} remote" if r.remotes else "")
            where = str(r.root) if r.root is not None else deckhub.redact_url(r.url) if r.url else ""
            note = f"{where} — {r.note}" if r.note and r.root is None else (r.note or where)
            table.add_row(glyph, Text(r.name), r.presence, r.launcher, str(r.port or "—"), r.version or "—", members, Text(note, style="yellow" if r.note else "dim"), key=f"{r.key}#{i}")
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
