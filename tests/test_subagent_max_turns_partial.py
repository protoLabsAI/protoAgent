"""max_turns is a budget, not a bomb (#1879-class): a subagent that hits the
recursion limit returns its PARTIAL transcript with a hard-stop marker instead of
raising SubagentError — the contract every subagent prompt already promises
("hard stop at max_turns: return what you have"). Seen live: an ADR 0078 shadow
review on protoContent lost a whole panel to one finder reading one file too many."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig


class _FakeSubagent:
    """astream yields one values-state carrying a partial AIMessage, then hits
    the recursion limit — the shape of a finder mid-investigation."""

    def __init__(self, states, raise_after=True):
        self._states = states
        self._raise = raise_after

    async def astream(self, _inputs, config=None, stream_mode=None):
        assert stream_mode == "values"
        for s in self._states:
            yield s
        if self._raise:
            raise GraphRecursionError("Recursion limit of 40 reached without hitting a stop condition.")


@pytest.fixture
def stubbed(monkeypatch):
    cfg = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"], max_turns=40)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", cfg)
    monkeypatch.setattr(agent_mod, "_subagent_tools", lambda *_a, **_k: [object()])
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "build_subagent_prompt", lambda *_a, **_k: "sys")
    return cfg


async def _run(monkeypatch, fake):
    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: fake)
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={},
        available_subagents="stub",
        description="review lane",
        prompt="go",
        subagent_type="stub",
    )


async def test_recursion_limit_salvages_partial_output(monkeypatch, stubbed):
    fake = _FakeSubagent([{"messages": [AIMessage(content="Partial findings so far: A and B.")]}])
    out = await _run(monkeypatch, fake)
    assert "hard-stopped at max_turns" in out and "PARTIAL" in out
    assert "Partial findings so far: A and B." in out  # the transcript survived


async def test_recursion_limit_with_no_output_is_an_explicit_gap(monkeypatch, stubbed):
    fake = _FakeSubagent([{"messages": []}])
    out = await _run(monkeypatch, fake)
    assert "hard-stopped at max_turns" in out and "Gap" in out
    assert not out.startswith("Error")


async def test_clean_completion_is_unchanged(monkeypatch, stubbed):
    fake = _FakeSubagent([{"messages": [AIMessage(content="All done.")]}], raise_after=False)
    out = await _run(monkeypatch, fake)
    assert out.startswith("[stub completed: review lane]")
    assert "All done." in out


async def test_other_failures_still_raise_subagent_error(monkeypatch, stubbed):
    class _Boom:
        async def astream(self, *_a, **_k):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: _Boom())
    with pytest.raises(agent_mod.SubagentError):
        await agent_mod._run_subagent(
            config=LangGraphConfig(),
            tool_map={},
            available_subagents="stub",
            description="review lane",
            prompt="go",
            subagent_type="stub",
        )
