"""One oversized operator message must not erase earlier context (ADR 0127, #4848)."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph import compaction_engine as engine
from tests.test_compaction_fidelity import (
    APPROVAL,
    NOTES,
    _RecordingModel,
    _middleware,
    _FakeGraph,
    _FakeHistory,
    _recording_summarizer,
)

LANGCHAIN_EMPTY_TRIM_FALLBACK = "Previous conversation was too long to summarize."
# A single-character marker stays countable even across arbitrary chunk boundaries.
BIG_MARKER = "⬟"


def _oversized_human_span() -> list:
    big = HumanMessage(content=BIG_MARKER * 25_001, id="big")
    assert len(big.content) > 24_000
    return [
        HumanMessage(content=f"Release plan: {APPROVAL}.", id="approval"),
        AIMessage(content="Understood. I'll follow that release plan.", id="ack"),
        big,
        HumanMessage(content="What are you waiting on?", id="tail-h"),
        AIMessage(content="Checking the release review.", id="tail-a"),
    ]


def _assert_oversized_fidelity(prompts, update, span):
    assert prompts, "the summarizer was never called"
    assert APPROVAL in "\n".join(prompts)
    replacement = update[1]
    assert replacement.content.startswith(engine.SUMMARY_PREFIX)
    assert LANGCHAIN_EMPTY_TRIM_FALLBACK not in replacement.content
    assert NOTES in replacement.content
    big = next(m for m in span if m.id == "big")
    assert sum(prompt.count(BIG_MARKER) for prompt in prompts) == big.content.count(BIG_MARKER)
    assert [m.id for m in update[2:]] == ["tail-h", "tail-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
async def test_auto_compaction_keeps_context_before_oversized_human(monkeypatch, use_async):
    span = _oversized_human_span()
    model = _RecordingModel(prompts=[])
    middleware = _middleware(model)
    # The shared fixture triggers at six messages; this minimal span has only five.
    monkeypatch.setattr(middleware, "_trigger_clauses", [{"messages": len(span)}])
    state = {"messages": span, "session_id": "s-big", "incognito": True}
    if use_async:
        out = await middleware.abefore_model(state, None)
    else:
        out = middleware.before_model(state, None)
    assert out is not None, "compaction should have fired"
    _assert_oversized_fidelity(model.prompts, out["messages"], span)


@pytest.mark.parametrize("force", [False, True], ids=["manual", "overflow-force"])
def test_compact_thread_keeps_context_before_oversized_human(force):
    from graph.compaction_op import compact_thread

    span = _oversized_human_span()
    prompts: list[str] = []
    graph = _FakeGraph(span)
    res = asyncio.run(
        compact_thread(
            graph,
            object(),
            None,
            SimpleNamespace(compaction_keep_messages=2),
            "a2a:s-big",
            "s-big",
            history_store=None if force else _FakeHistory(),
            summarizer=_recording_summarizer(prompts),
            force=force,
            budget_chars=10_000,
        )
    )
    assert res["refused"] is False, res
    assert len(prompts) >= 2
    _assert_oversized_fidelity(prompts, graph.updates[-1]["messages"], span)
