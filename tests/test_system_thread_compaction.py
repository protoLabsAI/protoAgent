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


# ── hysteresis: no re-compaction once the kept tail already clears the ceiling ──
#
# Compaction is keep-tail, so the summary + the newest ``keep`` messages can themselves
# sit at/above the low system ceiling. Without a gap between the trigger and that
# post-compaction size, every later ``before_model`` of a wake would compact again —
# a summariser call + archive + count per step, trimming almost nothing. These use a
# REAL per-message token counter (the fixed counter above can't vary by slice) so the
# removable-head size actually depends on the messages, which is what the gate measures.


def _len_counter(msgs) -> int:
    """~4 chars/token, content-length based — so a slice counts proportionally to its
    messages (unlike the pinned counter the other tests use)."""
    return sum(len(str(getattr(m, "content", ""))) for m in msgs) // 4


def _hysteresis_mw(ceiling: int = 30_000) -> CountingSummarizationMiddleware:
    mw = CountingSummarizationMiddleware(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="noop")]),
        trigger=("tokens", 1_000_000),  # chat trigger far out of reach
        keep=("messages", 2),
        token_counter=_len_counter,
        system_thread_max_tokens=ceiling,
        history_store=None,
    )
    mw._create_summary = lambda _msgs: "SUMMARY"  # avoid a real summariser call
    return mw


def test_system_thread_does_not_recompact_when_the_kept_tail_already_clears_the_ceiling():
    """Regression for the round-1 no-hysteresis finding. The post-compaction shape is
    ``[summary, *kept tail]``; here the tail — one message the cutoff can never remove —
    is ~40k alone, so the whole thread is well over the 30k ceiling. The only thing ahead
    of it is the leftover summary, which is not new history (#5106), so a second
    ``before_model`` must NOT compact again.

    (Before #5106 this used a two-message tail of 20k each under ``keep=2``. A system
    thread now keeps at most ``ceiling // 2`` tokens, so the older of those two IS
    removable and that shape compacts once — see the next test.)"""
    from graph.compaction_engine import summary_message

    mw = _hysteresis_mw(30_000)
    summary = summary_message("x" * 4_000, source="summarization")  # ~1k — a leftover summary
    tail = AIMessage(content="z" * 160_000)  # ~40k — the newest message, always kept
    state = {"messages": [summary, tail], "session_id": "system:activity"}
    assert _len_counter(state["messages"]) >= 30_000
    assert mw.before_model(state, None) is None


def test_an_oversized_kept_tail_compacts_once_then_settles():
    """#5106: a tail of several messages over the ceiling is trimmed to the system keep
    budget in ONE compaction, after which the thread does not re-fire."""
    from langchain_core.messages import RemoveMessage

    mw = _hysteresis_mw(30_000)
    tail_a = HumanMessage(content="y" * 80_000)  # ~20k
    tail_b = AIMessage(content="z" * 80_000)  # ~20k — over the 15k keep budget alone
    first = mw.before_model({"messages": [tail_a, tail_b], "session_id": "system:activity"}, None)
    assert _compacted(first)
    after = [m for m in first["messages"] if not isinstance(m, RemoveMessage)]
    assert after[-1].content == tail_b.content and len(after) == 2  # [summary, tail_b]
    assert mw.before_model({"messages": after, "session_id": "system:activity"}, None) is None


def test_system_thread_recompacts_once_enough_new_history_accrues():
    """The other side of the hysteresis gap: when enough fresh history has accumulated
    ahead of the kept tail (here ~15k, above the ceiling//4 margin), the thread compacts
    again — so the bound is maintained, it just isn't re-triggered on every step."""
    mw = _hysteresis_mw(30_000)
    summary = HumanMessage(content="x" * 4_000)  # ~1k — old summary
    accrued = HumanMessage(content="z" * 60_000)  # ~15k — new summarisable history
    tail_a = HumanMessage(content="y" * 80_000)  # ~20k — kept tail
    tail_b = AIMessage(content="y" * 80_000)  # ~20k — kept tail
    state = {"messages": [summary, accrued, tail_a, tail_b], "session_id": "system:activity"}
    # keep=2 → removable head = [summary, accrued] ≈ 16k ≥ 30k//4 → compacts.
    assert _compacted(mw.before_model(state, None))


def test_a_small_head_over_the_ceiling_does_not_compact():
    """Directly on the gate: over the ceiling but with a removable head below ceiling//4
    (≈7.5k for a 30k ceiling), no compaction fires."""
    mw = _hysteresis_mw(30_000)
    small_head = HumanMessage(content="x" * 8_000)  # ~2k — below the ~7.5k margin
    tail = AIMessage(content="z" * 160_000)  # ~40k — the newest message, always kept
    state = {"messages": [small_head, tail], "session_id": "system:activity"}
    assert _len_counter(state["messages"]) >= 30_000
    assert mw.before_model(state, None) is None


def test_chat_thread_never_sees_the_hysteresis_gate():
    """The gate is system-only: a chat thread with the same shape still answers purely to
    the parent's trigger (unreachable here), so it does not compact — behaviour unchanged."""
    mw = _hysteresis_mw(30_000)
    accrued = HumanMessage(content="z" * 60_000)
    tail_a = HumanMessage(content="y" * 80_000)
    tail_b = AIMessage(content="y" * 80_000)
    state = {"messages": [accrued, tail_a, tail_b], "session_id": "sess-chat-123"}
    assert mw.before_model(state, None) is None


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

    monkeypatch.setattr(langgraph.config, "get_config", lambda: {"configurable": {"thread_id": "a2a:system:activity"}})
    assert _is_system_thread({})
    # A non-system a2a thread must not be mistaken for one.
    monkeypatch.setattr(langgraph.config, "get_config", lambda: {"configurable": {"thread_id": "a2a:sess-chat-123"}})
    assert not _is_system_thread({})


# ── steady tool-result inflow (#5106) ─────────────────────────────────────────
#
# Production: a five-hour job on ``system:activity`` with default config (keep 20
# messages, 30k system ceiling) compacted 107 times in ~3.5h — nearly every model step,
# each one rewriting the cached prefix. The thread was fed one 2–5k-token tool result per
# step. The kept tail of 20 messages (~10 results) was itself over the ceiling, so right
# after each compaction the thread was over the ceiling again, and the hysteresis margin
# counted the previous summary as "removable" — one or two new results on top of it
# cleared the margin. This drives that shape through the real hooks, applying each
# compaction's update, and counts.


def _steady_inflow_run(steps: int = 60, ceiling: int = 30_000, summary_tokens: int = 2_000):
    from langchain_core.messages import RemoveMessage, ToolMessage

    mw = CountingSummarizationMiddleware(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="noop")]),
        trigger=("tokens", 200_000),  # the chat trigger (compaction.max_tokens) — out of reach
        keep=("messages", 20),  # compaction.keep_messages default
        system_thread_max_tokens=ceiling,
        history_store=None,
    )
    # A summariser's notes of a realistic size (the engine's required sections).
    mw._create_summary = lambda _msgs: "notes " * (summary_tokens * 4 // 6)
    sizes = [2_000, 3_500, 5_000, 2_500, 4_000]  # tokens per tool result, cycling
    messages: list = [HumanMessage(content="[Autonomous wake — scheduled run.] Run the merge queue.")]
    compacted_at: list[int] = []
    peak = 0
    from langchain_core.messages.utils import count_tokens_approximately

    for step in range(steps):
        call_id = f"call-{step}"
        messages.append(
            AIMessage(content=f"checking PR {step}", tool_calls=[{"id": call_id, "name": "gh", "args": {"pr": step}}])
        )
        messages.append(ToolMessage(content="x" * (sizes[step % len(sizes)] * 4), tool_call_id=call_id))
        result = mw.before_model({"messages": messages, "session_id": "system:activity"}, None)
        if result is not None:
            compacted_at.append(step)
            messages = [m for m in result["messages"] if not isinstance(m, RemoveMessage)]
        peak = max(peak, count_tokens_approximately(messages))
    return compacted_at, peak


def test_a_steady_inflow_of_tool_results_compacts_at_most_once_per_several_steps():
    compacted_at, peak = _steady_inflow_run()
    assert compacted_at, "the system ceiling must still bound the thread"
    gaps = [b - a for a, b in zip(compacted_at, compacted_at[1:], strict=False)]
    assert min(gaps) >= 3, f"compacted at steps {compacted_at}"
    assert len(compacted_at) <= 60 // 3
    # Bounded: never far past the ceiling (one step's inflow over it, at most).
    assert peak <= 30_000 + 6_000, peak


def test_a_large_previous_summary_does_not_count_as_new_history():
    """The previous compaction's notes sit ahead of the kept tail; re-summarising them is
    not progress, so they must not clear the margin on their own."""
    compacted_at, _ = _steady_inflow_run(summary_tokens=6_000)
    gaps = [b - a for a, b in zip(compacted_at, compacted_at[1:], strict=False)]
    assert compacted_at and min(gaps) >= 3, f"compacted at steps {compacted_at}"
