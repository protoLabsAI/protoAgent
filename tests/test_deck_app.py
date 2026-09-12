"""deck.app — the Textual deck driven by its pilot (#3468) over a fake backend: the roster
renders the console's presence words, lifecycle keys route to the backend and refuse the
host/remotes, the detail screen renders every pane, offline mode hides what needs a hub."""

from __future__ import annotations

import pytest
from textual.widgets import DataTable, Static

from deck import data as deckdata
from deck.app import DetailScreen, FleetDeck, RosterScreen

ROSTER = [
    {"name": "protoagent", "id": "protoagent", "port": 7870, "pid": 100, "running": True, "host": True, "version": "0.165.0"},
    {"name": "protoEngineer", "id": "protoEngineer-ba4c", "port": 7875, "pid": 15285, "running": True, "version": "0.165.0"},
    {"name": "old", "id": "old-1", "port": 7890, "pid": 77, "running": True, "version": "0.164.0"},
    {"name": "Cindi", "id": "Cindi-9f49", "port": 7880, "pid": None, "running": False, "version": "", "bundle": "cowork-stack"},
    {"name": "ava", "id": "r-ava", "port": None, "pid": None, "running": False, "remote": True, "url": "https://ava.tail:7870"},
]


class FakeBackend:
    def __init__(self, mode="live", roster=None, warnings=None):
        self.mode = mode
        # deep-copied: start/stop mutate rows, and ROSTER is shared by every test
        self.roster = [dict(a) for a in (ROSTER if roster is None else roster)]
        self.warnings = list(warnings or [])
        self.calls: list[tuple] = []
        self.closed = False

    def snapshot(self):
        snap = deckdata.Snapshot(mode=self.mode, label="live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat", roster=list(self.roster), host_version="0.165.0", warnings=list(self.warnings))
        snap.rollups["protoEngineer-ba4c"] = deckdata.Rollup(turns=38, cost_usd=12.4, success_rate=0.97, cache_hit_ratio=0.61)
        return snap

    def start(self, name):
        self.calls.append(("start", name))
        for a in self.roster:
            if a["name"] == name:
                a["running"], a["pid"] = True, 4242
        return {"ok": True, "agent": {"name": name}}

    def stop(self, name):
        self.calls.append(("stop", name))
        for a in self.roster:
            if a["name"] == name:
                a["running"], a["pid"] = False, None
        return {"ok": True, "stopped": True}

    def detail(self, agent):
        d = deckdata.MemberDetail(slug=deckdata.slug_of(agent), name=agent["name"])
        d.runtime = {"model": {"name": "claude-fable-5-1", "provider": "gateway"}, "identity": {"name": agent["name"], "operator": "kj"}, "version": "0.165.0", "setup_complete": True, "graph_loaded": True, "warnings": []}
        d.logs = [
            {"ts": "2026-09-12T09:41:02+00:00", "level": "INFO", "logger": "a2a", "message": "task 7f3a state=working"},
            {"ts": "2026-09-12T09:41:31+00:00", "level": "WARNING", "logger": "gateway", "message": "429, retry 2/5"},
        ]
        d.sessions = [{"context_id": "chat-1789169255449-mw1pz8", "state": "completed", "task_count": 12, "last_updated": "2026-09-11T23:29:04"}]
        return d

    def console_href(self, agent):
        return None if self.mode == "offline" else f"http://127.0.0.1:7870/app/agent/{deckdata.slug_of(agent)}/"

    def close(self):
        self.closed = True


def _rows(app: FleetDeck) -> list[list[str]]:
    table = app.screen.query_one("#roster", DataTable)
    out = []
    for key in table.rows:
        out.append([str(c) for c in table.get_row(key)])
    return out


@pytest.mark.asyncio
async def test_roster_renders_presence_words_skew_spend_and_topbar():
    be = FakeBackend(warnings=["1 fleet member(s) run a different protoAgent version"])
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, RosterScreen)
        assert "live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat" in str(app.screen.query_one("#topbar", Static).content)
        assert "different protoAgent version" in str(app.screen.query_one("#banner", Static).content)
        rows = _rows(app)
        assert [r[1] for r in rows] == ["protoagent", "protoEngineer", "old", "Cindi", "ava"]
        assert [r[2] for r in rows] == ["host", "online", "online", "stopped", "unreachable"]
        assert rows[2][4] == "v0.164.0 !skew" and rows[1][4] == "v0.165.0"
        assert rows[1][6] == "$12.40" and rows[3][6] == "—"
        assert rows[4][7] == "https://ava.tail:7870"
        assert "2 online · 1 stopped" in str(app.screen.query_one("#status", Static).content)
        await pilot.press("q")
    assert be.closed


@pytest.mark.asyncio
async def test_lifecycle_keys_route_to_the_backend_and_refuse_host_and_remote():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        # cursor starts on the host row: x must refuse (the hub can't stop itself)
        await pilot.press("x")
        await pilot.pause(0.2)
        assert be.calls == []
        # down to protoEngineer (online) → x stops it
        await pilot.press("j")
        await pilot.press("x")
        await pilot.pause(0.4)
        assert be.calls == [("stop", "protoEngineer")]
        # down to Cindi (stopped) → s starts it; the roster re-polls and shows it online
        await pilot.press("j", "j")
        await pilot.press("s")
        await pilot.pause(0.4)
        assert be.calls[-1] == ("start", "Cindi")
        rows = _rows(app)
        assert rows[3][2] == "online"
        # the remote row: s/x refuse
        await pilot.press("j")
        await pilot.press("x")
        await pilot.press("s")
        await pilot.pause(0.2)
        assert be.calls[-1] == ("start", "Cindi")
        # restart = stop then start on an ONLINE member (protoEngineer was stopped above,
        # so `r` is disabled there — restart `old` instead)
        await pilot.press("k", "k")  # back to old
        await pilot.press("r")
        await pilot.pause(0.4)
        assert be.calls[-2:] == [("stop", "old"), ("start", "old")]
        # ...and on the stopped protoEngineer, r does nothing
        await pilot.press("k")
        await pilot.press("r")
        await pilot.pause(0.2)
        assert be.calls[-1] == ("start", "old")


@pytest.mark.asyncio
async def test_detail_screen_renders_runtime_logs_sessions_and_telemetry():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("j")  # protoEngineer
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, DetailScreen)
        head = str(app.screen.query_one("#detail-head", Static).content)
        assert "protoEngineer" in head and "online" in head and ":7875" in head
        runtime = str(app.screen.query_one("#runtime", Static).content)
        assert "claude-fable-5-1 via gateway" in runtime and "protoEngineer · kj" in runtime and "warnings   none" in runtime
        sessions = app.screen.query_one("#sessions", DataTable)
        assert sessions.row_count == 1
        tele = str(app.screen.query_one("#telemetry", Static).content)
        assert "turns 38" in tele and "$12.40" in tele and "cache hit 61%" in tele
        log_head = str(app.screen.query_one("#log-head", Static).content)
        assert "● following" in log_head and "2 lines" in log_head
        await pilot.press("l")
        await pilot.pause(0.1)
        assert "○ paused" in str(app.screen.query_one("#log-head", Static).content)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)


@pytest.mark.asyncio
async def test_filter_narrows_the_roster_and_escape_clears_it():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("slash")
        await pilot.pause(0.2)
        await pilot.press(*"coach")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert [r[1] for r in _rows(app)] == []  # nothing matches "coach" in this roster
        await pilot.press("slash")
        await pilot.pause(0.2)
        await pilot.press(*(["backspace"] * 5))  # the prompt reopens with the previous filter
        await pilot.press(*"stopped")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert [r[1] for r in _rows(app)] == ["Cindi"]
        assert "filter: 'stopped'" in str(app.screen.query_one("#status", Static).content)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert len(_rows(app)) == 5


@pytest.mark.asyncio
async def test_offline_mode_is_badged_and_hides_hub_only_keys():
    be = FakeBackend(mode="offline", roster=[{"name": "alpha", "id": "alpha-1", "port": 7901, "pid": None, "running": False}])
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        status = str(app.screen.query_one("#status", Static).content)
        assert "offline: only start/stop are available" in status
        assert app.screen.check_action("detail", ()) is False
        assert app.screen.check_action("logs", ()) is False
        assert app.screen.check_action("start", ()) is True
        assert app.screen.check_action("stop", ()) is False
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)  # enter does nothing offline


@pytest.mark.asyncio
async def test_failed_poll_keeps_the_last_roster_and_says_so():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert len(_rows(app)) == 5
        be.snapshot = lambda: deckdata.Snapshot(mode="live", label="x", error="http://127.0.0.1:7870 did not answer (ReadTimeout)")  # type: ignore[assignment]
        app.poll()
        await pilot.pause(0.4)
        assert len(_rows(app)) == 5
        assert "last poll failed" in str(app.screen.query_one("#status", Static).content)
