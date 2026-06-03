"""Tests for Discord long-window context (ADR 0015, slice 4).

The turn log (SQLite) + context-envelope assembler, and the gateway warming path
that prepends recent turns and records new ones. The DB is pointed at a tmp file
via ``DISCORD_LOG_PATH``.
"""

from __future__ import annotations

import pytest

from surfaces.discord import gateway as gw
from surfaces.discord.context import assemble_discord_context
from surfaces.discord.turn_log import Turn, TurnLog


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_LOG_PATH", str(tmp_path / "turns.db"))
    return TurnLog()


# ── turn log ───────────────────────────────────────────────────────────────────


def test_record_and_recent_roundtrip(tlog):
    tlog.record_user_turn("c", "u", "hello", conversation_id="conv1")
    tlog.record_assistant_turn("c", "u", "hi there", conversation_id="conv1")
    recent = tlog.get_recent_turns("c", "u")
    assert [t.role for t in recent] == ["user", "assistant"]  # oldest-first
    assert recent[0].content == "hello" and recent[1].content == "hi there"


def test_recent_scoped_by_channel_user(tlog):
    tlog.record_user_turn("c1", "u", "one")
    tlog.record_user_turn("c2", "u", "two")
    tlog.record_user_turn("c1", "other", "three")
    assert [t.content for t in tlog.get_recent_turns("c1", "u")] == ["one"]


def test_recent_respects_limit_and_blank_skipped(tlog):
    for i in range(5):
        tlog.record_user_turn("c", "u", f"m{i}")
    tlog.record_user_turn("c", "u", "   ")  # blank ⇒ not stored
    recent = tlog.get_recent_turns("c", "u", limit=3)
    assert [t.content for t in recent] == ["m2", "m3", "m4"]  # last 3, oldest-first


# ── context assembler ───────────────────────────────────────────────────────────


def test_assemble_wraps_history_and_current():
    turns = [Turn(ts=1_700_000_000_000, channel_id="c", user_id="u", role="user", content="prior q"),
             Turn(ts=1_700_000_001_000, channel_id="c", user_id="u", role="assistant", content="prior a")]
    out = assemble_discord_context(turns, "now what?")
    assert "<recent_conversation>" in out and "User: prior q" in out and "Assistant: prior a" in out
    assert "<current_message>\nnow what?\n</current_message>" in out


def test_assemble_no_history_is_bare_current():
    out = assemble_discord_context([], "just this")
    assert "<recent_conversation>" not in out
    assert out == "<current_message>\njust this\n</current_message>"


def test_assemble_empty_current_is_empty():
    assert assemble_discord_context([], "") == ""


# ── gateway warming integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_warms_with_history_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_LOG_PATH", str(tmp_path / "turns.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    gw._message_buffers.clear()
    gw._conversations._conversations.clear()
    gw._turn_log = None  # force a fresh lazy-init against the tmp DB

    async def fake_api(method, path, body=None):
        return {"id": "r1"}

    monkeypatch.setattr(gw, "_api", fake_api)

    seen: dict = {}

    async def fake_invoke(prompt, session_id):
        seen["prompt"] = prompt
        return "the answer"

    gw._invoke = fake_invoke

    # Pre-seed a prior exchange in the log so warming has something to prepend.
    tl = gw._get_turn_log()
    tl.record_user_turn("dm", "u1", "earlier question", conversation_id="old")
    tl.record_assistant_turn("dm", "u1", "earlier answer", conversation_id="old")

    cid, _n, _t = gw._conversations.get_or_create("dm", "u1", timeout_s=900)
    gw._message_buffers["dm:u1"] = {
        "messages": [{"id": "m0", "content": "follow up"}], "channel_id": "dm",
        "user_id": "u1", "is_dm": True, "conversation_id": cid,
        "is_new_conversation": True, "timer": None,
    }
    await gw._flush_burst("dm:u1")

    # The invocation prompt was warmed with the prior turns.
    assert "<recent_conversation>" in seen["prompt"]
    assert "earlier question" in seen["prompt"] and "follow up" in seen["prompt"]
    # This turn (user msg + assistant reply) is now recorded.
    recent = tl.get_recent_turns("dm", "u1")
    assert "follow up" in [t.content for t in recent]
    assert "the answer" in [t.content for t in recent]

    gw._turn_log = None
