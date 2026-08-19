"""A subagent whose config declares ``tools=[]`` is a deliberate TEXT-ONLY
transform (edit/summarize/classify passes) and must run, not be refused (#2857).

The old gate refused ANY empty resolved toolset — which made model-pinned text
passes impossible: a creative-tuned vLLM lane advertising
``supports_function_calling: false`` hard-rejects tools-bearing requests, so the
subagent could neither carry a token tool (lane 400s) nor none (core refused).
Seen live wiring a deslop-pass editor to such a lane. The gate now distinguishes
DECLARED-empty (text-only by design → runs toolless) from declared-but-unresolved
(misconfiguration → still the actionable error)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig


class _FakeSubagent:
    def __init__(self, states):
        self._states = states
        self.created_with_tools = None

    async def astream(self, _inputs, config=None, stream_mode=None):
        assert stream_mode == "values"
        for s in self._states:
            yield s


async def _run(monkeypatch, fake, name):
    def _create_agent(**kw):
        fake.created_with_tools = kw.get("tools")
        return fake

    monkeypatch.setattr(agent_mod, "create_agent", _create_agent)
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={},
        available_subagents=name,
        description="text pass",
        prompt="edit this",
        subagent_type=name,
    )


@pytest.fixture
def _stub_llm(monkeypatch):
    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_mod, "build_subagent_prompt", lambda *_a, **_k: "sys")


async def test_declared_empty_tools_runs_toolless(monkeypatch, _stub_llm):
    cfg = SubagentConfig(name="txt", description="d", system_prompt="p", tools=[], max_turns=6)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "txt", cfg)
    fake = _FakeSubagent([{"messages": [AIMessage(content="Edited draft.")]}])
    out = await _run(monkeypatch, fake, "txt")
    assert "Edited draft." in out
    assert not out.startswith("Error:")
    assert fake.created_with_tools == []  # genuinely toolless on the wire


async def test_declared_tools_that_resolve_to_none_still_errors(monkeypatch, _stub_llm):
    # tools were DECLARED but none exist in the tool_map — a misconfiguration
    # (denylist / unbound instance), and the actionable error must survive.
    cfg = SubagentConfig(name="mis", description="d", system_prompt="p", tools=["ghost_tool"], max_turns=6)
    monkeypatch.setitem(SUBAGENT_REGISTRY, "mis", cfg)
    fake = _FakeSubagent([{"messages": [AIMessage(content="never runs")]}])
    out = await _run(monkeypatch, fake, "mis")
    assert out == "Error: No tools available for subagent 'mis'."
