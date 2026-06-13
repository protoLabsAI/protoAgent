"""graph.sdk.complete — a bare LLM completion for plugins (ADR 0043 consumption SDK)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_complete_invokes_the_model_with_prompt_and_system(monkeypatch):
    from graph import sdk

    captured = {}

    class _Resp:
        content = "the answer"

    class _LLM:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return _Resp()

    monkeypatch.setattr(sdk.STATE, "graph_config", object(), raising=False)
    monkeypatch.setattr("graph.llm.create_llm", lambda cfg, model_name=None: _LLM())

    out = await sdk.complete("ping", system="be terse")
    assert out == "the answer"
    roles = [type(m).__name__ for m in captured["messages"]]
    assert roles == ["SystemMessage", "HumanMessage"]
    assert captured["messages"][-1].content == "ping"


@pytest.mark.asyncio
async def test_complete_without_system_sends_only_the_prompt(monkeypatch):
    from graph import sdk

    class _Resp:
        content = "ok"

    class _LLM:
        async def ainvoke(self, messages):
            assert len(messages) == 1 and type(messages[0]).__name__ == "HumanMessage"
            return _Resp()

    monkeypatch.setattr(sdk.STATE, "graph_config", object(), raising=False)
    monkeypatch.setattr("graph.llm.create_llm", lambda cfg, model_name=None: _LLM())
    assert await sdk.complete("hi") == "ok"
