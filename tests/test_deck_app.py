"""deck.app — the Textual deck driven by its pilot (#3468) over a fake backend: the roster
renders the console's presence words, lifecycle keys route to the backend and refuse the
host/remotes, the detail screen renders every pane, offline mode hides what needs a hub."""

from __future__ import annotations

import pytest
from textual.widgets import DataTable, RichLog, Static

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
        # the diagnostics/sessions route's real row shape (#3171)
        d.sessions = [
            {"session_id": "chat-1789169255449-mw1pz8", "context_id": "chat-1789169255449-mw1pz8", "latest_task_id": "3137…", "latest_task_state": "TASK_STATE_COMPLETED", "last_activity": "2026-09-11T23:29:04+00:00", "status": "ok", "malformed": []}
        ]
        return d

    def console_href(self, agent):
        return None if self.mode == "offline" else f"http://127.0.0.1:7870/app/agent/{deckdata.slug_of(agent)}/"

    def close(self):
        self.closed = True


async def _settle(app: FleetDeck, pilot) -> None:
    """Wait for every thread worker (and the workers they chain) to finish, then let the
    UI loop apply the results — deterministic where a fixed pause would race."""
    from textual.worker import WorkerCancelled

    for _ in range(20):
        if not app.workers:
            break
        try:
            await app.workers.wait_for_complete()
        except WorkerCancelled:
            pass  # an exclusive worker superseded by a newer one — expected, keep draining
        await pilot.pause()
    await pilot.pause()


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
        await _settle(app, pilot)
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
        await _settle(app, pilot)
        # cursor starts on the host row: x must refuse (the hub can't stop itself)
        await pilot.press("x")
        await _settle(app, pilot)
        assert be.calls == []
        # down to protoEngineer (online) → x stops it
        await pilot.press("j")
        await pilot.press("x")
        await _settle(app, pilot)
        assert be.calls == [("stop", "protoEngineer")]
        # down to Cindi (stopped) → s starts it; the roster re-polls and shows it online
        await pilot.press("j", "j")
        await pilot.press("s")
        await _settle(app, pilot)
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
        await _settle(app, pilot)
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
        await _settle(app, pilot)
        await pilot.press("j")  # protoEngineer
        await pilot.press("i")
        await _settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        head = str(app.screen.query_one("#detail-head", Static).content)
        assert "protoEngineer" in head and "online" in head and ":7875" in head
        runtime = str(app.screen.query_one("#runtime", Static).content)
        assert "claude-fable-5-1 via gateway" in runtime and "protoEngineer · kj" in runtime and "warnings   none" in runtime
        sessions = app.screen.query_one("#sessions", DataTable)
        assert sessions.row_count == 1
        cells = [str(c) for c in sessions.get_row_at(0)]
        assert cells == ["chat-1789169255449-mw1pz8", "completed", "2026-09-11 23:29"]
        tele = str(app.screen.query_one("#telemetry", Static).content)
        assert "turns 38" in tele and "$12.40" in tele and "cache hit 61%" in tele
        log_head = str(app.screen.query_one("#log-head", Static).content)
        assert "● following" in log_head and "2 shown · window 2" in log_head
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
        # Esc INSIDE the prompt cancels and keeps the active filter (review MEDIUM-4)
        await pilot.press("slash")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert [r[1] for r in _rows(app)] == ["Cindi"]
        # Esc on the roster clears it
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
        await pilot.press("i")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)  # nor does detail


@pytest.mark.asyncio
async def test_log_tail_follows_a_rotating_ring_by_identity():
    """Review HIGH-1: the ring answers the newest N; slicing by count froze the tail at N."""
    be = FakeBackend()
    window: list[dict] = [{"ts": f"2026-09-12T09:00:{i:02d}+00:00", "level": "INFO", "logger": "t", "message": f"line {i}"} for i in range(5)]
    n = {"next": 5}

    def detail(agent):
        d = deckdata.MemberDetail(slug=deckdata.slug_of(agent), name=agent["name"])
        d.logs = list(window[-5:])  # a 5-line window over a growing ring
        return d

    be.detail = detail  # type: ignore[assignment]
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("j", "i")
        await pilot.pause(0.5)
        assert isinstance(app.screen, DetailScreen)
        log = app.screen.query_one("#log", RichLog)
        assert len(log.lines) == 5
        # three new lines arrive; the window still holds 5 → only the 3 new ones are written
        for _ in range(3):
            window.append({"ts": f"2026-09-12T09:00:{n['next']:02d}+00:00", "level": "INFO", "logger": "t", "message": f"line {n['next']}"})
            n["next"] += 1
        app.screen.refresh_detail()
        await pilot.pause(0.5)
        assert len(log.lines) == 8
        assert "line 7" in str(log.lines[-1])
        # a burst larger than the window rotates the anchor out → the window is re-rendered whole
        for _ in range(9):
            window.append({"ts": f"2026-09-12T09:00:{n['next']:02d}+00:00", "level": "INFO", "logger": "t", "message": f"line {n['next']}"})
            n["next"] += 1
        app.screen.refresh_detail()
        await pilot.pause(0.5)
        assert len(log.lines) == 5 and "line 16" in str(log.lines[-1])
        assert "5 shown · window 5" in str(app.screen.query_one("#log-head", Static).content)


@pytest.mark.asyncio
async def test_log_tail_anchors_on_seq_when_the_member_stamps_it_even_with_duplicate_records():
    """CodeRabbit: identical (ts, logger, message) records defeat identity anchoring; the
    ring now stamps `seq` and the tail anchors on it exactly. Also: an older member without
    seq but with duplicated records still advances (trailing-run identity match)."""
    be = FakeBackend()
    ring: list[dict] = []
    n = {"seq": 0}

    def push(msg: str, *, seq: bool = True) -> None:
        n["seq"] += 1
        rec = {"ts": "2026-09-12T09:00:00+00:00", "level": "INFO", "logger": "t", "message": msg}
        if seq:
            rec["seq"] = n["seq"]
        ring.append(rec)

    def detail(agent):
        d = deckdata.MemberDetail(slug=deckdata.slug_of(agent), name=agent["name"])
        d.logs = list(ring[-6:])
        return d

    be.detail = detail  # type: ignore[assignment]
    for _ in range(4):
        push("same")  # four identical records, same second
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "i")
        await _settle(app, pilot)
        log = app.screen.query_one("#log", RichLog)
        assert len(log.lines) == 4
        push("same")
        push("same")
        app.screen.refresh_detail()
        await _settle(app, pilot)
        assert len(log.lines) == 6  # exactly the two new duplicates, no skip, no re-render
        # a burst beyond the window → whole window re-rendered from seq
        for _ in range(9):
            push("burst")
        app.screen.refresh_detail()
        await _settle(app, pilot)
        assert len(log.lines) == 6 and all("burst" in str(line) for line in log.lines)

    # an older member: no seq, duplicated records — the trailing-run match still advances
    ring.clear()
    n["seq"] = 0
    for _ in range(3):
        push("dup", seq=False)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "i")
        await _settle(app, pilot)
        log = app.screen.query_one("#log", RichLog)
        assert len(log.lines) == 3
        push("dup", seq=False)
        push("after", seq=False)
        app.screen.refresh_detail()
        await _settle(app, pilot)
        # identity can't tell a 4th identical "dup" from the three shown (that is what seq
        # is for) — but the tail ADVANCES and never re-renders or duplicates what it showed
        assert "after" in str(log.lines[-1]) and 4 <= len(log.lines) <= 5


@pytest.mark.asyncio
async def test_log_tail_re_renders_when_the_member_restarts_and_seq_starts_over():
    """Final-review blocker: seq is per-process; after a restart the window's seqs are
    LOWER than the anchor, and filtering `> anchor` froze the tail with stale lines."""
    be = FakeBackend()
    window: list[dict] = []

    def rec(seq: int, msg: str, ts: str) -> dict:
        return {"seq": seq, "ts": ts, "level": "INFO", "logger": "t", "message": msg}

    def detail(agent):
        d = deckdata.MemberDetail(slug=deckdata.slug_of(agent), name=agent["name"])
        d.logs = list(window)
        return d

    be.detail = detail  # type: ignore[assignment]
    window[:] = [rec(498, "old a", "2026-09-12T09:00:01+00:00"), rec(499, "old b", "2026-09-12T09:00:02+00:00"), rec(500, "old c", "2026-09-12T09:00:03+00:00")]
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "i")
        await _settle(app, pilot)
        log = app.screen.query_one("#log", RichLog)
        assert len(log.lines) == 3
        # the member restarted: a fresh process, seq from 1, different records
        window[:] = [rec(1, "boot", "2026-09-12T09:05:00+00:00"), rec(2, "ready", "2026-09-12T09:05:01+00:00")]
        app.screen.refresh_detail()
        await _settle(app, pilot)
        assert [str(line) for line in log.lines][-2:] == [str(line) for line in log.lines][-2:]
        assert len(log.lines) == 2 and "ready" in str(log.lines[-1]) and "old" not in str(log.lines[0])
        # a restarted counter that happens to REUSE our anchor number with a different record
        window[:] = [rec(1, "boot", "2026-09-12T09:05:00+00:00"), rec(2, "ready", "2026-09-12T09:05:01+00:00"), rec(3, "x", "2026-09-12T09:05:02+00:00")]
        app.screen.refresh_detail()
        await _settle(app, pilot)
        assert len(log.lines) == 3  # exactly one new line: same seq 2, same record → advance
        window[:] = [rec(2, "different", "2026-09-12T09:09:00+00:00"), rec(3, "y", "2026-09-12T09:09:01+00:00"), rec(4, "z", "2026-09-12T09:09:02+00:00")]
        app.screen.refresh_detail()
        await _settle(app, pilot)
        assert len(log.lines) == 3 and "different" in str(log.lines[0])  # seq 3 exists but names another record → re-rendered


@pytest.mark.asyncio
async def test_roster_survives_duplicate_and_empty_ids():
    """CodeRabbit Major: DataTable raises DuplicateKey on a repeated key; a malformed
    roster must not abort the render on the UI thread."""
    roster = [
        {"name": "a", "id": "dup", "port": 1, "running": True},
        {"name": "b", "id": "dup", "port": 2, "running": False},
        {"port": 3, "running": False},  # no id, no name
    ]
    be = FakeBackend(roster=roster)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert [r[1] for r in _rows(app)] == ["a", "b", ""]
        await pilot.press("j", "j")
        await pilot.pause()
        assert app.screen.selected() is roster[2] or app.screen.selected() == roster[2]


def test_run_returns_textual_return_code_on_a_fatal_error(monkeypatch):
    """CodeRabbit: `run()` must surface Textual's non-zero return_code, not the exit value."""
    from deck import app as deckapp

    class Double:
        return_code = 1

        def __init__(self, backend):
            pass

        def run(self):
            return 0

    monkeypatch.setattr(deckapp, "FleetDeck", Double)
    assert deckapp.run(FakeBackend()) == 1
    Double.return_code = None
    assert deckapp.run(FakeBackend()) == 0


@pytest.mark.asyncio
async def test_detail_head_and_keys_follow_the_current_roster_row():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("j", "i")  # protoEngineer, online
        await pilot.pause(0.5)
        assert app.screen.check_action("stop", ()) is True
        await pilot.press("x")
        await pilot.pause(0.6)
        assert be.calls == [("stop", "protoEngineer")]
        assert "stopped" in str(app.screen.query_one("#detail-head", Static).content)
        assert app.screen.check_action("stop", ()) is False


@pytest.mark.asyncio
async def test_narrow_terminal_stacks_the_detail_panes():
    be = FakeBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("j", "i")
        await pilot.pause(0.5)
        assert app.screen.query_one("#detail-body").has_class("narrow")


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
