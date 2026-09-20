"""A subagent that stops mid-loop is continued, not reported "completed" (#3552).

An agent loop ends on the first model turn with no tool call, and the delegation's
answer is the last AIMessage with content. A model that just STOPS — narrates its next
read and never makes the call, or returns an empty turn so the walk-back lands on an
earlier narration — therefore looked exactly like one that finished. Seen live: 17% of
``review-finder`` steps "completed" on one sentence ("Let me check the other callers
of X:"), no findings array, p50 509s into the loop.

Driven through the REAL runner (real ``create_agent``, the real subagent middleware
stack, a real tool; only the chat model is scripted), like ``test_subagent_turn_budget``.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.middleware.completion_guard import NUDGE_MARK, CompletionGuardMiddleware
from graph.subagents.config import REVIEW_FINDER_CONFIG, REVIEW_SYNTHESIZER_CONFIG, SUBAGENT_REGISTRY, SubagentConfig

PROBE = "completion-probe"
MARKER = "```json"
DELIVERABLE = "No defects.\n\n```json\n[]\n```"
NARRATION = "Let me check the other callers of `resolveCustomTest`:"


def _call(i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "ping", "args": {}, "id": f"call-{i}"}])


class _ScriptedModel(BaseChatModel):
    """Replays ``script`` one turn per call; records the messages each call was shown."""

    script: list = []
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        turn = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        # A fresh message per call, as a real model returns: the state reducer merges by id.
        msg = AIMessage(content=turn.content, tool_calls=list(turn.tool_calls))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-turns"


@pytest.fixture
def probe(monkeypatch):
    """``arm(script, marker=…, max_turns=…)`` registers the probe subagent and scripts its model."""
    models: list[_ScriptedModel] = []
    state: dict = {}

    @tool
    def ping() -> str:
        """Return pong."""
        return "pong"

    def _create_llm(*_a, **_k):
        m = _ScriptedModel(script=state["script"], seen=[])
        models.append(m)
        return m

    monkeypatch.setattr(agent_mod, "create_llm", _create_llm)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def arm(script, *, marker=MARKER, max_turns=8):
        state["script"] = script
        monkeypatch.setitem(
            SUBAGENT_REGISTRY,
            PROBE,
            SubagentConfig(
                name=PROBE,
                description="d",
                system_prompt="p",
                tools=["ping"],
                max_turns=max_turns,
                completion_marker=marker,
            ),
        )
        return ping, models

    return arm


async def _run(ping, truncate=None) -> str:
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"ping": ping},
        available_subagents=PROBE,
        description="lane",
        prompt="go",
        subagent_type=PROBE,
        truncate=truncate,
    )


def _nudges(messages) -> int:
    # `.text`, not `.content`: the prompt-cache middleware re-shapes request content into blocks.
    return sum(1 for m in messages if isinstance(m, HumanMessage) and str(m.text).startswith(NUDGE_MARK))


async def test_a_narration_that_ends_the_loop_is_sent_back_to_the_model(probe):
    # One tool round, then the live failure shape: text announcing a read, no tool call.
    ping, models = probe([_call(0), AIMessage(content=NARRATION), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out
    assert len(models[-1].seen) == 3
    assert _nudges(models[-1].seen[-1]) == 1  # the recovering call saw exactly one nudge


async def test_an_empty_final_turn_is_sent_back_too(probe):
    # The other shape: the turn after a narrated tool round comes back empty, and the
    # walk-back used to surface the narration as the lane's whole answer.
    narrated_call = AIMessage(content=NARRATION, tool_calls=[{"name": "ping", "args": {}, "id": "c0"}])
    ping, models = probe([narrated_call, AIMessage(content=""), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out


async def test_the_model_may_answer_a_nudge_with_the_tool_call_it_described(probe):
    ping, models = probe([AIMessage(content=NARRATION), _call(0), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out


async def test_nudges_are_bounded_and_the_failure_is_labelled(probe):
    ping, models = probe([AIMessage(content=NARRATION)])  # never recovers
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane"), out
    assert "INCOMPLETE" in out and "Gap" in out and NARRATION in out
    assert "completed" not in out.splitlines()[0]
    assert len(models[-1].seen) == 3  # the original turn + two nudged retries, then it ends


async def test_a_delivered_answer_is_never_nudged(probe):
    ping, models = probe([_call(0), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 2 and _nudges(models[-1].seen[-1]) == 0


async def test_no_marker_means_no_guard(probe):
    ping, models = probe([AIMessage(content=NARRATION)], marker="")
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 1


async def test_the_deliverable_is_judged_before_truncation(probe):
    # A fan-out's `truncate` can cut the fence off a long answer; that is not a dead lane.
    ping, _ = probe([AIMessage(content="x" * 500 + "\n" + DELIVERABLE)])
    out = await _run(ping, truncate=100)
    assert out.startswith(f"[{PROBE} completed: lane]"), out


async def test_nudges_spend_the_turn_budget_and_never_outrun_it(probe):
    # max_turns is still the ceiling: a model that only ever narrates cannot loop on nudges.
    ping, models = probe([AIMessage(content=NARRATION)], max_turns=1)
    out = await _run(ping)
    assert len(models[-1].seen) <= 3
    assert "completed: lane]" not in out.splitlines()[0]


def test_a_turn_with_tool_calls_is_left_alone():
    guard = CompletionGuardMiddleware(marker=MARKER)
    assert guard._intervene({"messages": [HumanMessage(content="go"), _call(0)]}) is None


def test_the_review_lanes_declare_their_deliverable():
    for cfg in (REVIEW_FINDER_CONFIG, REVIEW_SYNTHESIZER_CONFIG):
        assert cfg.completion_marker == MARKER
        assert cfg.completion_marker in cfg.system_prompt  # the contract the prompt states
