"""deck.hubs — every hub on the box (#3472): enumeration from heartbeats and instance
roots (never the shell's environment alone), member counts read from a stopped hub's
files, the launcher word, the probe's state words (unauthorized ≠ unreachable), the port
conflict note, and the tree screen: attach, bring up."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from deck import hub as deckhub
from deck import hubs
from deck.app import FleetDeck, RosterScreen
from deck.hubs import HubRow, HubTreeScreen
from tests.test_deck_app import FakeBackend, _settle


def _hub_root(base: Path, name: str, *, members: int = 0, running_pids: list[int] | None = None, remotes: int = 0, server_pid: dict | None = None) -> Path:
    root = base / name
    ws = root / "workspaces"
    ws.mkdir(parents=True)
    fleet = {}
    for i in range(members):
        d = ws / f"m{i}-{name}"
        d.mkdir()
        (d / "workspace.yaml").write_text(f"id: m{i}-{name}\nname: m{i}\nport: {7900 + i}\n")
    for i, pid in enumerate(running_pids or []):
        fleet[f"m{i}-{name}"] = {"pid": pid, "port": 7900 + i}
    (ws / "fleet.json").write_text(json.dumps(fleet))
    if remotes:
        (ws / "remotes.json").write_text(json.dumps({f"r-{i}": {"name": f"r{i}", "url": f"https://r{i}:7870"} for i in range(remotes)}))
    if server_pid is not None:
        (root / "server.pid").write_text(json.dumps(server_pid))
    return root


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box with: a running hub (heartbeat, launched by `protoagent up`), a stopped scoped
    hub with members, a member root (skipped), the desktop root running, and this shell's
    own instance pointing somewhere unrelated."""
    home = tmp_path / "home"
    home.mkdir()
    running = _hub_root(home, "main", members=2, running_pids=[4242], remotes=1, server_pid={"pid": 111, "port": 7870, "version": "0.165.0"})
    (home / ".instances").mkdir()
    (home / ".instances" / "111.json").write_text(json.dumps({"pid": 111, "port": 7870, "identity": "protoagent", "instance_root": str(running)}))
    stopped = _hub_root(home, "dev", members=3, remotes=0, server_pid={"pid": 999, "port": 7871, "version": "0.164.0"})
    member = home / "main" / "workspaces" / "m0-main"  # already a member dir
    assert (member / "workspace.yaml").is_file()
    desktop = tmp_path / "desktop"
    _hub_root(tmp_path, "desktop", members=1)
    (desktop / ".instances").mkdir()
    (desktop / ".instances" / "222.json").write_text(json.dumps({"pid": 222, "port": 7872, "identity": "studio", "instance_root": str(desktop)}))
    (desktop / ".instances" / "333.json").write_text(json.dumps({"pid": 333, "port": 7875, "identity": "protoEngineer", "instance_root": str(desktop / "workspaces" / "m0-desktop")}))
    own = tmp_path / "own"
    own.mkdir()

    class _Paths:
        instance_root = own

    monkeypatch.setattr(deckhub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(deckhub, "pid_alive", lambda pid: pid in (111, 222, 333, 4242))
    monkeypatch.setattr(deckhub, "is_protoagent_pid", lambda pid: pid in (111, 222, 333, 4242))
    monkeypatch.setattr(deckhub, "known_box_roots", lambda: [home, desktop])
    monkeypatch.setattr(deckhub, "data_home", lambda: home)
    monkeypatch.setattr(deckhub, "desktop_box_roots", lambda: [desktop])
    return {"home": home, "running": running, "stopped": stopped, "desktop": desktop, "own": own}


def test_enumerate_finds_running_and_stopped_hubs_skips_members_and_counts_from_disk(box):
    rows = hubs.enumerate_hubs(peers=[{"name": "ava", "url": "https://ava.tail:7870", "host": "ava.tail", "port": 7870}, {"url": "http://127.0.0.1:7870"}])
    by = {r.name: r for r in rows}
    assert set(by) == {"protoagent", "studio", "dev", "ava"}  # the member heartbeat (7875) is not a hub; the loopback peer is the running hub, deduped
    main = by["protoagent"]
    assert (main.presence, main.launcher, main.port, main.pid, main.source) == ("running", "protoagent up", 7870, 111, "heartbeat")
    assert (main.members, main.running, main.remotes) == (2, 1, 1) and main.root == box["running"]
    studio = by["studio"]
    assert (studio.presence, studio.launcher, studio.port) == ("running", "desktop app", 7872) and studio.members == 1
    dev = by["dev"]
    assert (dev.presence, dev.launcher, dev.port, dev.version, dev.source) == ("stopped", "", 7871, "0.164.0", "root")
    assert (dev.members, dev.running, dev.remotes) == (3, 0, 0) and dev.url == "http://127.0.0.1:7871"
    ava = by["ava"]
    assert (ava.presence, ava.launcher, ava.source, ava.root, ava.candidate.url) == ("unreachable", "peer", "peer", None, "https://ava.tail:7870")
    assert ava.candidate.trusted is False  # an off-box peer discovery reported: never sent a credential
    assert all(r.candidate.trusted for r in rows if r.candidate is not None and r is not ava)  # this box's own listeners are
    assert all(not r.note for r in rows)


def test_two_stopped_roots_claiming_one_port_both_say_so(box):
    _hub_root(box["home"], "other", members=0, server_pid={"pid": 998, "port": 7871})
    rows = hubs.enumerate_hubs()
    notes = {r.name: r.note for r in rows if r.port == 7871}
    assert notes == {"dev": "port 7871 also claimed by other", "other": "port 7871 also claimed by dev"}


def test_launcher_word_and_member_counts(box):
    assert hubs.launcher_of(box["desktop"], 222) == "desktop app"
    assert hubs.launcher_of(box["running"], 111) == "protoagent up"
    assert hubs.launcher_of(box["running"], 4243) == "foreground"  # a heartbeat the pidfile does not name
    assert hubs.launcher_of(box["stopped"], None) == "" and hubs.launcher_of(None, None) == "peer"
    assert hubs.count_members(box["running"]) == (2, 1, 1) and hubs.count_members(box["own"]) == (0, 0, 0)


def test_probe_tells_unauthorized_from_unreachable_and_reads_a_hub(box, monkeypatch):
    row = hubs.enumerate_hubs()[0]
    assert row.name == "protoagent"

    def refused(*, candidates, token=None, insecure_http=False):
        raise deckhub.NoHub([candidates[0].url], [candidates[0].url], None, None)

    monkeypatch.setattr(deckhub, "connect", refused)
    hubs.probe(row, token=None)
    assert row.presence == "unauthorized" and "refused" in row.note

    def silent(*, candidates, token=None, insecure_http=False):
        raise deckhub.NoHub([candidates[0].url], [], None, None)

    peer = HubRow(name="ava", root=None, url="https://ava.tail:7870", port=7870, presence="unreachable", source="peer", candidate=deckhub.HubCandidate("https://ava.tail:7870", "peer"))
    monkeypatch.setattr(deckhub, "connect", silent)
    hubs.probe(peer, token="tok")
    assert peer.presence == "unreachable" and peer.note == "no answer"

    class _Client:
        url = "http://127.0.0.1:7870"
        _token = "fleet-token"

        def instance_root(self):
            return None  # a hub that does not say (an older build): the row keeps its root

        def close(self):
            pass

    def ok(*, candidates, token=None, insecure_http=False):
        roster = [
            {"name": "protoagent", "label": "protoagent", "id": "protoagent", "host": True, "running": True, "version": "0.165.0"},
            {"name": "a", "id": "a-1", "running": True},
            {"name": "b", "id": "b-1", "running": False},
            {"name": "r", "id": "r-1", "remote": True, "running": False},
        ]
        return deckhub.Connection(client=_Client(), candidate=candidates[0], card={"version": "0.165.0"}, roster=roster)

    monkeypatch.setattr(deckhub, "connect", ok)
    row.note = ""
    hubs.probe(row)
    assert (row.presence, row.version, row.members, row.running, row.remotes, row.token) == ("running", "0.165.0", 2, 1, 1, "fleet-token")


def test_wait_for_port_returns_false_when_nothing_answers(monkeypatch):
    class Dead:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def agent_card(self):
            return None

    monkeypatch.setattr(deckhub, "HubClient", Dead)
    assert hubs.wait_for_port("http://127.0.0.1:7999", timeout_s=0.3, every_s=0.05) is False


# ── the tree screen ──


class _TreeApp:
    """Drive the deck with canned hub rows: no disk, no network."""

    def __init__(self, rows, *, launcher=None, peers=None):
        self.rows = rows
        self.launcher_calls: list = []
        self.peers = peers
        self._launcher = launcher

    def enumerate(self, *, peers=None):
        self.peers_seen = peers
        return [HubRow(**{**r.__dict__}) for r in self.rows]


@pytest.mark.asyncio
async def test_h_opens_the_tree_enter_attaches_and_u_brings_a_hub_up(monkeypatch):
    running = HubRow(name="studio", root=Path("/tmp/desktop"), url="http://127.0.0.1:7872", port=7872, presence="running", launcher="desktop app", version="0.165.0", source="heartbeat", candidate=deckhub.HubCandidate("http://127.0.0.1:7872", "heartbeat"))
    stopped = HubRow(name="dev", root=Path("/tmp/dev"), url="http://127.0.0.1:7871", port=7871, presence="stopped", source="root", members=3, running=0, remotes=0)
    unauthorized = HubRow(name="ava", root=None, url="https://ava.tail:7870", port=7870, presence="unauthorized", launcher="peer", source="peer", note="answers, but every credential was refused — pass --token", candidate=deckhub.HubCandidate("https://ava.tail:7870", "peer"))
    tree = _TreeApp([running, stopped, unauthorized])
    monkeypatch.setattr("deck.app._enumerate_hubs", tree.enumerate)
    monkeypatch.setattr("deck.app._probe_hub", lambda row, **kw: row)
    monkeypatch.setattr("deck.app._wait_for_port", lambda url, **kw: True)
    attached: list = []

    class _Client:
        _token = "t"

        def __init__(self, url):
            self.url = url

        def close(self):
            pass

    def fake_connect(*, candidates, token=None, insecure_http=False):
        attached.append((candidates[0].url, token))
        return deckhub.Connection(client=_Client(candidates[0].url), candidate=candidates[0], card={"name": "studio"}, roster=[{"name": "studio", "id": "studio", "host": True, "running": True, "port": 7872}])

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    swapped: list = []
    from deck import data as deckdata

    class _Live(deckdata.LiveBackend):
        def snapshot(self):
            swapped.append(self.conn.client.url)
            return deckdata.Snapshot(mode="live", label=f"live · {self.conn.client.url}", roster=list(self.conn.roster))

        def fleet_events(self):
            return None

        def warm_max(self):
            return None

    monkeypatch.setattr(deckdata, "LiveBackend", _Live)
    launched: list = []
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0, peers=lambda: [{"name": "ava", "url": "https://ava.tail:7870"}], launcher=lambda row: launched.append(row.root))
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("H")
        await _settle(app, pilot)
        assert isinstance(app.screen, HubTreeScreen) and tree.peers_seen == [{"name": "ava", "url": "https://ava.tail:7870"}]
        table = app.screen.query_one("#hubs", DataTable)
        assert [str(table.get_row_at(i)[1]) for i in range(table.row_count)] == ["studio", "dev", "ava"]
        assert [str(table.get_row_at(i)[2]) for i in range(table.row_count)] == ["running", "stopped", "unauthorized"]
        assert "pass --token" in str(table.get_row_at(2)[7]) and "3 found · 1 running" in str(app.screen.query_one("#hubs-head", Static).content)
        # the footer offers what applies — the RENDERED bindings, not just check_action
        # (`enter` belongs to the focused table, whose row-selected event attaches; `u` is the screen's)
        def footer_keys():
            return {b.binding.action for b in app.screen.active_bindings.values() if b.enabled}

        assert app.screen.check_action("attach", ()) is True and "bring_up" not in footer_keys()
        table.move_cursor(row=1)
        await pilot.pause(0.2)
        assert "bring_up" in footer_keys() and app.screen.check_action("attach", ()) is False
        # u on the stopped hub: the launcher runs for its root, the port answers, the deck attaches
        await pilot.press("u")
        await _settle(app, pilot)
        assert launched == [Path("/tmp/dev")]
        assert attached and attached[-1][0] == "http://127.0.0.1:7871"
        assert isinstance(app.screen, RosterScreen) and swapped and swapped[-1] == "http://127.0.0.1:7871"  # the roster polls the hub that was brought up
        assert be.closed  # the previous hub's client was closed on the switch
        # enter on a running hub from the tree attaches to it
        await pilot.press("H")
        await _settle(app, pilot)
        app.screen.query_one("#hubs", DataTable).move_cursor(row=0)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert isinstance(app.screen, RosterScreen) and attached[-1][0] == "http://127.0.0.1:7872"


@pytest.mark.asyncio
async def test_the_tree_opens_first_with_start_on_hubs_and_bring_up_needs_a_launcher(monkeypatch):
    stopped = HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=None, presence="stopped", source="root")
    monkeypatch.setattr("deck.app._enumerate_hubs", lambda *, peers=None: [HubRow(**stopped.__dict__)])
    monkeypatch.setattr("deck.app._probe_hub", lambda row, **kw: row)
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0, start_on_hubs=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        assert isinstance(app.screen, HubTreeScreen) and isinstance(app.screen_stack[1], RosterScreen)
        assert app.screen.check_action("bring_up", ()) is False  # no launcher (the deck was not started by the CLI)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(app.screen, RosterScreen)


def test_a_listener_found_by_port_is_folded_into_the_root_it_runs_from_and_members_are_dropped(box, monkeypatch):
    """Live finding: the desktop hub writes no heartbeat on this box (its members pruned
    them before #3482), so it is only found by the local port scan — as a nameless
    listener. Its `/api/config/explain` names its root: the tree shows ONE running row for
    that root. Members answering as a fleet of themselves, and listeners that are not
    hubs at all, are not hub rows."""
    desktop = box["desktop"]
    (desktop / ".instances" / "222.json").unlink()  # no heartbeat for the desktop hub any more
    peers = [
        {"name": "protoagent", "url": "http://127.0.0.1:7872", "host": "127.0.0.1", "port": 7872},  # the desktop hub, by port
        {"name": "Roxy", "url": "http://127.0.0.1:7877", "host": "127.0.0.1", "port": 7877},  # a member
        {"name": "hermes", "url": "http://127.0.0.1:7903", "host": "127.0.0.1", "port": 7903},  # not a protoAgent hub
        {"name": "pve01", "url": "http://pve01.tail:7880", "host": "pve01.tail", "port": 7880},  # a tailnet peer, no bearer
    ]
    rows = hubs.enumerate_hubs(peers=peers)
    assert [r.name for r in rows if r.root == desktop] == ["desktop"] and next(r for r in rows if r.root == desktop).presence == "stopped"
    assert [(r.name, r.source) for r in rows if r.root is None] == [("protoagent", "local"), ("Roxy", "local"), ("hermes", "local"), ("pve01", "peer")]

    class _Client:
        def __init__(self, url, root):
            self.url, self._token, self._root = url, "tok", root

        def instance_root(self):
            return self._root

        def close(self):
            pass

    def fake_connect(*, candidates, token=None, insecure_http=False):
        url = candidates[0].url
        if url.endswith(":7872") or url.endswith(":7870"):  # the desktop hub (by port) and the heartbeat-backed hub
            roster = [{"name": "protoagent", "label": "protoagent", "id": "protoagent", "host": True, "running": True, "version": "0.165.0"}, {"name": "m", "id": "m-1", "running": True}]
            return deckhub.Connection(client=_Client(url, str(desktop) if url.endswith(":7872") else str(box["running"])), candidate=candidates[0], card={}, roster=roster)
        if url.endswith(":7877"):
            raise deckhub.NoHub([url], [], [url], None)  # a member: a fleet of itself
        if url.endswith(":7903"):
            raise deckhub.NoHub([url], [], None, {url: "HTTP 404: not found"})
        raise deckhub.NoHub([url], [url], None, None)  # pve01 refuses every credential

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    for r in rows:
        if r.candidate is not None:
            hubs.probe(r)
    rows = hubs.reconcile(rows)
    names = [(r.name, r.presence, r.launcher, r.port) for r in rows]
    assert ("protoagent", "running", "desktop app", 7872) in names  # the desktop root's row, now running, named by its identity
    assert not any(r.root is None and r.source == "local" for r in rows)  # every local listener was folded or dropped
    assert ("pve01", "unauthorized", "peer", 7880) in names
    pve = next(r for r in rows if r.name == "pve01")
    assert pve.candidate.trusted is False and "never sent a credential" in pve.note and "--hub <url> --token" in pve.note
    assert not any(n[0] in ("Roxy", "hermes") for n in names)
    desk = next(r for r in rows if r.root == desktop)
    assert (desk.members, desk.running, desk.version, desk.token, desk.url) == (1, 1, "0.165.0", "tok", "http://127.0.0.1:7872")
    # a heartbeat-backed running row is never overwritten by its own listener
    running = next(r for r in rows if r.root == box["running"])
    assert running.presence == "running" and running.source == "heartbeat"


@pytest.mark.asyncio
async def test_a_rediscover_during_a_bring_up_keeps_the_starting_row_and_a_timeout_is_said(monkeypatch):
    """Reviewer: `r` mid-launch replaced the row the launcher held — its outcome landed on an
    orphan, the tree read `stopped` and offered a second launch."""
    import threading

    stopped = HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=7871, presence="stopped", source="root")
    monkeypatch.setattr("deck.app._enumerate_hubs", lambda *, peers=None: [HubRow(**stopped.__dict__)])
    monkeypatch.setattr("deck.app._probe_hub", lambda row, **kw: row)
    monkeypatch.setattr("deck.app._wait_for_port", lambda url, **kw: False)  # it never answers
    gate = threading.Event()
    seen: list = []
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0, launcher=lambda row: gate.wait(5))
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        app.notify = lambda msg, **kw: seen.append((msg, kw.get("severity")))  # type: ignore[method-assign]
        await pilot.press("H")
        await _settle(app, pilot)
        await pilot.press("u")
        await pilot.pause(0.3)
        table = app.screen.query_one("#hubs", DataTable)
        assert str(table.get_row_at(0)[2]) == "starting" and app.screen.check_action("refresh", ()) is False
        app.discover_hubs()  # a rediscover lands while the launcher is still out
        await pilot.pause(0.5)
        assert str(app.screen.query_one("#hubs", DataTable).get_row_at(0)[2]) == "starting"  # the in-flight row survives
        assert app.screen.check_action("bring_up", ()) is False  # no second launch offered
        gate.set()
        await _settle(app, pilot)
        assert str(app.screen.query_one("#hubs", DataTable).get_row_at(0)[2]) == "stopped"
        assert "did not answer" in str(app.screen.query_one("#hubs", DataTable).get_row_at(0)[7])
        assert any("did not answer" in m and sev == "error" for m, sev in seen)


@pytest.mark.asyncio
async def test_reopening_the_tree_mid_discovery_still_says_working(monkeypatch):
    import threading

    gate = threading.Event()
    row = HubRow(name="ava", root=None, url="https://ava.tail:7870", port=7870, presence="unreachable", launcher="peer", source="peer", candidate=deckhub.HubCandidate("https://ava.tail:7870", "peer"))
    monkeypatch.setattr("deck.app._enumerate_hubs", lambda *, peers=None: [HubRow(**row.__dict__)])

    def slow_probe(r, **kw):
        gate.wait(5)
        r.presence = "unauthorized"
        return r

    monkeypatch.setattr("deck.app._probe_hub", slow_probe)
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("H")
        await pilot.pause(0.4)
        assert isinstance(app.screen, HubTreeScreen) and "working" in str(app.screen.query_one("#hubs-head", Static).content)
        await pilot.press("escape")
        await pilot.pause(0.1)
        await pilot.press("H")
        await pilot.pause(0.3)
        assert "working" in str(app.screen.query_one("#hubs-head", Static).content)  # re-opened: still discovering
        gate.set()
        await _settle(app, pilot)
        assert "working" not in str(app.screen.query_one("#hubs-head", Static).content)
        assert str(app.screen.query_one("#hubs", DataTable).get_row_at(0)[2]) == "unauthorized"


# ── round-1 review (discovery / attach / launcher) ──


def test_the_loose_box_root_is_never_a_hub_row_but_its_default_child_is(box):
    """Reviewer: the plain data home is the BOX root; a pre-scoping fleet.json left there
    made it a "default · stopped" row whose `u` would start an UNSCOPED server (#706).
    The default instance lives at <box>/default."""
    home = box["home"]
    (home / "workspaces").mkdir()
    (home / "workspaces" / "fleet.json").write_text("{}")
    _hub_root(home, "default", members=1)
    names = [(r.name, r.root) for r in hubs.enumerate_hubs()]
    assert (home.name, home) not in names and not any(r == home for _, r in names)
    assert ("default", home / "default") in names
    assert hubs._is_loose_box_root(home) and not hubs._is_loose_box_root(box["desktop"])  # the desktop's root is an instance of its own


def test_a_recycled_pid_in_a_stopped_hubs_fleet_json_is_not_a_running_member(box, monkeypatch):
    monkeypatch.setattr(deckhub, "is_protoagent_pid", lambda pid: False)  # alive, but not ours
    assert hubs.count_members(box["running"]) == (2, 0, 1)


def test_failures_are_told_apart_insecure_unreadable_not_a_hub():
    assert hubs._classify_failure("http://x:7870 refuses to send a credential over plain http — use an https:// hub URL, or pass --insecure-http for a link you know is encrypted")[0] == "insecure"
    assert hubs._classify_failure("HTTP 404: Not Found") == ("unreachable", "not a hub: HTTP 404: Not Found", True)
    p, n, d = hubs._classify_failure("http://x:7870 did not answer (ReadTimeout)")
    assert p == "unreadable" and "roster did not" in n and d is False
    assert hubs._classify_failure("a live server process (pid 5) did not answer its agent card") == ("unreachable", "a live server process (pid 5) did not answer its agent card", False)


def test_local_listeners_are_matched_to_roots_on_disk_before_any_request(box, monkeypatch):
    """A member's port is in its hub's fleet.json → never a hub row, whatever it would
    refuse; a hub's port in its server.pid → that root's own fleet token is in the chain."""
    calls: list = []
    monkeypatch.setattr(deckhub, "connect", lambda **kw: (calls.append(kw["candidates"][0]) or (_ for _ in ()).throw(deckhub.NoHub([kw["candidates"][0].url], [kw["candidates"][0].url], None, None))))
    peers = [
        {"name": "m0", "url": "http://127.0.0.1:7900", "host": "127.0.0.1", "port": 7900},  # main's member (fleet.json port 7900)
        {"name": "devhub", "url": "http://127.0.0.1:7871", "host": "127.0.0.1", "port": 7871},  # dev's server.pid port
    ]
    rows = hubs.enumerate_hubs(peers=peers)
    assert not any(r.port == 7900 and r.root is None for r in rows)  # the member never became a listener row
    dev_listener = next(r for r in rows if r.root is None and r.port == 7871)
    assert dev_listener.candidate.instance_root == box["stopped"] and dev_listener.seen_root == box["stopped"]
    hubs.probe(dev_listener)
    assert calls[0].instance_root == box["stopped"]  # its own root's token is what the chain reads first
    rows = hubs.reconcile(rows)
    dev = next(r for r in rows if r.root == box["stopped"])
    assert dev.presence == "unauthorized" and dev.port == 7871 and "refused" in dev.note  # one row, the root's, wearing the word
    assert not any(r.root is None and r.port == 7871 for r in rows)


def test_a_refused_loopback_listener_is_retried_with_every_hub_roots_own_token(box, monkeypatch):
    dev = box["stopped"]
    (dev / "workspaces" / ".fleet-token").write_text("dev-token")
    seen: list = []

    def fake_connect(*, candidates, token=None, insecure_http=False):
        cand = candidates[0]
        seen.append(cand.instance_root)
        if cand.instance_root == dev:
            class _C:
                url, _token = cand.url, "dev-token"

                def instance_root(self):
                    return str(dev)

                def close(self):
                    pass

            return deckhub.Connection(client=_C(), candidate=cand, card={}, roster=[{"name": "dev", "id": "dev", "host": True, "running": True, "version": "0.1"}])
        raise deckhub.NoHub([cand.url], [cand.url], None, None)

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    row = HubRow(name="?", root=None, url="http://127.0.0.1:7871", port=7871, presence="unreachable", source="local", candidate=deckhub.HubCandidate("http://127.0.0.1:7871", "peer"))
    hubs.probe(row, roots=hubs.instance_roots())
    assert seen[0] is None and dev in seen  # the default chain first, then each root that has a token
    assert (row.presence, row.token, row.seen_root) == ("running", "dev-token", dev)
    # a tailnet peer is never retried with local tokens
    seen.clear()
    peer = HubRow(name="ava", root=None, url="https://ava.tail:7870", port=7870, presence="unreachable", source="peer", candidate=deckhub.HubCandidate("https://ava.tail:7870", "peer"))
    hubs.probe(peer, roots=hubs.instance_roots())
    assert seen == [None] and peer.presence == "unauthorized"


@pytest.mark.asyncio
async def test_a_poll_of_the_hub_the_deck_just_left_never_paints_the_new_roster(monkeypatch):
    """Reviewer: `exclusive` cancels the awaiting task, not the thread — the old hub's
    snapshot landed after the switch, under the new hub's label."""
    import threading
    import time

    from deck import data as deckdata

    gate = threading.Event()

    class SlowOld(FakeBackend):
        def snapshot(self):
            gate.wait(5)
            time.sleep(0.05)
            return super().snapshot()

    old = SlowOld()
    new = FakeBackend(roster=[{"name": "newhub", "id": "newhub", "port": 7872, "pid": 9, "running": True, "host": True, "version": "0.165.0"}, {"name": "nm", "id": "nm-1", "port": 7901, "pid": 10, "running": True, "version": "0.165.0"}])
    app = FleetDeck(old, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)  # the first poll is out, blocked on the gate
        row = HubRow(name="newhub", root=None, url="http://127.0.0.1:7872", port=7872, presence="running", source="peer")
        app._switch_backend(new, row)  # attach while the old poll is still out
        await pilot.pause(0.2)
        gate.set()
        await _settle(app, pilot)
        await pilot.pause(0.3)
        table = app.screen.query_one("#roster", DataTable)
        names = [str(table.get_row_at(i)[1]) for i in range(table.row_count)]
        assert names == ["newhub", "nm"], names  # never protoEngineer/old/Cindi from the hub we left
        assert app.snapshot is not None and app.snapshot.roster[0]["id"] == "newhub"
        assert isinstance(deckdata, object)


def test_a_listener_that_names_another_root_is_rehomed_and_the_disowned_root_reads_stopped(box, monkeypatch):
    """Round 2: a heartbeat for `dev` whose pid the OS recycled to ANOTHER instance's hub
    (one of ours, so the pid check passes) — the listener on that port is `other`'s hub.
    Before: `dev`'s row wore `other`'s name, roster and credential, `other` read stopped,
    and `u` on it would have started a second server for its root."""
    home = box["home"]
    other = _hub_root(home, "other", members=1)
    (home / ".instances" / "100.json").write_text(json.dumps({"pid": 100, "port": 7877, "identity": "dev-hub", "instance_root": str(box["stopped"])}))
    monkeypatch.setattr(deckhub, "pid_alive", lambda pid: pid in (100, 111, 222, 333, 4242))
    monkeypatch.setattr(deckhub, "is_protoagent_pid", lambda pid: pid in (100, 111, 222, 333, 4242))
    rows = hubs.enumerate_hubs()
    dev = next(r for r in rows if r.root == box["stopped"].resolve())
    assert dev.presence == "running" and dev.port == 7877  # the heartbeat's claim, before the probe

    class _Client:
        def __init__(self, url, root):
            self.url, self._token, self._root = url, "token-other", root

        def instance_root(self):
            return self._root

        def close(self):
            pass

    def fake_connect(*, candidates, token=None, insecure_http=False):
        url = candidates[0].url
        if url.endswith(":7877"):
            roster = [{"name": "other-hub", "label": "other-hub", "id": "other-hub", "host": True, "running": True, "version": "0.165.0"}, {"name": "m", "id": "m-1", "running": True}]
            return deckhub.Connection(client=_Client(url, str(other)), candidate=candidates[0], card={}, roster=roster)
        if url.endswith(":7870") or url.endswith(":7872"):
            return deckhub.Connection(client=_Client(url, None), candidate=candidates[0], card={}, roster=[{"name": "h", "id": "h", "host": True, "running": True}])
        raise deckhub.NoHub([url], [], None, None)

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    for r in rows:
        if r.candidate is not None:
            hubs.probe(r, roots=hubs.instance_roots())
    rows = hubs.reconcile(rows)
    by_root = {r.root.name: r for r in rows if r.root is not None}
    assert (by_root["other"].presence, by_root["other"].name, by_root["other"].port, by_root["other"].token) == ("running", "other-hub", 7877, "token-other")
    assert by_root["dev"].presence == "stopped" and "another instance" in by_root["dev"].note and by_root["dev"].members == 3
    assert [r.root.name for r in rows if r.root is not None].count("dev") == 1


@pytest.mark.asyncio
async def test_member_detail_open_while_an_attach_lands_does_not_kill_the_deck(monkeypatch):
    """Round 2: `_switch_backend` closes the old hub's client under the detail screen's
    worker; the next proxied GET raises a plain RuntimeError (not a HubError) and, unguarded,
    that worker's exception took the whole TUI down. Keystrokes: H, u, esc, i, hub comes up."""

    class TornOld(FakeBackend):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.raised = threading.Event()

        def detail(self, agent):
            self.gate.wait(5)
            if self.closed:
                self.raised.set()
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return super().detail(agent)

        def close(self):
            super().close()
            self.gate.set()

    old = TornOld()
    new = FakeBackend(roster=[{"name": "newhub", "id": "newhub", "port": 7872, "pid": 9, "running": True, "host": True, "version": "0.165.0"}, {"name": "nm", "id": "nm-1", "port": 7901, "pid": 10, "running": True, "version": "0.165.0"}])
    app = FleetDeck(old, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("i")
        await pilot.pause(0.3)  # the detail worker is out, blocked on the gate
        row = HubRow(name="newhub", root=None, url="http://127.0.0.1:7872", port=7872, presence="running", source="peer")
        app._switch_backend(new, row)  # the attach lands: closes `old` under that worker
        for _ in range(20):
            await pilot.pause(0.05)
        assert old.raised.is_set() and isinstance(app.screen, RosterScreen) and app._exception is None
        await _settle(app, pilot)
        table = app.screen.query_one("#roster", DataTable)
        assert [str(table.get_row_at(i)[1]) for i in range(table.row_count)] == ["newhub", "nm"]


@pytest.mark.asyncio
async def test_the_last_attach_the_operator_chose_wins(monkeypatch):
    """Round 2: `exclusive` cancels the awaiting task, not the thread — a slow attach to A,
    abandoned for B, landed after B and replaced it (closing the hub the operator chose)."""
    from deck import data as deckdata

    gates = {"7871": threading.Event(), "7872": threading.Event()}
    closed: list[str] = []

    class _Client:
        _token = "t"

        def __init__(self, url):
            self.url = url

        def close(self):
            closed.append(self.url)

    def gated_connect(*, candidates, token=None, insecure_http=False):
        url = candidates[0].url
        gates[url[-4:]].wait(5)
        return deckhub.Connection(client=_Client(url), candidate=candidates[0], card={}, roster=[{"name": f"hub{url[-4:]}", "id": f"hub{url[-4:]}", "host": True, "running": True, "port": int(url[-4:])}])

    class _Live(deckdata.LiveBackend):
        def snapshot(self):
            return deckdata.Snapshot(mode="live", label=f"live · {self.conn.client.url}", roster=list(self.conn.roster))

        def fleet_events(self):
            return None

        def warm_max(self):
            return None

    monkeypatch.setattr(deckhub, "connect", gated_connect)
    monkeypatch.setattr(deckdata, "LiveBackend", _Live)
    a = HubRow(name="A", root=None, url="http://127.0.0.1:7871", port=7871, presence="running", source="peer", candidate=deckhub.HubCandidate("http://127.0.0.1:7871", "peer"))
    b = HubRow(name="B", root=None, url="http://127.0.0.1:7872", port=7872, presence="running", source="peer", candidate=deckhub.HubCandidate("http://127.0.0.1:7872", "peer"))
    app = FleetDeck(FakeBackend(), poll_s=0)
    notes: list[str] = []
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        app.notify = lambda msg, **kw: notes.append(msg)
        app.attach_hub(a)  # slow
        await pilot.pause(0.1)
        app.attach_hub(b)  # the operator gave up on A
        gates["7872"].set()
        await pilot.pause(0.5)
        assert app.backend.conn.client.url.endswith(":7872")
        gates["7871"].set()  # A's connect returns late
        await _settle(app, pilot)
        await pilot.pause(0.3)
        assert app.backend.conn.client.url.endswith(":7872"), notes  # B, the last choice, is the deck's hub
        assert "http://127.0.0.1:7871" in closed and "http://127.0.0.1:7872" not in closed
        assert [n for n in notes if n.startswith("attached to")] == ["attached to B (http://127.0.0.1:7872)"]


@pytest.mark.asyncio
async def test_offline_deck_tree_lists_disk_and_probes_nothing(monkeypatch):
    """Round 2: `--offline` documented "skips the scan and the probes", but the deck's tree
    still probed every running row with this box's fleet tokens."""
    running = HubRow(name="studio", root=Path("/tmp/desktop"), url="http://127.0.0.1:7872", port=7872, presence="running", launcher="desktop app", source="heartbeat", candidate=deckhub.HubCandidate("http://127.0.0.1:7872", "heartbeat"))
    probed: list = []
    monkeypatch.setattr("deck.app._enumerate_hubs", lambda *, peers=None: [HubRow(**running.__dict__)])
    monkeypatch.setattr("deck.app._probe_hub", lambda row, **kw: probed.append(row.url) or row)
    monkeypatch.setattr("deck.app._instance_roots", lambda: [])
    app = FleetDeck(FakeBackend(mode="offline"), poll_s=0, peers=None, launcher=lambda row: None, start_on_hubs=True, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.pause(0.3)
        assert isinstance(app.screen, HubTreeScreen) and not app.screen.busy
        assert [(r.name, r.presence) for r in app.hub_rows] == [("studio", "running")]  # what disk says, unprobed
    assert probed == []
