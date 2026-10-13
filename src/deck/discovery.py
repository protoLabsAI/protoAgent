"""Every hub on the box (#3472): discovery and probing — the model the hub tree and the CLI's ``fleet --all`` share.

Textual-free by contract: ``protoagent fleet --all --json`` (and every non-interactive verb)
must never load Textual — the screen lives in ``deck.hubs``, which imports THIS module.

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
different problems, two words. A peer that network discovery reported is never sent a
credential (``HubCandidate.trusted=False``): its name is its own claim. Bringing a hub up runs ``protoagent up`` for THAT instance
root (an injected launcher — the CLI supplies it); stopping a hub is not a deck action.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path


from deck import hub as deckhub


FLEET_JSON = "fleet.json"
REMOTES_JSON = "remotes.json"
PRESENCE_GLYPH = {"running": "●", "unauthorized": "◐", "insecure": "◐", "unreadable": "◐", "unreachable": "◌", "stopped": "○", "starting": "◍"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(value: object) -> str:
    """A remotely supplied string (a peer's name, a hub's version, a note built from either)
    with every C0 / C1 control character removed, for a terminal's stdout: a newline breaks
    the row, an ESC or an OSC sequence can clear or rewrite the screen. JSON output keeps the
    raw value; the deck's cells are rich ``Text``, which strips control codes itself."""
    return _CONTROL_RE.sub("", str(value))


def row_text(r: HubRow) -> tuple[str, str]:
    """The two derived cells the CLI table and the deck's tree both show: ``members``
    (``2/3 up · 1 remote``) and ``where`` (the instance root, or the redacted url — with
    the note folded in for a row that has no root). One place, so they cannot drift."""
    members = "—" if r.members is None else (f"{r.running}/{r.members} up" if r.running is not None else f"{r.members}") + (f" · {r.remotes} remote" if r.remotes else "")
    place = str(r.root) if r.root is not None else (deckhub.redact_url(r.url) if r.url else "")
    where = f"{place} — {r.note}" if r.note and r.root is None else (r.note or place)
    return members, where


def _peer_destination_ok(url: str) -> bool:
    """A destination policy for peers that discovery REPORTED: an mDNS advert on an untrusted
    LAN can name any address, so only a unicast address a hub could plausibly listen on is
    probed — loopback (this box), private, tailnet or public — never link-local (the cloud
    metadata range lives there), multicast, unspecified or reserved. A hostname is left to
    DNS; either way a discovered peer is sent no credential (``HubCandidate.trusted``)."""
    host = (urlsplit(url).hostname or "").strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return bool(host)
    return not (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


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
    disowned_root: Path | None = field(default=None, repr=False)  # the root a heartbeat/pidfile claimed, when the listener said it runs from another

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
                        if deckhub.is_protoagent_pid(int(rec.get("pid") or 0)):  # a stopped hub's fleet.json outlives reboots: a recycled pid is not a running member
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
        return "foreground" if pid is not None else "peer"  # a heartbeat that names no root is still a local server
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
    """Named by its directory; the desktop's box root by the app id (the probe gives it
    the hub's identity once it answers)."""
    return root.name


def _is_loose_box_root(root: Path) -> bool:
    """The plain data home (``~/.protoagent``) is the machine-shared BOX root, not an
    instance: the default instance lives at ``<box>/default``. A pre-scoping
    ``workspaces/fleet.json`` left in the box root must not make it a hub row — bringing it
    up would run an UNSCOPED server writing into the shared root (#706). The desktop app's
    root is both box and instance by design and is not loose."""
    r = _resolve(root)
    if r is None or any(r == _resolve(d) for d in deckhub.desktop_box_roots()):
        return False
    return r == _resolve(deckhub.data_home())


def instance_roots() -> list[Path]:
    """Every instance root on this box that is (or was) a hub: each known box root's child
    instance roots (``<box>/default``, ``<box>/dev``, …), the desktop's root (an instance of
    its own), and this shell's instance root — existing, deduped. The plain data home
    itself is a box root, not an instance (see :func:`_is_loose_box_root`)."""
    bases: list[Path] = []
    for b in [*deckhub.known_box_roots(), deckhub.data_home()]:
        rb = _resolve(b)
        if rb is not None and rb.is_dir() and rb not in bases:
            bases.append(rb)
    out: list[Path] = []
    cands: list[Path] = []
    for b in bases:
        if not _is_loose_box_root(b):
            cands.append(b)
        try:
            cands.extend(sorted(p for p in b.iterdir() if p.is_dir() and not p.name.startswith(".")))
        except OSError:
            pass
    own = _resolve(deckhub.instance_paths().instance_root)
    if own is not None:
        cands.append(own)
    for c in cands:
        rc = _resolve(c)
        if rc is not None and rc not in out and _is_hub_root(rc) and not _is_loose_box_root(rc):
            out.append(rc)
    return out


def _record_ports(workspaces: Path) -> list[int]:
    """The port each member's ``workspace.yaml`` records under ``<root>/workspaces``. A
    STOPPED member is usually absent from ``fleet.json`` (the supervisor lists what it
    started), yet its port is still its own: the member binds it at its next start."""
    import yaml

    try:
        dirs = sorted(d for d in workspaces.iterdir() if d.is_dir())
    except OSError:
        return []
    out: list[int] = []
    for d in dirs:
        try:
            rec = yaml.safe_load((d / "workspace.yaml").read_text(encoding="utf-8")) or {}
            port = int(rec.get("port") or 0) if isinstance(rec, dict) else 0
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            continue
        if port:
            out.append(port)
    return out


def _ports_on_disk(roots: list[Path], *, records: bool = False) -> tuple[dict[int, Path], dict[int, Path]]:
    """``(member ports, hub ports)`` every hub root on this box records: members from each
    root's ``fleet.json`` (the members its supervisor started), the hub itself from its
    ``server.pid``. A local listener is matched here BEFORE any request — a member is then
    a row under its hub, never a hub row, whatever credential it would refuse.

    ``records=True`` adds every member's ``workspace.yaml`` port (a STOPPED member is
    usually only there) — for a caller about to TAKE a port, i.e. bringing a hub up. The
    tree's listener skip must not use it: a listener on a stopped member's port can never
    be that member, so skipping it could only hide a real hub found by the port scan."""
    members: dict[int, Path] = {}
    hubs_: dict[int, Path] = {}
    for root in roots:
        if records:
            for port in _record_ports(root / "workspaces"):
                members.setdefault(port, root)
        try:
            fleet = json.loads((root / "workspaces" / FLEET_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fleet = {}
        if isinstance(fleet, dict):
            for rec in fleet.values():
                try:
                    port = int((rec or {}).get("port") or 0) if isinstance(rec, dict) else 0
                except (TypeError, ValueError):
                    port = 0
                if port:
                    members.setdefault(port, root)
        try:
            port = int(_server_pid_record(root).get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port:
            hubs_.setdefault(port, root)
    return members, hubs_


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
    roots = instance_roots()
    member_ports, hub_ports = _ports_on_disk(roots)
    for root in roots:
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
        if url in seen_urls or not _peer_destination_ok(url):
            continue
        seen_urls.add(url)
        try:
            port = int(p.get("port") or url.rsplit(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            port = None
        local = deckhub.is_loopback(url)
        if local and port in member_ports:
            continue  # a member of a hub on this box (its fleet.json says so): a row under that hub, never a hub row
        # a listener on THIS box found by port (the desktop hub whose heartbeat is missing,
        # a foreground server) is a hub row only once it says which root it runs from —
        # `reconcile` folds it into that root's row; a member answers as a fleet of itself
        # and is dropped there. A port a root's server.pid remembers names the root up front,
        # so that root's own fleet token is in the chain.
        known_root = hub_ports.get(port) if local and port else None
        # An off-box peer that discovery reported is never sent a credential (its name is
        # self-reported; a bearer belongs to a hub the operator names with --hub).
        rows.append(HubRow(name=str(p.get("name") or p.get("host") or url), root=None, url=url, port=port, presence="unreachable", launcher="" if local else "peer", source="local" if local else "peer", candidate=deckhub.HubCandidate(url, "peer", instance_root=known_root, trusted=local), seen_root=known_root))
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


def _classify_failure(why: str) -> tuple[str, str, bool]:
    """``(presence, note, drop)`` for a ``NoHub.failed`` reason — three different things
    hide in it: a credential that would travel in cleartext (the operator's call), a hub
    that answered its card but not its roster (slow, not absent), and a listener that is
    not a hub at all (nothing to show)."""
    w = why.lower()
    if "insecure-http" in w or "plain http" in w or "cleartext" in w:
        return "insecure", "would send a credential over plain http — pass --insecure-http for a link you trust", False
    if "http 404" in w or "malformed roster" in w or "no agents list" in w:
        return "unreachable", f"not a hub: {why}", True
    if "did not answer its agent card" in w:
        return "unreachable", why, False
    return "unreadable", f"answers, but its roster did not: {why}", False


def probe(row: HubRow, *, token: str | None = None, insecure_http: bool = False, roots: list[Path] | None = None) -> HubRow:
    """Ask a running hub or a peer what it is: version and member counts through its own
    fleet token (an off-box peer that discovery reported gets open mode only — never
    ``token``; see ``HubCandidate.trusted``). Sets ``presence`` to ``running`` /
    ``unauthorized`` / ``insecure`` / ``unreadable`` / ``unreachable`` and keeps the
    connection's credential for a later attach. A loopback listener that refuses this
    shell's credentials is retried with every hub root's own fleet token (``roots``): a
    scoped hub's token lives under its root, which the default chain does not know.
    Never raises."""
    if row.candidate is None:
        return row
    tried_roots: list[Path | None] = [row.candidate.instance_root]
    retry = [r for r in (roots or []) if r not in tried_roots] if deckhub.is_loopback(row.candidate.url) else []
    try:
        conn = deckhub.connect(candidates=[row.candidate], token=token, insecure_http=insecure_http)
    except deckhub.NoHub as exc:
        if exc.unauthorized and retry:
            for root in retry:
                cand = deckhub.HubCandidate(row.candidate.url, row.candidate.source, instance_root=root, identity=row.candidate.identity, pid=row.candidate.pid)
                if not any(p.is_file() for p in deckhub.fleet_token_files(cand)[:1]):
                    continue  # that root has no fleet token to offer
                try:
                    conn = deckhub.connect(candidates=[cand], token=token, insecure_http=insecure_http)
                except deckhub.NoHub as again:
                    exc = again
                    continue
                except Exception:  # noqa: BLE001
                    continue
                row.candidate = cand
                break
            else:
                conn = None
        else:
            conn = None
        if conn is None:
            return _probe_failed(row, exc)
    except Exception as exc:  # noqa: BLE001 — a probe never takes the tree down
        row.presence = "unreachable"
        row.note = row.note or str(exc)
        return row
    return _probe_read(row, conn)


def _probe_failed(row: HubRow, exc: deckhub.NoHub) -> HubRow:
    if exc.unauthorized:
        row.presence = "unauthorized"
        if row.candidate is not None and not row.candidate.trusted:
            row.note = row.note or "answers, but a discovered peer is never sent a credential — name it: protoagent fleet --hub <url> --token …"
        else:
            row.note = row.note or "answers, but every credential was refused — pass --token"
    elif exc.failed:
        presence, note, drop = _classify_failure(next(iter(exc.failed.values()), ""))
        row.presence = presence
        row.note = row.note or note
        row.drop = drop and row.source in ("local", "peer")
    elif exc.members:
        row.presence = "running"
        row.note = row.note or "answers as a member (a fleet of itself), not a hub"
        row.drop = True  # a member is a row under its hub, never a hub row
    else:
        row.presence = "unreachable" if row.source in ("peer", "local") else "stopped"
        row.note = row.note or ("no answer" if row.source in ("peer", "local") else "its heartbeat is here but it does not answer")
        row.drop = row.source == "local"
    return row


def _probe_read(row: HubRow, conn: deckhub.Connection) -> HubRow:
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
        if deckhub.is_loopback(row.url):
            seen = conn.client.instance_root()
            seen_root = _resolve(Path(seen)) if seen else None
            if row.root is None:
                row.seen_root = seen_root
            elif seen_root is not None and seen_root != _resolve(row.root):
                # the heartbeat / pidfile that named this root points at a pid the OS has
                # since handed to ANOTHER instance's hub: this listener is that hub, not this
                # root's. Re-home the row so `reconcile` folds it into the root it named, and
                # remember the root it wrongly wore so it is re-listed as stopped.
                row.disowned_root, row.root, row.seen_root, row.source = row.root, None, seen_root, "local"
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
                    # the listener IS that root's hub — running, or answering but refusing /
                    # unreadable: the root's row carries that word instead of "stopped"
                    target.presence, target.url, target.port = r.presence, r.url, r.port
                    target.launcher = target.launcher or launcher_of(target.root, None) or ("desktop app" if any(_resolve(target.root) == _resolve(d) for d in deckhub.desktop_box_roots()) else "foreground")
                    if r.presence == "running":
                        target.version, target.members, target.running, target.remotes = r.version, r.members, r.running, r.remotes
                        target.name = r.name if r.name and r.name != r.url else target.name
                        target.note = ""
                    else:
                        target.note = r.note
                    target.candidate, target.token = r.candidate, r.token
                continue  # folded in (or the root already reads running)
            r.launcher = r.launcher or "foreground"
            if r.seen_root is not None and r.source == "local":
                r.root = r.seen_root  # a hub on this box whose root we did not list (an unusual location)
        out.append(r)
    # a root whose heartbeat turned out to be another instance's hub has no listener of its
    # own: it is a stopped hub, and `u` must be able to bring it up
    listed = {r.root for r in out if r.root is not None}
    for r in rows:
        d = r.disowned_root
        if d is not None and not r.drop and d not in listed:
            local, running, remotes = count_members(d)
            out.append(HubRow(name=_root_name(d), root=d, url=None, port=None, presence="stopped", launcher="", source="root", members=local, running=running, remotes=remotes, note="its heartbeat named a pid that now runs another instance's hub"))
            listed.add(d)
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

