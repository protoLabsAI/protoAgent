"""System-thread early compaction (#4625).

Scheduled wakes run on the single durable Activity thread (``system:activity``) and
resend the whole accumulated thread every time, so they need a much lower compaction
ceiling than a chat thread. ``CountingSummarizationMiddleware`` applies
``compaction.system_thread_max_tokens`` ONLY on ``system:*`` threads, OR-ed with the
configured trigger; a chat thread of the same size behaves exactly as before.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware.compaction import CountingSummarizationMiddleware, _is_system_thread


def _messages(n: int = 6) -> list:
    """A handful of plain Human/AI turns — enough that a keep=2 cutoff lands above 0
    (so the parent actually produces a compaction update rather than bailing)."""
    out: list = []
    for i in range(n):
        out.append(HumanMessage(content=f"ask {i}"))
        out.append(AIMessage(content=f"answer {i}"))
    return out


def _mw(system_thread_max_tokens: int, *, total_tokens: int) -> CountingSummarizationMiddleware:
    """A middleware whose chat trigger is far out of reach (``tokens:200000``) and whose
    token counter is pinned to ``total_tokens`` — so only the system-thread ceiling can
    decide a compaction in these tests. The summary itself is stubbed (no model call)."""
    mw = CountingSummarizationMiddleware(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="noop")]),
        trigger=("tokens", 200_000),
        keep=("messages", 2),
        token_counter=lambda _msgs: total_tokens,
        system_thread_max_tokens=system_thread_max_tokens,
        history_store=None,
    )
    mw._create_summary = lambda _msgs: "SUMMARY"  # avoid a real summariser call
    return mw


def _compacted(result) -> bool:
    """A real compaction returns ``[RemoveMessage, <summary HumanMessage>, *preserved]``."""
    if result is None:
        return False
    msgs = result.get("messages") or []
    return any(isinstance(m, HumanMessage) and "SUMMARY" in str(m.content) for m in msgs)


# ── the core behaviour (#4625) ────────────────────────────────────────────────


def test_system_thread_compacts_once_over_its_lower_ceiling():
    """40k tokens is under the 200k chat trigger but over the 30k system ceiling, so a
    ``system:activity`` thread compacts."""
    mw = _mw(30_000, total_tokens=40_000)
    state = {"messages": _messages(), "session_id": "system:activity"}
    assert _compacted(mw.before_model(state, None))


def test_chat_thread_of_the_same_size_does_not_compact():
    """Same 40k tokens, same config — a chat thread sees only the parent's 200k trigger,
    so its behaviour is unchanged (no compaction)."""
    mw = _mw(30_000, total_tokens=40_000)
    state = {"messages": _messages(), "session_id": "sess-chat-123"}
    assert mw.before_model(state, None) is None


def test_zero_disables_the_system_thread_ceiling():
    """``compaction.system_thread_max_tokens = 0`` turns the early valve off — a system
    thread then falls back to the chat trigger alone and does NOT compact at 40k."""
    mw = _mw(0, total_tokens=40_000)
    state = {"messages": _messages(), "session_id": "system:activity"}
    assert mw.before_model(state, None) is None


def test_system_thread_below_its_ceiling_does_not_compact():
    """20k tokens is under both thresholds — nothing fires."""
    mw = _mw(30_000, total_tokens=20_000)
    state = {"messages": _messages(), "session_id": "system:activity"}
    assert mw.before_model(state, None) is None


@pytest.mark.asyncio
async def test_system_thread_compacts_on_the_async_path():
    mw = _mw(30_000, total_tokens=40_000)

    async def _summary(_msgs):
        return "SUMMARY"

    mw._acreate_summary = _summary
    state = {"messages": _messages(), "session_id": "system:activity"}
    assert _compacted(await mw.abefore_model(state, None))


# ── thread detection ──────────────────────────────────────────────────────────


def test_is_system_thread_matches_the_activity_session_id():
    assert _is_system_thread({"session_id": "system:activity"})
    assert _is_system_thread({"session_id": "system:inbox"})
    assert not _is_system_thread({"session_id": "sess-chat-123"})
    assert not _is_system_thread({})


def test_is_system_thread_falls_back_to_the_prefixed_thread_id(monkeypatch):
    """The checkpointer thread id carries a transport prefix (``a2a:system:activity``),
    so detection must still see the ``system:`` segment when the state lacks a session id."""
    import langgraph.config

    monkeypatch.setattr(
        langgraph.config, "get_config", lambda: {"configurable": {"thread_id": "a2a:system:activity"}}
    )
    assert _is_system_thread({})
    # A non-system a2a thread must not be mistaken for one.
    monkeypatch.setattr(
        langgraph.config, "get_config", lambda: {"configurable": {"thread_id": "a2a:sess-chat-123"}}
    )
    assert not _is_system_thread({})
