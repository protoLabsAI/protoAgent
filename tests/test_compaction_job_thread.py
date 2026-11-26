"""#5786: compacting a scheduled job's own ``job:<uuid>`` thread keeps only the job prompt
as a standing instruction; the agent's own improvised searches and earlier conclusions are
moved to STATE & DECISIONS as history instead of being re-read as rules every run."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph import compaction_engine as engine

ONLY_STANDING = "The only standing instruction is the job prompt"
TO_HISTORY = "carry them forward but move them to STATE & DECISIONS as history"
OWN_ACTIONS = "Your own earlier actions (searches, queries, filters, contact or item lists)"
PREVIOUS_RULE = "Keep from:ramp.com in this search."

PREVIOUS_NOTES = (
    "## STANDING INSTRUCTIONS & APPROVALS\nNone\n"
    "## PENDING ACTIONS\n"
    f"Searches (from:pbrown@example.com OR from:ramp.com). {PREVIOUS_RULE}\n"
    "## NEXT STEPS\nRun the search again."
)
NOTES = "## STATE & DECISIONS\n2026-11-24: searched the inbox.\n## NEXT STEPS\nRe-check the inbox."


def _span() -> list:
    return [
        engine.summary_message(PREVIOUS_NOTES),
        HumanMessage(
            content="[Autonomous wake — scheduled run. Orient from <working_state>, then:]\n\nTriage the inbox."
        ),
        AIMessage(content="Searched the inbox; pbrown hasn't written again."),
    ]


def _assert_job_rules(prompt: str) -> None:
    assert ONLY_STANDING in prompt
    assert OWN_ACTIONS in prompt
    assert "go under STATE & DECISIONS as dated history" in prompt
    assert TO_HISTORY in prompt
    assert "standing instructions, commitments or pending" in prompt
    assert "re-check the source of truth" in prompt
    # The previous notes still ride the prompt so the summarizer can carry them forward.
    assert PREVIOUS_RULE in prompt
    assert "<previous_notes>" in prompt


@pytest.mark.parametrize("sid", ["job:abc", "a2a:job:abc"])
def test_summarize_span_job_thread_adds_rules(sid):
    prompts: list[str] = []
    out = engine.summarize_span(_span(), lambda p: prompts.append(p) or NOTES, session_id=sid)
    assert len(prompts) == 1
    _assert_job_rules(prompts[0])
    assert out.startswith(NOTES)


def test_asummarize_span_job_thread_adds_rules():
    prompts: list[str] = []

    async def summarize(p: str) -> str:
        prompts.append(p)
        return NOTES

    asyncio.run(engine.asummarize_span(_span(), summarize, session_id="job:abc"))
    assert len(prompts) == 1
    _assert_job_rules(prompts[0])


def test_chat_session_prompt_unchanged():
    prompts: list[str] = []
    engine.summarize_span(_span(), lambda p: prompts.append(p) or NOTES, session_id="chat-123")
    assert len(prompts) == 1
    assert ONLY_STANDING not in prompts[0]
    assert TO_HISTORY not in prompts[0]
    seed, blocks, _ = engine._plan(_span(), None)
    assert prompts[0] == engine.build_prompt("\n\n".join(blocks), seed)


def test_build_prompt_flag_default_is_chat_prompt():
    base = engine.build_prompt("chunk", "notes")
    assert engine.build_prompt("chunk", "notes", job_thread=False) == base
    job = engine.build_prompt("chunk", "notes", job_thread=True)
    assert job != base and ONLY_STANDING in job
    # The block sits between the generic rules and the section list.
    assert job.index("Rules:") < job.index(ONLY_STANDING) < job.index("Write exactly these sections")


@pytest.mark.parametrize("sid", ["job:x", "a2a:job:x"])
def test_is_job_session_true(sid):
    assert engine.is_job_session(sid) is True


@pytest.mark.parametrize("sid", ["chat-1", "system:activity", "", "a2a:chat-1", ":job:x", "a:b:job:x"])
def test_is_job_session_false(sid):
    assert engine.is_job_session(sid) is False
