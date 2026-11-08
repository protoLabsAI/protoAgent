"""Compaction fidelity against the 2026-11-07 release incident (ADR 0127 D1/D2/D4/D7).

The incident's shape: the operator gives a conditional approval ("once A and B have both
merged, cut the release; stop only on a WARN or FAIL"), the agent sets up a wait that will
act on its own, then >30K characters of tool output (PR diffs) and the agent's long
commentary on them push the approval far back, then a casual "work is wrapped up". The span
also opens with a previous compaction's summary.

Before ADR 0127 the auto path summarized only the last 4,000 tokens of the span with
langchain's stock prompt, and the manual / overflow path summarized the last 16,000
characters of a tool-less render with the memory-digest prompt — the approval never reached
the summarizer, and neither prompt had a place for it. These tests drive all three paths
with a fake summarizer that records every prompt it receives.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from graph import compaction_engine as engine

APPROVAL = (
    "once #4801 and #4808 part 1 have both merged, cut the release; stop only on a WARN or FAIL "
    "from the review panel and come back to me instead of merging"
)
PREVIOUS_NOTES = "EARLIER-NOTES-7f3a: the operator prefers squash merges and wants releases tagged vX.Y.Z"
NOTES = "\n".join(f"## {h}\nNone" for h in engine.SECTION_HEADINGS)


def _incident_span() -> list:
    """The incident fixture, oldest first. The last two messages are the kept tail."""
    msgs: list = [
        # The 01:08 compaction's summary, in langchain's old framing.
        HumanMessage(
            content=f"Here is a summary of the conversation to date:\n\n{PREVIOUS_NOTES}",
            additional_kwargs={"lc_source": "summarization"},
            id="prev-summary",
        ),
        HumanMessage(content=f"Release plan: {APPROVAL}.", id="approval"),
        AIMessage(
            content="Understood. I'll cut the release once both have merged, and come back to you on a WARN or FAIL.",
            tool_calls=[
                {
                    "id": "call-wait",
                    "name": "wait",
                    "args": {"until": "release PR review", "on_pass": "squash-merge #4847 and tag v0.231.0"},
                }
            ],
            id="ack",
        ),
        ToolMessage(
            content="Scheduled wait w-91: on PASS, squash-merge release PR #4847 and tag v0.231.0.",
            tool_call_id="call-wait",
            name="wait",
            id="wait-result",
        ),
    ]
    for i in range(4):
        cid = f"call-diff-{i}"
        msgs.append(
            AIMessage(
                content=f"Reviewing diff {i}. " + ("The change rewires the release pipeline step by step. " * 110),
                tool_calls=[{"id": cid, "name": "gh_pr_diff", "args": {"pr": 4801 + i}}],
                id=f"ai-diff-{i}",
            )
        )
        msgs.append(
            ToolMessage(
                content=f"diff --git a/f{i}.py b/f{i}.py\n" + ("+    line = compute(value)  # changed\n" * 260),
                tool_call_id=cid,
                name="gh_pr_diff",
                id=f"tool-diff-{i}",
            )
        )
    msgs += [
        HumanMessage(content="what are you waiting on? work is wrapped up", id="casual"),
        AIMessage(content="Waiting on the review panel for the release PR.", id="reply"),
        HumanMessage(content="what are you doing?", id="tail-h"),
        AIMessage(content="Checking the release PR's review.", id="tail-a"),
    ]
    tool_chars = sum(len(m.content) for m in msgs if isinstance(m, ToolMessage))
    assert tool_chars > 30_000, tool_chars
    return msgs


def _assert_fidelity(prompts: list[str], replacement) -> None:
    """The core contract, the same on every path."""
    assert prompts, "the summarizer was never called"
    joined = "\n".join(prompts)
    # The approval, with its condition and stop rule, reached the summarizer verbatim.
    assert APPROVAL in joined
    # The previous compaction's notes seeded the fold (they lead the first prompt).
    assert PREVIOUS_NOTES in prompts[0]
    assert "<previous_notes>" in prompts[0]
    # Every prompt carries the required continuation sections and the no-inference rule.
    for prompt in prompts:
        for heading in engine.SECTION_HEADINGS:
            assert f"## {heading}" in prompt, heading
        assert "Never infer an approval or a revocation" in prompt
    # The pending wait's action reached it too.
    assert "squash-merge release PR #4847 and tag v0.231.0" in joined
    # The replacement is framed as the agent's own notes, and the next compaction sees it as a seed.
    assert isinstance(replacement, HumanMessage)
    assert replacement.content.startswith(engine.SUMMARY_PREFIX)
    assert "not a message from the operator" in replacement.content
    assert engine.is_summary_message(replacement)


# ── a fake summarizer model that records every prompt ────────────────────────


class _RecordingModel(BaseChatModel):
    prompts: list = []
    reply: str = NOTES

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.prompts.append(str(messages[-1].content))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _middleware(model):
    from graph.middleware.compaction import CountingSummarizationMiddleware

    return CountingSummarizationMiddleware(model=model, trigger=("messages", 6), keep=("messages", 2))


def _state() -> dict:
    # Incognito: compacts without an archive, so no history store is needed here.
    return {"messages": _incident_span(), "session_id": "s-incident", "incognito": True}


def test_auto_middleware_path_keeps_the_approval():
    model = _RecordingModel(prompts=[])
    out = _middleware(model).before_model(_state(), None)
    assert out is not None, "compaction should have fired"
    update = out["messages"]
    _assert_fidelity(model.prompts, update[1])
    assert update[1].additional_kwargs.get("lc_source") == "summarization"
    assert [m.id for m in update[2:]] == ["tail-h", "tail-a"]


@pytest.mark.asyncio
async def test_auto_middleware_async_path_keeps_the_approval():
    model = _RecordingModel(prompts=[])
    out = await _middleware(model).abefore_model(_state(), None)
    assert out is not None
    _assert_fidelity(model.prompts, out["messages"][1])


def test_auto_middleware_summarizer_failure_skips_compaction():
    class _Down(_RecordingModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
            raise ValueError("bad request")

    state = _state()
    assert _middleware(_Down(prompts=[])).before_model(state, None) is None


# ── manual /compact and the overflow force path ──────────────────────────────


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages
        self.updates: list = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)}, created_at=None)

    async def aupdate_state(self, config, update):
        self.updates.append(update)


class _FakeHistory:
    def __init__(self):
        self.docs: list = []

    def add(self, content, **kwargs):
        self.docs.append(content)
        return 1


def _recording_summarizer(prompts: list[str]):
    async def summarize(prompt, config):
        prompts.append(prompt)
        return NOTES

    return summarize


@pytest.mark.parametrize("force", [False, True], ids=["manual", "overflow-force"])
def test_compact_thread_keeps_the_approval(force):
    from graph.compaction_op import compact_thread

    prompts: list[str] = []
    graph = _FakeGraph(_incident_span())
    res = asyncio.run(
        compact_thread(
            graph,
            object(),
            None,
            SimpleNamespace(compaction_keep_messages=2),
            "a2a:s-incident",
            "s-incident",
            history_store=None if force else _FakeHistory(),
            summarizer=_recording_summarizer(prompts),
            force=force,
        )
    )
    assert res["refused"] is False, res
    rewritten = graph.updates[-1]["messages"]
    _assert_fidelity(prompts, rewritten[1])
    assert rewritten[1].additional_kwargs.get("lc_source") == "compaction"
    assert [m.id for m in rewritten[2:]] == ["tail-h", "tail-a"]
    # Honest archive note inside the framing.
    assert ("NOT archived" in rewritten[1].content) is force


def test_compact_thread_refuses_when_a_chunk_fails_midway():
    """A summarizer failure on any chunk refuses the manual compaction (never a partial view)."""
    from graph.compaction_op import compact_thread

    calls = {"n": 0}

    async def flaky(prompt, config):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("gateway down")
        return NOTES

    graph = _FakeGraph(_incident_span())
    res = asyncio.run(
        compact_thread(
            graph,
            object(),
            None,
            SimpleNamespace(compaction_keep_messages=2),
            "a2a:s",
            "s",
            history_store=_FakeHistory(),
            summarizer=flaky,
            budget_chars=10_000,
        )
    )
    assert res["refused"] is True and res["reason"] == "summary_error"
    assert graph.updates == []


# ── the engine itself ────────────────────────────────────────────────────────


def test_engine_folds_chunks_oldest_to_newest_carrying_the_running_summary():
    prompts: list[str] = []
    answers = iter(f"NOTES-{i}\n{NOTES}" for i in range(100))

    def summarize(prompt):
        prompts.append(prompt)
        return next(answers)

    out = engine.summarize_span(_incident_span()[:-2], summarize, budget_chars=16_000)
    assert len(prompts) > 1, "a small budget must split the span into several chunks"
    # Each later prompt carries the previous answer as its running notes.
    for i in range(1, len(prompts)):
        assert f"NOTES-{i - 1}" in prompts[i]
    assert out.startswith(f"NOTES-{len(prompts) - 1}")
    # The whole span is covered: the head (approval) and the tail (casual remark).
    assert APPROVAL in prompts[0]
    assert "work is wrapped up" in prompts[-1]


def test_engine_clips_tool_results_with_an_explicit_marker_but_keeps_text_whole():
    long_human = "x" * 50_000
    rendered = engine.render_message(ToolMessage(content="a" * 20_000, tool_call_id="t", name="read"))
    assert "chars of tool output omitted" in rendered and len(rendered) < 3_500
    assert engine.render_message(HumanMessage(content=long_human)).count("x") == 50_000
    chunks = engine.chunk_blocks([engine.render_message(HumanMessage(content=long_human))], 10_000)
    assert sum(c.count("x") for c in chunks) == 50_000


def test_engine_seeds_from_its_own_summary_framing_too():
    msg = engine.summary_message("MY-NOTES", note="The full transcript is archived.")
    assert engine.is_summary_message(msg)
    assert engine.summary_text_of(msg) == "MY-NOTES"
    prompts: list[str] = []
    engine.summarize_span([msg, HumanMessage(content="next")], lambda p: prompts.append(p) or NOTES)
    assert "MY-NOTES" in prompts[0] and "<previous_notes>" in prompts[0]
    # A span that is only a previous summary is carried forward with no call.
    assert engine.summarize_span([msg], lambda p: pytest.fail("no call expected")) == "MY-NOTES"


def test_engine_old_manual_framing_seeds_without_its_archive_note():
    old = HumanMessage(
        content="Here is a summary of the earlier conversation (the full transcript is archived; "
        "use recall_session(session_id='s1')):\n\nOLD-MANUAL-NOTES"
    )
    assert engine.summary_text_of(old) == "OLD-MANUAL-NOTES"


def test_engine_raises_on_failure_and_on_an_empty_answer():
    span = [HumanMessage(content="hello")]
    with pytest.raises(RuntimeError):
        engine.summarize_span(span, lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(engine.EmptySummaryError):
        engine.summarize_span(span, lambda p: "   ")


def test_budget_follows_the_summarizer_window_with_a_conservative_fallback():
    assert engine.summarizer_budget_chars(None) == engine.summarizer_budget_chars(
        engine.FALLBACK_SUMMARIZER_WINDOW_TOKENS
    )
    assert engine.summarizer_budget_chars(200_000) > engine.summarizer_budget_chars(None)
