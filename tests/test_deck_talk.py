"""deck.talk — the conversation screen (#3469) driven by the pilot over a fake backend
whose A2A client replays canned frames: send/stream/tool cards/cost, cancel, a session
replay with tool cards from durable history, the session picker, the reasoning fold, and
the offline refusal."""

from __future__ import annotations

import time

import pytest
from textual.widgets import Input, Markdown, Static, Tree

from deck import a2a
from deck import data as deckdata
from deck.app import DetailScreen, FleetDeck, RosterScreen
from deck.talk import ConversationScreen, PagerScreen, SessionPicker
from tests.test_deck_app import ROSTER, FakeBackend, _settle

TOOL = a2a.TOOL_CALL_EXT_URI
COST = a2a.COST_EXT_URI


def status(state="TASK_STATE_WORKING", *, parts=None, metadata=None, final=False, cid="s"):
    msg: dict = {"role": "ROLE_AGENT", "messageId": "m", "parts": parts or []}
    if metadata:
        msg["metadata"] = metadata
    return {"result": {"statusUpdate": {"taskId": "t1", "contextId": cid, "status": {"state": state, "message": msg}, "final": final}}}


def artifact(text, *, append=None, last=False, metadata=None, cid="s"):
    upd: dict = {"taskId": "t1", "contextId": cid, "artifact": {"artifactId": "a", "parts": [{"text": text}], **({"metadata": metadata} if metadata else {})}}
    if append is not None:
        upd["append"] = append
    if last:
        upd["lastChunk"] = True
    return {"result": {"artifactUpdate": upd}}


def canned_frames(cid: str) -> list[dict]:
    return [
        {"result": {"task": {"id": "t1", "contextId": cid, "status": {"state": "TASK_STATE_SUBMITTED"}}}},
        status(parts=[{"data": {"text": "let me look"}, "metadata": {"mimeType": a2a.REASONING_MIME}}], cid=cid),
        status(metadata={TOOL: {"toolCallId": "c1", "name": "task", "phase": "started", "args": "board_triage"}}, cid=cid),
        status(metadata={TOOL: {"toolCallId": "c2", "name": "run_command", "phase": "started", "args": "gh pr list", "parentToolCallId": "c1"}}, cid=cid),
        status(metadata={TOOL: {"toolCallId": "c2", "name": "run_command", "phase": "completed", "result": "3 open", "outputChars": 120}}, cid=cid),
        status(metadata={TOOL: {"toolCallId": "c1", "name": "task", "phase": "completed", "result": "done"}}, cid=cid),
        artifact("Three PRs are ", append=True, cid=cid),
        artifact("open.", append=True, cid=cid),
        artifact("Three PRs are open.", last=True, metadata={COST: {"usage": {"input_tokens": 900, "output_tokens": 40}, "costUsd": 0.0042, "durationMs": 3100}}, cid=cid),
        status("TASK_STATE_COMPLETED", final=True, cid=cid),
    ]


class FakeA2A:
    """A canned A2A client. `hang=True` blocks before the last frame until cancel() or
    abort() (bounded at 5 s so a regression fails instead of hanging the suite);
    `block=True` yields the first N frames then blocks until abort() and raises like a
    closed socket — the shape of a stalled/half-open stream."""

    def __init__(self, frames=None, *, hang=False, block_after=None, task_state="TASK_STATE_COMPLETED", sub_frames=None):
        self.frames, self.hang, self.block_after = frames, hang, block_after
        self.task_state = task_state
        self.sub_frames = sub_frames  # what SubscribeToTask replays (default: the canned turn)
        self.sent: list[dict] = []
        self.subscribed: list[str] = []
        self.cancelled: list[str] = []
        self.aborted = False
        self.closed = False
        self.exited = False

    def _wait(self, until):
        import time

        deadline = time.monotonic() + 5.0
        while not until() and time.monotonic() < deadline:
            time.sleep(0.02)
        return until()

    def stream(self, text, *, context_id, task_id=None, metadata=None):
        self.sent.append({"text": text, "context_id": context_id, "task_id": task_id, "metadata": metadata})
        frames = self.frames if self.frames is not None else canned_frames(context_id)
        try:
            for i, f in enumerate(frames):
                if self.block_after is not None and i == self.block_after:
                    if self._wait(lambda: self.aborted):
                        from deck import hub as deckhub

                        raise deckhub.HubUnreachable("http://127.0.0.1:7870", "stream closed (abort)")
                    raise AssertionError("blocked stream was never aborted within 5s")
                if self.hang and f is frames[-1]:
                    if not self._wait(lambda: self.cancelled or self.aborted):
                        raise AssertionError("hanging stream was never cancelled within 5s")
                    yield status("TASK_STATE_CANCELED", final=True, cid=context_id)
                    return
                yield f
        finally:
            self.exited = True

    def subscribe(self, task_id):
        """SubscribeToTask. With `hang=True` the last frame is held until cancel() (→ a
        canceled frame) or abort() (→ raises like the shut socket it really is)."""
        self.subscribed.append(task_id)
        frames = self.sub_frames if self.sub_frames is not None else canned_frames("s")
        try:
            for f in frames:
                if self.hang and f is frames[-1]:
                    if not self._wait(lambda: self.cancelled or self.aborted):
                        raise AssertionError("hanging subscription was never cancelled within 5s")
                    if self.aborted and not self.cancelled:
                        from deck import hub as deckhub

                        raise deckhub.HubUnreachable("http://127.0.0.1:7870", "stream closed (abort)")
                    yield status("TASK_STATE_CANCELED", final=True, cid="s")
                    return
                yield f
        finally:
            self.exited = True

    def abort(self):
        self.aborted = True

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return {}

    def get_task(self, task_id):
        return {"id": task_id, "status": {"state": self.task_state}, "artifacts": [{"parts": [{"text": "finalized from the task"}]}]}

    def close(self):
        self.closed = True


class TalkBackend(FakeBackend):
    def __init__(self, *a, a2a_client=None, sessions=None, turns=None, **kw):
        super().__init__(*a, **kw)
        self._a2a = a2a_client or FakeA2A()
        self._sessions = sessions or []
        self._turns = turns or {}
        self.turn_reads: list[str] = []

    def sessions(self, agent):
        return list(self._sessions)

    turns_delay: dict[str, float] = {}

    def turns(self, agent, session_id, limit=50):
        import time

        self.turn_reads.append(session_id)
        if session_id in self.turns_delay:
            time.sleep(self.turns_delay[session_id])
        return list(self._turns.get(session_id, []))

    def a2a(self, agent):
        return self._a2a


async def _open_talk(be, pilot, app):
    await _settle(app, pilot)
    await pilot.press("j", "enter")  # protoEngineer, online
    await _settle(app, pilot)
    assert isinstance(app.screen, ConversationScreen)


async def _until(pilot, cond, timeout=4.0):
    """Poll the UI loop until `cond()` holds (a fixed pause races the worker threads)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


async def _send(app, pilot, text):
    comp = app.screen.query_one("#composer", Input)
    comp.focus()
    comp.value = text
    await pilot.press("enter")
    await _settle(app, pilot)


@pytest.mark.asyncio
async def test_enter_on_an_online_member_opens_the_conversation_and_streams_a_turn():
    be = TalkBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        assert app.screen.convo.session_id.startswith("chat-")
        await _send(app, pilot, "status?")
        sent = be._a2a.sent[0]
        assert sent["text"] == "status?" and sent["context_id"] == app.screen.convo.session_id
        # transcript: the user line, the meta line (tools + folded thinking), the answer
        mds = app.screen.query(Markdown)
        assert len(mds) == 1 and "Three PRs are open." in str(mds.first().source)
        meta = str(app.screen.query(".turn-meta").first().content)
        assert "▸ thinking · 11 chars" in meta and "⚙ 1 tool call" in meta and "⟳" not in meta
        # work tree: the task card with its nested subagent call
        tree = app.screen.query_one("#work-tree", Tree)
        top = list(tree.root.children)
        assert len(top) == 1 and top[0].data.name == "task" and top[0].data.status == "done"
        assert [c.data.name for c in top[0].children] == ["run_command"]
        assert "$0.0042" in str(app.screen.query_one("#cost", Static).content)
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)
        assert "1 turn" in str(app.screen.query_one("#talk-head", Static).content)


@pytest.mark.asyncio
async def test_esc_cancels_a_running_turn_then_backs_out():
    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "go"
        await pilot.press("enter")
        assert await _until(pilot, lambda: app.screen.convo.live is not None and app.screen.convo.live.turn.task_id == "t1")
        assert "⟳" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("escape")
        assert await _until(pilot, lambda: fake.cancelled == ["t1"] and app.screen.convo.live is None)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, RosterScreen)


@pytest.mark.asyncio
async def test_a_second_message_while_working_steers_the_turn_not_a_new_one():
    """A message typed while the member works is QUEUED into the running turn (#3470),
    never sent as a competing turn; the transcript shows it queued."""
    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "one"
        await pilot.press("enter")
        await pilot.pause(0.3)
        comp.value = "two"
        await pilot.press("enter")
        assert await _until(pilot, lambda: any(c[0] == "steer" for c in be.calls))
        assert [s["text"] for s in fake.sent] == ["one"]
        steer = next(c for c in be.calls if c[0] == "steer")
        assert steer[1] == app.screen.convo.session_id and steer[3] == "two"
        assert "queued" in str(app.screen.query(".steer-msg").first().content)
        assert "1 queued" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("escape")
        await _settle(app, pilot)


@pytest.mark.asyncio
async def test_session_picker_replays_durable_turns_with_tool_cards():
    row = {
        "task_id": "old1",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "Yesterday's answer."}]}],
        "history": [
            {"role": "ROLE_USER", "parts": [{"text": "yesterday's question"}]},
            {"role": "ROLE_AGENT", "parts": [], "metadata": {TOOL: {"toolCallId": "m1", "name": "@sonnet", "phase": "started", "args": "report"}}},
            {"role": "ROLE_AGENT", "parts": [], "metadata": {TOOL: {"toolCallId": "m1", "name": "@sonnet", "phase": "completed", "result": "1 replied"}}},
        ],
    }
    be = TalkBackend(sessions=[{"session_id": "chat-1-old", "turn_count": 1, "last_updated": "2026-09-11T23:29:04"}], turns={"chat-1-old": [row]})
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert isinstance(app.screen, SessionPicker)
        await pilot.press("enter")  # the only (first) session
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen)
        assert app.screen.convo.session_id == "chat-1-old" and be.turn_reads[-1] == "chat-1-old"
        assert "yesterday's question" in str(app.screen.query(".user-msg").first().content)
        tree = app.screen.query_one("#work-tree", Tree)
        assert [n.data.name for n in tree.root.children] == ["@sonnet"] and tree.root.children[0].data.output == "1 replied"
        # enter on the card → pager with args and result
        tree.focus()
        tree.select_node(tree.root.children[0])
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PagerScreen)
        assert "1 replied" in str(app.screen.query(".pager-block")[1].content)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        # ctrl+n → a fresh session
        await pilot.press("ctrl+n")
        await _settle(app, pilot)
        assert app.screen.convo.session_id != "chat-1-old" and app.screen.convo.exchanges == []


@pytest.mark.asyncio
async def test_reasoning_fold_toggles_the_thinking_text():
    be = TalkBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "hi")
        assert "▸ thinking" in str(app.screen.query(".turn-meta").first().content)
        await pilot.press("ctrl+z")
        await pilot.pause(0.2)
        assert "▾ thinking" in str(app.screen.query(".turn-meta").first().content)


@pytest.mark.asyncio
async def test_talk_refuses_offline_and_stopped_members():
    be = TalkBackend(mode="offline")
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)
    be = TalkBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "j", "j", "enter")  # Cindi, stopped
        await pilot.pause(0.2)
        assert isinstance(app.screen, RosterScreen)
        await pilot.press("i")  # detail still opens, and `c` from there is refused too
        await _settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DetailScreen)


@pytest.mark.asyncio
async def test_a_parked_question_is_surfaced_in_the_status_line():
    class Parked(FakeA2A):
        def stream(self, text, *, context_id, task_id=None, metadata=None):
            yield status("TASK_STATE_INPUT_REQUIRED", parts=[{"text": "Merge this PR?"}, {"data": {"question": "Merge this PR?"}, "metadata": {"mimeType": a2a.HITL_MIME}}], cid=context_id)

    be = TalkBackend(a2a_client=Parked())
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "ship it")
        st = str(app.screen.query_one("#talk-status", Static).content)
        assert "needs you (question): Merge this PR?" in st and "ctrl+r" in st


@pytest.mark.asyncio
async def test_stall_finalizes_from_the_task_and_unblocks_the_reader(monkeypatch):
    """Review HIGH-1: the watchdog finalized the Turn but never closed the stream, so the
    reader thread stayed blocked (and the process could not exit). Now the probe aborts
    the stream first; the worker unwinds through its except path and the client closes."""
    from deck import talk as talkmod

    monkeypatch.setattr(talkmod, "STALL_IDLE_S", 0.2)
    fake = FakeA2A(block_after=3)  # Task frame + reasoning + one tool start, then silence
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "go"
        await pilot.press("enter")
        # the Task frame must have landed (task_id set) and the idle window elapsed, or the
        # watchdog takes the no-frame branch instead of the durable-task one
        assert await _until(pilot, lambda: app.screen.convo.live is not None and app.screen.convo.live.turn.task_id == "t1")
        assert await _until(pilot, lambda: time.monotonic() - app.screen.convo.live.turn.last_frame_at >= talkmod.STALL_IDLE_S)
        app.screen._check_stall()  # the 5 s timer, driven by hand
        await _settle(app, pilot)
        ex = app.screen.convo.exchanges[-1]
        assert fake.aborted and fake.exited and fake.closed
        assert ex.turn.done and not ex.live and ex.error == ""
        assert "finalized from the task" in ex.turn.content
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)


@pytest.mark.asyncio
async def test_a_member_that_sends_nothing_is_aborted_after_the_window(monkeypatch):
    from deck import talk as talkmod

    monkeypatch.setattr(talkmod, "STALL_IDLE_S", 0.2)
    fake = FakeA2A(block_after=0, task_state="TASK_STATE_WORKING")  # accepted the POST, produced nothing
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "go"
        await pilot.press("enter")
        await pilot.pause(0.4)
        app.screen._check_stall()
        await _settle(app, pilot)
        ex = app.screen.convo.exchanges[-1]
        assert fake.aborted and fake.exited and not ex.live and ex.error


@pytest.mark.asyncio
async def test_read_timeout_consults_the_task_before_failing():
    """A half-open connection surfaces as StreamStalled from the client; the worker asks
    GetTask and finalizes only if the server finished."""
    from deck import a2a as a2amod

    class Stalls(FakeA2A):
        def stream(self, text, *, context_id, task_id=None, metadata=None):
            self.sent.append({"text": text})
            yield {"result": {"task": {"id": "t1", "contextId": context_id, "status": {"state": "TASK_STATE_SUBMITTED"}}}}
            raise a2amod.StreamStalled("http://127.0.0.1:7870", "no frame for 60s")

        def subscribe(self, task_id):
            # still silent after re-attaching: every window stalls again
            raise a2amod.StreamStalled("http://127.0.0.1:7870", "no frame for 60s")
            yield  # pragma: no cover — makes this a generator

    be = TalkBackend(a2a_client=Stalls())
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "go")
        ex = app.screen.convo.exchanges[-1]
        assert ex.turn.done and "finalized from the task" in ex.turn.content and not ex.error
    # a task that is WORKING but never produces a frame: re-attach up to the cap, then fail
    from deck import talk as talkmod

    be = TalkBackend(a2a_client=Stalls(task_state="TASK_STATE_WORKING"))
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "go")
        ex = app.screen.convo.exchanges[-1]
        assert app.screen.reconnects == talkmod.MAX_RECONNECTS
        assert not ex.turn.done and "timed out" in ex.error and not ex.live


@pytest.mark.asyncio
async def test_a_silent_stretch_on_a_working_turn_re_attaches_instead_of_failing():
    """Round-2 note: a >60 s silent tool call must not fail the exchange while the server
    keeps working — the deck re-attaches with SubscribeToTask and carries on."""
    from deck import a2a as a2amod

    class Quiet(FakeA2A):
        def __init__(self):
            super().__init__()
            self.subscribed: list[str] = []

        def stream(self, text, *, context_id, task_id=None, metadata=None):
            self.sent.append({"text": text})
            frames = canned_frames(context_id)
            yield frames[0]  # the Task frame (task_id known)
            yield frames[2]  # a tool started…
            raise a2amod.StreamStalled("http://127.0.0.1:7870", "no frame for 60s")

        def get_task(self, task_id):
            return {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}

        def subscribe(self, task_id):
            self.subscribed.append(task_id)
            frames = canned_frames("s")
            # the snapshot first (history so far), then the rest of the live frames
            yield {"result": {"task": {"id": task_id, "contextId": self.sent and self.sent[-1].get("cid") or "s", "status": {"state": "TASK_STATE_WORKING"}, "history": [], "artifacts": []}}}
            yield from frames[3:]

    fake = Quiet()
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        # the fake's subscribe frames carry contextId "s"; make the session match
        app.screen._apply_session("s", [], None)
        await _send(app, pilot, "go")
        ex = app.screen.convo.exchanges[-1]
        assert fake.subscribed == ["t1"] and app.screen.reconnects == 1
        assert ex.turn.done and not ex.error and "Three PRs are open." in ex.turn.content
        assert [c.name for c in ex.turn.tool_calls] == ["task", "run_command"]


@pytest.mark.asyncio
async def test_a_session_load_never_orphans_a_live_turn():
    """Review HIGH-2: a slow turns() fetch landing after a send replaced the conversation,
    the live exchange vanished (status "idle"), and a second stream could start."""
    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    be.turns_delay = {}
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        # while a session load is in flight, sending is refused
        sid = app.screen.convo.session_id
        be.turns_delay[sid] = 0.6
        app.screen.load_session(sid)
        await pilot.pause(0.1)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "too early"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert fake.sent == []
        await _settle(app, pilot)
        # a load that lands while a turn streams is dropped, not applied
        comp.value = "one"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.screen.convo.live is not None
        app.screen._apply_session("chat-other", [], None)
        assert app.screen.convo.live is not None and app.screen.convo.session_id == sid
        assert "⟳" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("escape")
        await _settle(app, pilot)


@pytest.mark.asyncio
async def test_a_stale_session_load_is_ignored():
    be = TalkBackend(turns={"chat-A": [], "chat-B": []})
    be.turns_delay = {"chat-A": 0.5, "chat-B": 0.0}
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        app.screen.load_session("chat-A")
        await pilot.pause(0.05)
        app.screen.load_session("chat-B")
        await _settle(app, pilot)
        await pilot.pause(0.6)
        await _settle(app, pilot)
        assert app.screen.convo.session_id == "chat-B"  # A landed later but was superseded


@pytest.mark.asyncio
async def test_second_escape_abandons_a_turn_that_will_not_stop():
    class Stubborn(FakeA2A):
        def cancel(self, task_id):
            raise RuntimeError("member unreachable")

    fake = Stubborn(hang=True)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        comp = app.screen.query_one("#composer", Input)
        comp.value = "go"
        await pilot.press("enter")
        assert await _until(pilot, lambda: app.screen.convo.live is not None and app.screen.convo.live.turn.task_id == "t1")
        await pilot.press("escape")  # cancel fails; the stream is (deliberately) still hanging
        assert await _until(pilot, lambda: "abandons" in str(app.screen.query_one("#talk-status", Static).content))
        assert isinstance(app.screen, ConversationScreen)
        await pilot.press("escape")  # abandon: abort the reader, leave
        assert await _until(pilot, lambda: isinstance(app.screen, RosterScreen))
        assert await _until(pilot, lambda: fake.aborted and fake.exited)


def test_sessions_and_turns_read_through_the_slug_proxy():
    import httpx

    from deck import hub

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/api/chat/sessions"):
            return httpx.Response(200, json={"sessions": [{"session_id": "chat-1", "turn_count": 2}, {"bad": 1}]})
        return httpx.Response(200, json={"turns": [{"task_id": "t"}]})

    client = hub.HubClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(handler))
    conn = hub.Connection(client=client, candidate=hub.HubCandidate(client.url, "flag"), card={}, roster=list(ROSTER))
    be = deckdata.LiveBackend(conn)
    assert be.sessions(ROSTER[1]) == [{"session_id": "chat-1", "turn_count": 2}]
    assert be.turns(ROSTER[1], "chat-1") == [{"task_id": "t"}]
    assert seen == ["/agents/protoEngineer-ba4c/api/chat/sessions", "/agents/protoEngineer-ba4c/api/chat/sessions/chat-1/turns"]
    assert be.a2a(ROSTER[1]).endpoint == "http://127.0.0.1:7870/agents/protoEngineer-ba4c/a2a"
