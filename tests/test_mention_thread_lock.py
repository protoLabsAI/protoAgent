"""An `@`-addressed exchange writes the shared checkpointer thread under the lock.

`server/chat.py` states the invariant where it takes the turn lock: concurrent unlocked
writers to `a2a:{sid}` lost-update the history. The mention path runs at STEP 0, *before*
the turn lock, and writes that same thread — so it has to take the lock itself. This is a
race, so a test that merely calls the path proves nothing; these drive two writers at once
and assert they never overlap.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

import runtime.state as rs

sc = importlib.import_module("server.chat")


class _Reg:
    def names(self):
        return ["proto"]

    def roster(self):
        return [{"name": "proto", "type": "acp", "description": "", "url": ""}]

    def get(self, name):
        return type("D", (), {"type": "acp"})()

    async def dispatch(self, name, query, **kw):
        return "ok"


@pytest.fixture
def roster(monkeypatch):
    monkeypatch.setattr(rs.STATE, "delegate_registry", _Reg(), raising=False)
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)


@pytest.mark.asyncio
async def test_two_concurrent_addresses_on_one_thread_never_overlap(roster, monkeypatch):
    """The lock is per-thread, so two mentions on the SAME session serialize."""
    inside, overlaps = 0, []

    async def _slow(message, session_id="", request_metadata=None):
        nonlocal inside
        inside += 1
        overlaps.append(inside)
        await asyncio.sleep(0.02)
        inside -= 1
        return "reply", None

    monkeypatch.setattr(sc, "_at_delegate_exchange", _slow)

    async def _drive():
        return [f async for f in sc._chat_langgraph_stream_impl("@proto hi", "same-session")]

    await asyncio.gather(_drive(), _drive())
    assert max(overlaps) == 1, f"two writers were inside the critical section: {overlaps}"


@pytest.mark.asyncio
async def test_different_sessions_are_not_blocked_by_each_other(roster, monkeypatch):
    """Per-thread, not global — a mention on one session must not serialize another."""
    inside, peak = 0, []

    async def _slow(message, session_id="", request_metadata=None):
        nonlocal inside
        inside += 1
        peak.append(inside)
        await asyncio.sleep(0.02)
        inside -= 1
        return "reply", None

    monkeypatch.setattr(sc, "_at_delegate_exchange", _slow)

    async def _drive(sid):
        return [f async for f in sc._chat_langgraph_stream_impl("@proto hi", sid)]

    await asyncio.gather(_drive("session-a"), _drive("session-b"))
    assert max(peak) == 2, "distinct sessions should run concurrently"


@pytest.mark.asyncio
async def test_the_non_streaming_driver_takes_the_lock_too(roster, monkeypatch):
    inside, overlaps = 0, []

    async def _slow(message, session_id="", request_metadata=None):
        nonlocal inside
        inside += 1
        overlaps.append(inside)
        await asyncio.sleep(0.02)
        inside -= 1
        return "reply", None

    monkeypatch.setattr(sc, "_at_delegate_exchange", _slow)
    await asyncio.gather(
        sc._chat_langgraph_impl("@proto hi", "same-session"),
        sc._chat_langgraph_impl("@proto hi", "same-session"),
    )
    assert max(overlaps) == 1, f"two writers were inside the critical section: {overlaps}"
