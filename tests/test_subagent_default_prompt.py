"""Bare `/dream` / `/distill` dispatch (#2165): the canonical ADR 0054 forms
(also used by `schedule_task`) reach `_run_subagent` with an EMPTY user message
— OpenAI-compat gateways reject empty content, and when they don't the model
stalls. Self-contained subagents declare `SubagentConfig.default_prompt`; the
runner substitutes it when the incoming prompt is empty/whitespace. A subagent
without one keeps the old pass-through behavior, and a with-args dispatch
passes the operator's text through verbatim."""

from __future__ import annotations

from langchain_core.messages import AIMessage

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import (
    DISTILL_CONFIG,
    DREAM_CONFIG,
    SUBAGENT_REGISTRY,
    SubagentConfig,
)


class _CapturingSubagent:
    """Records the input payload handed to astream, then completes cleanly."""

    def __init__(self):
        self.inputs = None

    async def astream(self, inputs, config=None, stream_mode=None):
        self.inputs = inputs
        yield {"messages": [AIMessage(content="done")]}


async def _dispatch(monkeypatch, subagent_type, prompt):
    """Run `_run_subagent` against a capturing fake and return the first user
    message content the subagent graph was invoked with."""
    fake = _CapturingSubagent()
    monkeypatch.setattr(agent_mod, "_subagent_tools", lambda *_a, **_k: [object()])
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "build_subagent_prompt", lambda *_a, **_k: "sys")
    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: fake)
    await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={},
        available_subagents=subagent_type,
        description="curation pass",
        prompt=prompt,
        subagent_type=subagent_type,
    )
    return fake.inputs["messages"][0]["content"]


def test_dream_and_distill_declare_default_prompts():
    assert DREAM_CONFIG.default_prompt.strip()
    assert DISTILL_CONFIG.default_prompt.strip()
    # A config that doesn't opt in stays blank — pass-through behavior.
    assert SubagentConfig(name="n", description="d", system_prompt="p").default_prompt == ""


async def test_bare_dream_dispatch_gets_the_declared_default(monkeypatch):
    content = await _dispatch(monkeypatch, "dream", "")
    assert content == DREAM_CONFIG.default_prompt
    assert content.strip()  # a real message, never empty content at the gateway


async def test_bare_distill_dispatch_gets_the_declared_default(monkeypatch):
    content = await _dispatch(monkeypatch, "distill", "")
    assert content == DISTILL_CONFIG.default_prompt
    assert content.strip()


async def test_whitespace_only_prompt_counts_as_bare(monkeypatch):
    content = await _dispatch(monkeypatch, "dream", "   \n\t ")
    assert content == DREAM_CONFIG.default_prompt


async def test_with_args_dispatch_passes_through_verbatim(monkeypatch):
    content = await _dispatch(monkeypatch, "dream", "focus only on trading sessions")
    assert content == "focus only on trading sessions"


async def test_subagent_without_default_prompt_keeps_empty_passthrough(monkeypatch):
    cfg = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"])
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", cfg)
    content = await _dispatch(monkeypatch, "stub", "")
    assert content == ""
