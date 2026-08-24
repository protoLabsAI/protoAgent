"""A `delegate_to` call is the orchestrator addressing a participant (#3042).

`delegate_to` is a declared intent — a target and a query — so the room needs no prose
parsing to know a delegation happened. Recording it is what makes the transcript honest:
a later `@` catches that participant up on it, and the next turn's orchestrator reads its
own past delegations as conversation rather than tool output it must remember paraphrasing.

The invariant these protect: **the reply is the job, the record is bookkeeping.** Every
degraded path still returns the delegate's answer.
"""

from __future__ import annotations

import pytest
from langgraph.constants import START

import runtime.state as rs
from plugins.delegates import _dispatch_into_room


class _Delegate:
    type = "acp"


class _Registry:
    def __init__(self, reply="line 40", raises=None):
        self.reply, self.raises = reply, raises
        self.calls = []

    def get(self, name):
        return _Delegate()

    def names(self):
        return ["proto"]

    async def dispatch(self, name, query, **kw):
        self.calls.append({"name": name, "query": query, **kw})
        if self.raises:
            raise self.raises
        return self.reply


class _Graph:
    def __init__(self):
        self.written = []

    async def aget_state(self, config):
        return type("S", (), {"values": {"messages": []}})()

    async def aupdate_state(self, config, update, *, as_node=None):
        assert as_node == START
        self.written.append((config, update))


@pytest.fixture
def wired(monkeypatch):
    graph = _Graph()
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    monkeypatch.setattr("tools.lg_tools._session_id_from", lambda state: "sess-1")
    return graph


@pytest.mark.asyncio
async def test_a_delegation_is_recorded_in_the_room(wired):
    reg = _Registry()
    out = await _dispatch_into_room(reg, "proto", "look at auth", {})

    assert out == "line 40"
    assert len(wired.written) == 1
    config, update = wired.written[0]
    assert config == {"configurable": {"thread_id": "a2a:sess-1"}}
    assert [m.additional_kwargs["room"] for m in update["messages"]] == [
        {"from": "assistant", "to": "proto"},
        {"from": "proto"},
    ]


@pytest.mark.asyncio
async def test_the_orchestrator_is_the_speaker_not_the_operator(wired):
    """The operator did not ask this — the orchestrator did. Recording it as the
    operator's would put words in their mouth and repeat them in every catch-up."""
    await _dispatch_into_room(_Registry(), "proto", "check it", {})
    addressing = wired.written[0][1]["messages"][0]
    assert addressing.additional_kwargs["room"]["from"] == "assistant"
    assert 'from="assistant"' in addressing.content


@pytest.mark.asyncio
async def test_a_managed_git_call_bypasses_the_room_rather_than_losing_its_item_id(wired):
    """`item_id` is a one-PR-per-item claim `run_mention` has no parameter for. Silently
    stripping it would duplicate work; taking the direct route keeps the claim."""
    reg = _Registry()
    out = await _dispatch_into_room(reg, "proto", "build it", {}, item_id="issue-7")
    assert out == "line 40"
    assert reg.calls[0]["item_id"] == "issue-7"
    assert wired.written == []


@pytest.mark.asyncio
async def test_a_parked_task_resume_bypasses_the_room_too(wired):
    reg = _Registry()
    await _dispatch_into_room(reg, "proto", "the answer", {}, resume_task_id="task-9")
    assert reg.calls[0]["resume_task_id"] == "task-9"
    assert wired.written == []


@pytest.mark.asyncio
async def test_no_graph_still_returns_the_reply(monkeypatch):
    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)
    monkeypatch.setattr("tools.lg_tools._session_id_from", lambda state: "sess-1")
    assert await _dispatch_into_room(_Registry(), "proto", "hi", {}) == "line 40"


@pytest.mark.asyncio
async def test_no_session_id_still_returns_the_reply(wired, monkeypatch):
    monkeypatch.setattr("tools.lg_tools._session_id_from", lambda state: "")
    assert await _dispatch_into_room(_Registry(), "proto", "hi", {}) == "line 40"
    assert wired.written == []


@pytest.mark.asyncio
async def test_a_broken_room_never_costs_the_caller_the_reply(wired, monkeypatch):
    """This is the orchestrator's ONLY route to a delegate — bookkeeping must not break it."""

    async def _boom(*a, **kw):
        raise RuntimeError("checkpointer exploded")

    monkeypatch.setattr("graph.mention_op.run_mention", _boom)
    assert await _dispatch_into_room(_Registry(), "proto", "hi", {}) == "line 40"


@pytest.mark.asyncio
async def test_a_failed_delegate_is_reported_as_a_tool_error(wired):
    out = await _dispatch_into_room(_Registry(raises=RuntimeError("offline")), "proto", "hi", {})
    assert out.startswith("Error: delegate 'proto' failed")
    assert "offline" in out


@pytest.mark.asyncio
async def test_a_later_mention_catches_the_participant_up_on_the_delegation(wired):
    """The payoff: what the orchestrator delegated is readable as room conversation."""
    import graph.mention_op as mop

    await _dispatch_into_room(_Registry(reply="the token expires early"), "proto", "why is auth failing?", {})
    room = [m for _c, u in wired.written for m in u["messages"]]
    window, _ = mop.catchup_window(room, "reviewer")
    assert ("assistant", "why is auth failing?") in window
    assert ("proto", "the token expires early") in window
