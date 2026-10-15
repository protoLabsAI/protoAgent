"""Configured-model grading shares rubric semantics without loading other credentials."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from evals.judge import ascore_rubric


@pytest.mark.asyncio
async def test_async_grader_uses_supplied_model_and_parses_native_text_blocks(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not reload the production grader configuration")
    monkeypatch.setattr("graph.config.LangGraphConfig.from_yaml", forbidden)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=[{
        "type": "text", "text": '{"criteria":[{"met":true,"why":"supported"},{"met":false}]}'
    }])))
    verdict = await ascore_rubric("fixture answer", ["first", "second"], grader=model)
    assert verdict.score == 0.5 and verdict.met == {"first": True, "second": False}
    assert verdict.error is None
    messages = model.ainvoke.call_args.args[0]
    assert [m.type for m in messages] == ["system", "human"]
    assert "fixture answer" in messages[1].content
    assert "1. first\n2. second" in messages[1].content


@pytest.mark.asyncio
async def test_async_grader_preserves_failure_for_experiment_reporting():
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("fixture failure")))
    with pytest.raises(RuntimeError, match="fixture failure"):
        await ascore_rubric("answer", ["criterion"], grader=model)
