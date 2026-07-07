"""Fleet tracing: `_run_subagent` wraps its execution in a `subagent:<type>`
boundary observation so the subagent's tool/LLM spans nest under one node in
the current Langfuse trace — WITHOUT regressing the #1879 salvage contract
(GraphRecursionError → partial output) or the SubagentError contract, and as a
strict no-op when tracing is disabled."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from observability import tracing


class _FakeSubagent:
    def __init__(self, states, raise_after=False):
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


@pytest.fixture
def fake_langfuse(monkeypatch):
    """Enable the REAL tracing module with a fake Langfuse client (the module
    object `_run_subagent` resolves via `from observability import tracing`)."""
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "trace-abc"
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return fake


async def _run(monkeypatch, fake_agent):
    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: fake_agent)
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={},
        available_subagents="stub",
        description="review lane",
        prompt="go",
        subagent_type="stub",
        parent_task_id="call-7",
    )


async def test_subagent_run_is_wrapped_in_boundary_span(monkeypatch, stubbed, fake_langfuse):
    out = await _run(monkeypatch, _FakeSubagent([{"messages": [AIMessage(content="done.")]}]))
    assert out.startswith("[stub completed: review lane]")

    fake_langfuse.start_as_current_observation.assert_called_once()
    kwargs = fake_langfuse.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "subagent:stub"
    assert kwargs["as_type"] == "agent"
    assert kwargs["metadata"]["description"] == "review lane"
    assert kwargs["metadata"]["parent_task_id"] == "call-7"
    cm = fake_langfuse.start_as_current_observation.return_value
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()


async def test_salvage_path_survives_boundary_span(monkeypatch, stubbed, fake_langfuse):
    """The #1879 contract holds under tracing: recursion-limit stop still
    salvages the partial transcript, and the boundary span still closes."""
    fake = _FakeSubagent([{"messages": [AIMessage(content="Partial findings so far.")]}], raise_after=True)
    out = await _run(monkeypatch, fake)
    assert "hard-stopped at max_turns" in out and "PARTIAL" in out
    assert "Partial findings so far." in out
    cm = fake_langfuse.start_as_current_observation.return_value
    cm.__exit__.assert_called_once()


async def test_subagent_error_contract_survives_boundary_span(monkeypatch, stubbed, fake_langfuse):
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
    # the span was opened AND closed despite the hard failure
    cm = fake_langfuse.start_as_current_observation.return_value
    cm.__exit__.assert_called_once()


async def test_tracing_disabled_is_a_noop(monkeypatch, stubbed):
    """No Langfuse ⇒ no span calls, identical output."""
    monkeypatch.setattr(tracing, "_langfuse", None)
    monkeypatch.setattr(tracing, "_enabled", False)
    out = await _run(monkeypatch, _FakeSubagent([{"messages": [AIMessage(content="done.")]}]))
    assert out.startswith("[stub completed: review lane]")


async def test_langfuse_setup_failure_never_breaks_the_run(monkeypatch, stubbed):
    fake = MagicMock()
    fake.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    out = await _run(monkeypatch, _FakeSubagent([{"messages": [AIMessage(content="done.")]}]))
    assert out.startswith("[stub completed: review lane]")
