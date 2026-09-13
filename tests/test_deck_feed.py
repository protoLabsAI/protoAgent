"""deck.feed — the fleet-wide activity model, the work feed screen, and the roster's TURN
column (#3470)."""

from __future__ import annotations

import time

import pytest
from textual.widgets import DataTable, Static

from deck import events as deckevents
from deck import feed
from deck.app import FleetDeck, RosterScreen
from deck.feed import WorkFeedScreen
from deck.talk import ConversationScreen
from tests.test_deck_app import FakeBackend, _settle


def ev(slug, topic, **data):
    return deckevents.Event(slug=slug, topic=topic, data=data, seq=None)


def test_activity_folds_server_fired_turns_and_tools_into_rows_and_state():
    act = feed.Activity({"protoEngineer-ba4c": "protoEngineer"})
    s = "protoEngineer-ba4c"
    act.apply(ev(s, "turn.started", session_id="chat-1", origin="scheduler", trigger="daily"))
    assert act.turn_cell(s) == "⟳ running"
    act.apply(ev(s, "chat.progress", session_id="chat-1", task_id="t1", phase="tool_start", tool="run_command", tool_call_id="c1", control={"operator_controllable": True}))
    act.apply(ev(s, "chat.progress", session_id="chat-1", task_id="t1", phase="tool_end", tool="run_command", tool_call_id="c1", output="3 open"))
    act.apply(ev(s, "chat.progress", session_id="chat-1", task_id="t1", phase="text", text="narration is not a feed row"))
    act.apply(ev(s, "chat.progress", session_id="chat-1", task_id="t1", phase="room_reply", message_id="m1", author="sonnet", text="1 replied"))
    act.apply(ev(s, "turn.finished", session_id="chat-1", task_id="t1", origin="scheduler", ok=True))
    act.apply(ev(s, "turn.usage", task_id="t1", context_id="chat-1", state="TASK_STATE_COMPLETED", model="m", cost_usd=0.5, input_tokens=100, output_tokens=5))
    labels = [(r.glyph, r.label) for r in act.rows]
    assert labels == [("⟳", "turn started"), ("⟳", "run_command"), ("✓", "run_command"), ("✓", "@sonnet replied"), ("✓", "turn finished"), ("$", "turn completed")]
    assert act.rows[2].detail.startswith("3 open") and act.rows[1].controllable
    assert act.turn_cell(s) == "idle" and act.state[s].last_cost_usd == 0.5 and act.state[s].last_model == "m"
    assert act.last_active_cell(s).endswith("s ago")
    assert act.rows[0].member == "protoEngineer"


def test_activity_usage_closes_open_tools_and_failed_turns_are_marked():
    act = feed.Activity()
    act.apply(ev("x", "chat.progress", session_id="s", task_id="t", phase="tool_start", tool="fetch_url", tool_call_id="c9"))
    assert act.turn_cell("x") == "⟳ running"
    act.apply(ev("x", "turn.usage", task_id="t", context_id="s", state="TASK_STATE_FAILED", cost_usd="inf"))
    assert act.turn_cell("x") == "idle" and act.state["x"].open_tools == {} and act.rows[-1].error and act.state["x"].last_cost_usd == 0.0
    act.apply(ev("x", "chat.resumed", session_id="s", task_id="t2", text="", error="boom"))
    assert act.rows[-1].glyph == "✗" and act.rows[-1].label == "settled"


def test_activity_own_turns_and_parking():
    act = feed.Activity({"r": "Roxy"})
    act.note_live("r", "t1", True)
    assert act.turn_cell("r") == "⟳ running"
    act.note_tool("r", "s", "t1", "c1", "current_time", done=False)
    act.note_tool("r", "s", "t1", "c1", "current_time", done=True, output="02:07")
    assert [r.label for r in act.rows] == ["current_time", "current_time"] and act.rows[-1].detail.startswith("02:07")
    act.park("r", "s", "Merge this PR?", task_id="t1")
    assert act.turn_cell("r") == "⚑ needs you" and act.rows[-1].kind == "needs-you" and act.rows[-1].session == "s"
    act.park("r", "s", "Merge this PR?", task_id="t1")  # unchanged → no second row, no second ring
    assert [r.kind for r in act.rows].count("needs-you") == 1
    assert act.ring_due() == [("r", "s", "Merge this PR?")] and act.ring_due() == []
    act.apply(ev("r", "turn.resumed", task_id="t1", context_id="s"))
    assert act.turn_cell("r") == "⟳ running" and act.state["r"].parked == {}
    act.note_live("r", "t1", False)
    assert act.turn_cell("r") == "idle"


def test_stale_open_tool_and_a_turn_whose_end_was_lost_stop_counting_as_running():
    act = feed.Activity()
    act.note_tool("x", "s", "t", "c", "slow", done=False)
    act.state["x"].open_tools["c"] = ("slow", time.monotonic() - feed.RUNNING_TTL_S - 1, "t")
    assert act.turn_cell("x") == "idle"
    act.apply(ev("x", "turn.started", session_id="chat-1", origin="scheduler"))  # its turn.finished never comes
    assert act.turn_cell("x") == "⟳ running"
    act.state["x"].running["chat-1"] -= feed.TURN_TTL_S + 1
    assert act.turn_cell("x") == "idle"
    # a frame for a task refreshes it: a long turn that keeps talking never ages out
    act.apply(ev("x", "chat.progress", session_id="chat-2", task_id="t2", phase="tool_start", tool="t", tool_call_id="c2"))
    act.state["x"].running["t2"] -= feed.TURN_TTL_S + 1
    act.state["x"].open_tools["c2"] = ("t", time.monotonic() - feed.RUNNING_TTL_S - 1, "t2")
    assert act.turn_cell("x") == "idle"
    act.apply(ev("x", "chat.progress", session_id="chat-2", task_id="t2", phase="text", text="still here"))
    assert act.turn_cell("x") == "⟳ running"


def test_parks_are_per_session_and_the_three_writers_merge():
    """Reviewer finding: one park slot per member let the probe, the bus, and the deck's own
    conversation overwrite each other (a bell every 30 s, or "idle" while a human is needed)."""
    act = feed.Activity()
    t0 = time.monotonic()
    act.apply(ev("x", "turn.input_required", context_id="chat-A", task_id="tA", prompt="Approve?"))
    # the deck talks in ANOTHER session: its bookkeeping must not clear A's park
    act.unpark("x", "chat-B")
    act.park("x", "chat-B", "Which branch?", task_id="tB", source="deck")
    assert act.parked_sessions("x") == ["chat-A", "chat-B"] and act.turn_cell("x") == "⚑ needs you"
    act.unpark("x", "chat-B")
    assert act.parked_sessions("x") == ["chat-A"]
    # a probe that did not SEE session A leaves it alone; one that saw it clean clears it
    act.probe("x", {"chat-C": ("", "tC")}, probed_at=time.monotonic())
    assert act.parked_sessions("x") == ["chat-A"]
    # a probe read BEFORE the bus parked A is stale: it must not clear the park…
    act.probe("x", {"chat-A": ("", "tA")}, probed_at=t0 - 1)
    assert act.parked_sessions("x") == ["chat-A"]
    # …and a probe read before the bus answered it must not re-park
    act.apply(ev("x", "turn.resumed", context_id="chat-A", task_id="tA"))
    assert act.parked_sessions("x") == []
    act.probe("x", {"chat-A": ("waiting on you", "tA")}, probed_at=t0)
    assert act.parked_sessions("x") == []
    # a fresh probe is authoritative for what it saw
    act.probe("x", {"chat-A": ("waiting on you", "tA")}, probed_at=time.monotonic())
    assert act.parked_sessions("x") == ["chat-A"] and act.state["x"].parked["chat-A"].source == "probe"
    act.probe("x", {"chat-A": ("", "tA")}, probed_at=time.monotonic())
    assert act.parked_sessions("x") == []
    # a task the deck SETTLED (a redeemed plugin form the member keeps reporting parked) is
    # never re-parked by a probe, however many cycles later; a NEW task in that session is
    act.apply(ev("x", "turn.input_required", context_id="chat-F", task_id="tF", prompt="hello form"))
    act.unpark("x", "chat-F", settle_task="tF")
    for k in range(1, 6):
        act.probe("x", {"chat-F": ("waiting on you", "tF")}, probed_at=time.monotonic() + 30 * k)
        assert act.parked_sessions("x") == [] and act.ring_due() == [], f"cycle {k}"
    act.probe("x", {"chat-F": ("waiting on you", "tG")}, probed_at=time.monotonic() + 200)
    assert act.parked_sessions("x") == ["chat-F"]
    # clearing an already-clear session still stamps it: a probe that read before now cannot re-park
    act.unpark("x", "chat-F")
    act.unpark("x", "chat-F")
    stamp = act.state["x"].unparked["chat-F"]
    act.probe("x", {"chat-F": ("waiting on you", "tH")}, probed_at=stamp - 0.001)
    assert act.parked_sessions("x") == []
    # the bell re-checks: a park answered within the same drain never rings
    act.apply(ev("x", "turn.input_required", context_id="chat-A", task_id="tA", prompt="Approve?"))
    act.apply(ev("x", "turn.resumed", context_id="chat-A", task_id="tA"))
    assert act.ring_due() == []


def test_replayed_events_carry_no_time_unless_the_member_stamped_them():
    act = feed.Activity()
    act.apply(deckevents.Event(slug="x", topic="turn.usage", data={"task_id": "t", "context_id": "s", "state": "TASK_STATE_COMPLETED", "cost_usd": 1}, seq=1, replayed=True))
    assert act.rows[-1].at is None and act.last_active_cell("x") == ""
    act.apply(deckevents.Event(slug="x", topic="turn.usage", data={"task_id": "t2", "context_id": "s", "state": "TASK_STATE_COMPLETED"}, seq=2, replayed=True, ts=time.time() - 7200))
    assert act.rows[-1].at is not None and act.last_active_cell("x") == "2h ago"
    act.apply(ev("x", "turn.usage", task_id="t3", context_id="s", state="TASK_STATE_COMPLETED"))
    assert act.last_active_cell("x") == "0s ago"
    # usage for ONE task closes only that task's tools
    act.apply(ev("x", "chat.progress", session_id="s1", task_id="a", phase="tool_start", tool="t", tool_call_id="ca"))
    act.apply(ev("x", "chat.progress", session_id="s2", task_id="b", phase="tool_start", tool="t", tool_call_id="cb"))
    act.apply(ev("x", "turn.usage", task_id="a", context_id="s1", state="TASK_STATE_COMPLETED"))
    assert list(act.state["x"].open_tools) == ["cb"]


class FakeEvents:
    def __init__(self):
        self.pending: list[deckevents.Event] = []
        self.watched: list[list[str]] = []
        self.closed = False

    def watch(self, slugs):
        self.watched.append(list(slugs))

    def drain(self, limit=500):
        out, self.pending = self.pending[:limit], self.pending[limit:]
        return out

    def status(self):
        return {s: {"connected": True, "last_seq": 1, "error": ""} for s in (self.watched[-1] if self.watched else [])}

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_roster_turn_column_follows_the_bus_and_the_bell_rings_on_a_park():
    be = FakeBackend()
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    rings: list = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(130, 30)) as pilot:
        await _settle(app, pilot)
        assert fe.watched[-1] == ["host", "protoEngineer-ba4c", "old-1"]  # online + host only
        rows = app.screen.query_one("#roster", DataTable)
        assert str(rows.get_row_at(1)[3]) == "idle"
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id="s", task_id="t", phase="tool_start", tool="run_command", tool_call_id="c1"))
        await pilot.pause(0.7)
        assert str(app.screen.query_one("#roster", DataTable).get_row_at(1)[3]) == "⟳ running"
        fe.pending.append(ev("protoEngineer-ba4c", "turn.usage", task_id="t", context_id="s", state="TASK_STATE_COMPLETED", cost_usd=0.01))
        await pilot.pause(0.7)
        assert str(app.screen.query_one("#roster", DataTable).get_row_at(1)[3]) == "idle"
        assert "ago" in str(app.screen.query_one("#roster", DataTable).get_row_at(1)[8])
        # a park on the bus rings once, through the drain
        rings.clear()
        fe.pending.append(ev("protoEngineer-ba4c", "turn.input_required", context_id="chat-1", task_id="t9", prompt="Merge?"))
        await pilot.pause(0.7)
        assert rings == [1] and str(app.screen.query_one("#roster", DataTable).get_row_at(1)[3]) == "⚑ needs you"
        # a parked probe result from the poll: a session it saw parked rings; protoEngineer's
        # own park (a session the probe did not see) is untouched
        rings.clear()
        snap = be.snapshot()
        snap.parked = {"old-1": {"chat-9": ("waiting on you", "t9")}}
        snap.parked_at = time.monotonic()
        app._apply(snap)
        await pilot.pause(0.2)
        assert rings == [1]
        assert "⚑ 2 turns parked" in str(app.screen.query_one("#status", Static).content)
        snap.parked = {"old-1": {"chat-9": ("", "t9")}, "protoEngineer-ba4c": {"chat-1": ("", "t9")}}  # both seen clean
        snap.parked_at = time.monotonic()
        app._apply(snap)
        await pilot.pause(0.2)
        assert "parked" not in str(app.screen.query_one("#status", Static).content)
        await pilot.press("q")
    assert fe.closed


@pytest.mark.asyncio
async def test_work_feed_screen_lists_rows_filters_and_opens_the_member():
    be = FakeBackend()
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    async with app.run_test(size=(130, 34)) as pilot:
        await _settle(app, pilot)
        fe.pending.extend([
            ev("protoEngineer-ba4c", "chat.progress", session_id="chat-1", task_id="t", phase="tool_start", tool="run_command", tool_call_id="c1"),
            ev("old-1", "turn.usage", task_id="t2", context_id="chat-2", state="TASK_STATE_COMPLETED", cost_usd=0.2),
        ])
        await pilot.pause(0.7)
        await pilot.press("w")
        await pilot.pause(0.7)
        assert isinstance(app.screen, WorkFeedScreen)
        table = app.screen.query_one("#feed", DataTable)
        assert table.row_count == 2
        assert [str(table.get_row_at(i)[1]) for i in range(2)] == ["protoEngineer", "old"]
        assert "sse ● 3 of 3 members" in str(app.screen.query_one("#feed-head", Static).content)
        await pilot.press("f")
        await pilot.press(*"old", "enter")
        await pilot.pause(0.7)
        assert app.screen.query_one("#feed", DataTable).row_count == 1
        await pilot.press("escape")  # clears the filter
        await pilot.pause(0.7)
        assert app.screen.query_one("#feed", DataTable).row_count == 2
        # enter on the protoEngineer row opens its conversation at that session
        app.screen.query_one("#feed", DataTable).move_cursor(row=0)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen) and app.screen.convo.session_id == "chat-1"
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, WorkFeedScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)


@pytest.mark.asyncio
async def test_offline_has_no_feed():
    be = FakeBackend(mode="offline", roster=[{"name": "alpha", "id": "alpha-1", "port": 7901, "pid": None, "running": False}])
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        assert app.events is None
        await pilot.press("w")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)
