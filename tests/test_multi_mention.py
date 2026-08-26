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
