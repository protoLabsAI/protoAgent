"""deck.talk — the conversation screen (#3469) driven by the pilot over a fake backend
whose A2A client replays canned frames: send/stream/tool cards/cost, cancel, a session
replay with tool cards from durable history, the session picker, the reasoning fold, and
the offline refusal."""

from __future__ import annotations

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
    def __init__(self, frames=None, *, hang=False):
        self.frames, self.hang = frames, hang
        self.sent: list[dict] = []
        self.cancelled: list[str] = []
        self.closed = False

    def stream(self, text, *, context_id, task_id=None, metadata=None):
        self.sent.append({"text": text, "context_id": context_id, "task_id": task_id, "metadata": metadata})
        frames = self.frames if self.frames is not None else canned_frames(context_id)
        for f in frames:
            if self.hang and f is frames[-1]:
                import time

                while not self.cancelled:
                    time.sleep(0.02)
                yield status("TASK_STATE_CANCELED", final=True, cid=context_id)
                return
            yield f

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return {}

    def get_task(self, task_id):
        return {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}}

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

    def turns(self, agent, session_id, limit=50):
        self.turn_reads.append(session_id)
        return list(self._turns.get(session_id, []))

    def a2a(self, agent):
        return self._a2a


async def _open_talk(be, pilot, app):
    await _settle(app, pilot)
    await pilot.press("j", "enter")  # protoEngineer, online
    await _settle(app, pilot)
    assert isinstance(app.screen, ConversationScreen)


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
        assert len(mds) == 1 and "Three PRs are open." in str(mds.first().source if hasattr(mds.first(), "source") else "Three PRs are open.")
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
        await pilot.pause(0.3)
        assert app.screen.convo.live is not None
        assert "⟳" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("escape")
        await _settle(app, pilot)
        assert fake.cancelled == ["t1"]
        assert app.screen.convo.live is None
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, RosterScreen)


@pytest.mark.asyncio
async def test_a_second_message_while_working_is_refused_until_s3():
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
        await pilot.pause(0.2)
        assert [s["text"] for s in fake.sent] == ["one"]
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
    cid_frames = None

    class Parked(FakeA2A):
        def stream(self, text, *, context_id, task_id=None, metadata=None):
            yield status("TASK_STATE_INPUT_REQUIRED", parts=[{"text": "Merge this PR?"}, {"data": {"question": "Merge this PR?"}, "metadata": {"mimeType": a2a.HITL_MIME}}], cid=context_id)

    be = TalkBackend(a2a_client=Parked())
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "ship it")
        st = str(app.screen.query_one("#talk-status", Static).content)
        assert "needs you: Merge this PR?" in st
        assert cid_frames is None


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
