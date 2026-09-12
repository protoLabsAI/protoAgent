"""A leading RUN of mentions fans out (#3042 — "can't @ multiple members").

`@proto @reviewer <msg>` addresses both. The run extends while each next `@token`
resolves to a delegate; the first thing that doesn't begins the message — so a
mid-message `@` stays prose, exactly as before. Dispatch is SEQUENTIAL in written
order, which the room turns into a feature: the second addressee's catch-up already
contains the first one's reply.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

import runtime.state as rs

sc = importlib.import_module("server.chat")


class _Delegate:
    type = "acp"

    def __init__(self, name=""):
        self.url = f"http://127.0.0.1:78{len(name):02d}/a2a"


class _Reg:
    def __init__(self, names=("proto", "reviewer"), replies=None):
        self._names = list(names)
        self.replies = dict(replies or {})
        self.calls = []

    def names(self):
        return list(self._names)

    def roster(self):
        return [{"name": n, "type": "acp", "description": "", "url": ""} for n in self._names]

    def get(self, name):
        return _Delegate(name) if name in self._names else None

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append({"name": name, "query": query})
        return self.replies.get(name, f"{name} says hi")


@pytest.fixture
def wired(monkeypatch):
    reg = _Reg()
    monkeypatch.setattr(rs.STATE, "delegate_registry", reg, raising=False)
    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)  # dispatch survives no graph
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    return reg


# --- the run parser -----------------------------------------------------------


def test_two_mentions_fan_out(wired):
    assert sc._parse_at_delegates("@proto @reviewer what do you think?") == (
        ["proto", "reviewer"],
        "what do you think?",
    )


def test_one_mention_is_unchanged(wired):
    assert sc._parse_at_delegates("@proto fix it") == (["proto"], "fix it")


def test_an_unresolvable_token_past_the_first_begins_the_message(wired):
    """For tokens after the first, "doesn't resolve" means "is prose" — the same rule
    as a mid-message `@`, so a typo'd second name degrades to words, not an error."""
    assert sc._parse_at_delegates("@proto @nope hi") == (["proto"], "@nope hi")


def test_an_unknown_FIRST_token_keeps_the_roster_error_contract(wired):
    assert sc._parse_at_delegates("@nope hi") is None


def test_duplicates_deduplicate(wired):
    assert sc._parse_at_delegates("@proto @proto hi") == (["proto"], "hi")


def test_case_folds_to_the_registered_names(wired):
    assert sc._parse_at_delegates("@Proto @REVIEWER go") == (["proto", "reviewer"], "go")


# --- the exchange -------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_target_receives_the_message_in_written_order(wired):
    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?")
    assert [c["name"] for c in wired.calls] == ["proto", "reviewer"]
    assert all(c["query"].endswith("status?") for c in wired.calls)
    assert [o["author"] for o in outcomes] == ["proto", "reviewer"]


@pytest.mark.asyncio
async def test_the_combined_reply_attributes_each_participant(wired):
    wired.replies = {"proto": "line 40", "reviewer": "agreed"}
    reply, _ = await sc._at_delegate_exchange("@proto @reviewer status?")
    assert "**@proto** — line 40" in reply
    assert "**@reviewer** — agreed" in reply
    assert reply.index("@proto") < reply.index("@reviewer")  # written order


@pytest.mark.asyncio
async def test_a_single_mention_reply_is_not_suddenly_attributed(wired):
    """One participant answering reads as their answer, same as always — the attribution
    join is for when several voices would otherwise collapse into one."""
    wired.replies = {"proto": "line 40"}
    reply, _ = await sc._at_delegate_exchange("@proto status?")
    assert reply == "line 40"


@pytest.mark.asyncio
async def test_one_failure_does_not_cost_the_other_replies(wired):
    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        wired.calls.append({"name": name, "query": query})
        if name == "proto":
            raise RuntimeError("offline")
        return "agreed"

    wired.dispatch = _dispatch
    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?")
    assert "Delegate @proto failed" in reply and "offline" in reply
    assert "**@reviewer** — agreed" in reply
    assert [o["ok"] for o in outcomes] == [False, True]


@pytest.mark.asyncio
async def test_mixed_success_and_startable_failure_does_not_redispatch_the_success(wired, monkeypatch):
    """The explicit #3129 edge: @a @b with a up and b stopped stays on the direct
    path, otherwise falling through would ask the lead to dispatch to a twice."""
    from plugins.delegates.adapters import DelegateError, KIND_UNREACHABLE
    from plugins.delegates import autostart

    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        wired.calls.append({"name": name, "query": query})
        if name == "reviewer":
            raise DelegateError("offline", kind=KIND_UNREACHABLE)
        return "proto answered"

    wired.dispatch = _dispatch
    monkeypatch.setattr(
        autostart,
        "startable_member",
        lambda url: {"id": url, "name": url, "port": 1},
    )

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?")
    assert "proto answered" in reply
    assert "Delegate @reviewer failed: offline" in reply
    assert [call["name"] for call in wired.calls] == ["proto", "reviewer"]
    assert outcomes is not None


@pytest.mark.asyncio
async def test_every_startable_unreachable_target_falls_through_to_the_lead(wired, monkeypatch):
    from plugins.delegates.adapters import DelegateError, KIND_UNREACHABLE
    from plugins.delegates import autostart

    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        raise DelegateError(f"{name} is down", kind=KIND_UNREACHABLE)

    wired.dispatch = _dispatch
    monkeypatch.setattr(
        autostart,
        "startable_member",
        lambda url: {"id": url, "name": url, "port": 1},
    )

    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer status?")
    assert reply is None
    assert outcomes is None


@pytest.mark.asyncio
async def test_unreachable_remote_target_keeps_the_direct_error(wired, monkeypatch):
    from plugins.delegates.adapters import DelegateError, KIND_UNREACHABLE
    from plugins.delegates import autostart

    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        raise DelegateError("offline", kind=KIND_UNREACHABLE)

    wired.dispatch = _dispatch
    monkeypatch.setattr(autostart, "startable_member", lambda url: None)

    reply, outcomes = await sc._at_delegate_exchange("@proto status?")
    assert reply == "Delegate @proto failed: offline"
    assert outcomes and outcomes[0]["error_kind"] == KIND_UNREACHABLE


@pytest.mark.asyncio
async def test_a_bare_run_gets_one_usage_hint_naming_everyone(wired):
    reply, outcomes = await sc._at_delegate_exchange("@proto @reviewer")
    assert "@proto @reviewer" in reply and "add a message" in reply.lower()
    assert outcomes is None


@pytest.mark.asyncio
async def test_the_streaming_driver_emits_one_frame_per_exchange(wired, monkeypatch):
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    async def _fake(message, session_id="", request_metadata=None):
        return "combined", [
            {"author": "proto", "ok": True, "reply": "line 40", "catchup": 0, "truncated": False},
            {"author": "reviewer", "ok": True, "reply": "agreed", "catchup": 2, "truncated": False},
        ]

    monkeypatch.setattr(sc, "_at_delegate_exchange", _fake)
    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto @reviewer status?", "s1")]
    stamps = [dict(p) for k, p in frames if k == "room_reply"]
    assert [(x["author"], x["text"]) for x in stamps] == [("proto", "line 40"), ("reviewer", "agreed")]
    assert frames[-1] == ("done", "combined")


@pytest.mark.asyncio
async def test_addressed_turn_opens_live_work_before_delegate_finishes(wired, monkeypatch):
    """#3052: a slow direct address must not leave the console on a bare spinner.

    Prove temporal order, not merely frame membership: the first frame is observable
    while the delegate exchange is still blocked. Its running card supplies the live
    elapsed timer for adapters that cannot expose finer-grained progress.
    """
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    release = asyncio.Event()

    async def _slow(message, session_id="", request_metadata=None):
        await release.wait()
        return "line 40", [
            {"author": "proto", "ok": True, "reply": "line 40", "catchup": 0, "truncated": False}
        ]

    monkeypatch.setattr(sc, "_at_delegate_exchange", _slow)
    stream = sc._chat_langgraph_stream_impl("@proto status?", "s-progress")

    assert await anext(stream) == (
        "tool_start",
        {"id": "mention:proto", "name": "@proto", "input": "status?"},
    )
    release.set()
    remaining = [frame async for frame in stream]

    assert remaining[0] == (
        "tool_end",
        {"id": "mention:proto", "name": "@proto", "output": "1 replied", "error": False},
    )
    assert remaining[1][0] == "room_reply"
    assert remaining[-1] == ("done", "line 40")


@pytest.mark.asyncio
async def test_the_card_counts_participants_not_dispatches(wired, monkeypatch):
    """A multi-round room produces one outcome per participant PER ROUND. Summing them
    renders "4 replied over 2 rounds" for a room of two people, which describes a cast
    that does not exist — the round count is already the other half of the sentence."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    async def _two_rounds(message, session_id="", request_metadata=None):
        return "combined", [
            {"author": "proto", "ok": True, "reply": "a", "round": 1, "catchup": 0, "truncated": False},
            {"author": "reviewer", "ok": True, "reply": "b", "round": 1, "catchup": 0, "truncated": False},
            {"author": "proto", "ok": True, "reply": "c", "round": 2, "catchup": 1, "truncated": False},
            {"author": "reviewer", "ok": True, "reply": "d", "round": 2, "catchup": 1, "truncated": False},
        ]

    monkeypatch.setattr(sc, "_at_delegate_exchange", _two_rounds)
    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto @reviewer status?", "s-card")]
    ends = [p for k, p in frames if k == "tool_end"]
    assert ends[0]["output"] == "2 replied over 2 rounds"


@pytest.mark.asyncio
async def test_bare_address_does_not_flash_a_work_card(wired, monkeypatch):
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    frames = [frame async for frame in sc._chat_langgraph_stream_impl("@proto", "s-bare")]
    assert not [frame for frame in frames if frame[0] in ("tool_start", "tool_end")]
    assert frames[-1][0] == "done"


@pytest.mark.asyncio
async def test_unexpected_address_failure_settles_work_card(wired, monkeypatch):
    """An exception outside normal adapter error conversion must not strand the card."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    async def _boom(message, session_id="", request_metadata=None):
        raise RuntimeError("dispatch machinery broke")

    monkeypatch.setattr(sc, "_at_delegate_exchange", _boom)
    frames = [frame async for frame in sc._chat_langgraph_stream_impl("@proto status?", "s-failed")]

    assert frames[:2] == [
        ("tool_start", {"id": "mention:proto", "name": "@proto", "input": "status?"}),
        (
            "tool_end",
            {
                "id": "mention:proto",
                "name": "@proto",
                "output": "dispatch machinery broke",
                "error": True,
            },
        ),
    ]
    assert frames[-1] == ("error", "dispatch machinery broke")


# --- the answer says which replies it restates (#3449) ------------------------
#
# An `@`-address short-circuits the lead, so ONE answer goes out TWICE over: a
# `room_reply` frame per exchange carrying that participant's own words, and the
# terminal `done` text composed from those very replies for consumers that get no room
# frames at all (A2A, `/v1`). A console that renders both drew the answer twice,
# verbatim — the v0.164.0 report.
#
# The split is PER EXCHANGE, because the rendering is: `in_answer` marks each exchange
# whose bubble carries what the answer says for it, and whatever the answer says beyond
# those bubbles rides its own `note` frame. Claiming per TURN instead — dropping the
# claim whenever the answer carried anything extra — left the reported symptom alive on
# DEFAULT config (a long chat truncates its catch-up, appends a note), which is what the
# `room_note` tests below pin.


@pytest.mark.asyncio
async def test_a_single_address_declares_that_the_answer_restates_its_reply(wired, monkeypatch):
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    wired.replies = {"proto": "line 40"}

    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto status?", "s-claim")]
    rooms = [p for k, p in frames if k == "room_reply"]

    assert [r["text"] for r in rooms] == ["line 40"]
    assert rooms[0]["in_answer"] is True
    assert rooms[0]["from"] == "operator"  # the console honours a claim only from the operator
    # …and the claim is TRUE: the answer is that reply and nothing else, so a consumer
    # that drew the bubble has already shown every word of it.
    assert frames[-1] == ("done", "line 40")


@pytest.mark.asyncio
async def test_a_multi_address_join_still_declares_each_reply(wired, monkeypatch):
    """The attribution the join adds (`**@proto** — `) IS the byline the authored bubble
    already renders, so it is not content the answer holds alone."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    wired.replies = {"proto": "line 40", "reviewer": "agreed"}

    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto @reviewer status?", "s-claim2")]
    rooms = [p for k, p in frames if k == "room_reply"]

    assert [(r["author"], r["text"], r["in_answer"]) for r in rooms] == [
        ("proto", "line 40", True),
        ("reviewer", "agreed", True),
    ]
    assert frames[-1] == ("done", "**@proto** — line 40\n\n**@reviewer** — agreed")


@pytest.mark.asyncio
async def test_a_failed_address_rides_its_own_note_frame_and_the_rest_is_claimed(wired, monkeypatch):
    """`@a @b` with one member offline — the reviewer's case. b's failure line lives only
    in the answer, so it goes out as the ROOM's note; a's reply is still claimed, so it
    renders once. Claiming per TURN dropped a's claim and doubled its reply."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        if name == "proto":
            raise RuntimeError("offline")
        return "agreed"

    wired.dispatch = _dispatch
    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto @reviewer status?", "s-claim3")]
    rooms = [p for k, p in frames if k == "room_reply"]

    replies = [r for r in rooms if r.get("author")]
    notes = [r for r in rooms if r.get("note")]
    assert [(r["author"], r["text"], r.get("in_answer")) for r in replies] == [
        ("proto", "", None),  # a failed address has no words of its own to show
        ("reviewer", "agreed", True),
    ]
    assert len(notes) == 1 and notes[0]["text"] == "**@proto** — Delegate @proto failed: offline"
    # Every word of the answer is on screen exactly once: the claimed bubble, the note.
    assert frames[-1][1] == "**@proto** — Delegate @proto failed: offline\n\n**@reviewer** — agreed"


@pytest.mark.asyncio
async def test_an_empty_reply_rides_the_note_frame(wired, monkeypatch):
    """A delegate that answers with nothing: the answer's stand-in line is in no bubble,
    so it is the note — and with NOTHING claimed there is no note frame at all, because
    the console lands the answer whole and the frame would be the duplicate."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    wired.replies = {"proto": "   "}

    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto status?", "s-claim4")]
    rooms = [p for k, p in frames if k == "room_reply"]

    assert len(rooms) == 1 and "in_answer" not in rooms[0]  # byline only, no claim
    assert not [r for r in rooms if r.get("note")]
    assert frames[-1] == ("done", "@proto replied with nothing.")


@pytest.mark.asyncio
async def test_a_truncated_catchup_is_claimed_and_its_note_rides_a_frame(wired, monkeypatch):
    """The reported symptom's own shape (#3449): an ordinary long chat truncates the
    catch-up on DEFAULT caps, which appends a room note. Dropping the claim for the whole
    turn left that case doubling; the note gets its own frame instead."""
    from langchain_core.messages import AIMessage, HumanMessage

    from graph.config import LangGraphConfig

    class _Graph:
        def __init__(self, messages):
            self.messages = list(messages)

        async def aget_state(self, config):
            return type("S", (), {"values": {"messages": list(self.messages)}})()

        async def aupdate_state(self, config, update, *, as_node=None):
            self.messages.extend(update["messages"])

    history = []
    for i in range(60):
        history.append(HumanMessage(content=f"operator line {i}"))
        history.append(AIMessage(content=f"lead line {i}"))
    monkeypatch.setattr(rs.STATE, "graph", _Graph(history), raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)  # DEFAULTS
    wired.replies = {"proto": "line 40"}

    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto status?", "s-longroom")]
    rooms = [p for k, p in frames if k == "room_reply"]
    replies = [r for r in rooms if r.get("author")]
    notes = [r for r in rooms if r.get("note")]

    assert replies[0]["truncated"] is True  # the default caps really did clip the window
    assert replies[0]["in_answer"] is True  # …and the reply is still claimed
    assert len(notes) == 1
    assert "left out of the catch-up for @proto" in notes[0]["text"]
    assert "room.catchup_max_messages" in notes[0]["text"]
    # The answer still carries the whole thing for consumers with no room frames.
    assert frames[-1][1].startswith("line 40")
    assert "left out of the catch-up" in frames[-1][1]


@pytest.mark.asyncio
async def test_the_note_frame_is_ordered_last(wired, monkeypatch):
    """It is a footnote on what was just said, so it must not precede the bubbles it
    annotates — the console inserts room frames in arrival order."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    async def _dispatch(name, query, *, conversation_key=None, permissions=None):
        if name == "reviewer":
            raise RuntimeError("offline")
        return "line 40"

    wired.dispatch = _dispatch
    frames = [f async for f in sc._chat_langgraph_stream_impl("@proto @reviewer status?", "s-order")]
    kinds = [("note" if p.get("note") else "reply") for k, p in frames if k == "room_reply"]
    assert kinds == ["reply", "reply", "note"]


def test_a_reply_with_no_words_is_not_covered_by_a_bubble():
    """The coverage test the claim is built on, in isolation: a bubble the console will
    render, whose text is what the answer says for that exchange. Both halves matter —
    a failed exchange's answer line is the failure, not any reply it might carry."""
    assert sc._covered_by_a_bubble({"ok": True, "reply": "line 40"}) is True
    assert sc._covered_by_a_bubble({"ok": True, "reply": "   "}) is False
    assert sc._covered_by_a_bubble({"ok": True, "reply": ""}) is False
    assert sc._covered_by_a_bubble({"ok": False, "reply": "line 40"}) is False
    assert sc._covered_by_a_bubble({}) is False
