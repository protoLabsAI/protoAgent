"""ADR 0133 M3: the harvest, facts and compaction seams build their model through
``graph.model_roles.create_role_llm`` and fail over on a retryable provider error.

A BLANK memory model inherits the aux chain (harvest → aux; facts → harvest → aux;
compaction → aux). With ``aux = [a, b]`` and ``a`` returning a 429, every such seam must
answer from ``b``. When ``memory.harvest_model`` / ``memory.facts_model`` IS set, the harvest
and facts passes build from it instead (the ADR 0133 fix); compaction is untouched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from graph import model_roles
from graph.compaction_op import continuation_summarizer
from graph.conversation_harvest import _default_summarizer
from graph.memory_facts import _default_extractor


@pytest.fixture(autouse=True)
def _reset_diagnostics(monkeypatch):
    # Isolate the process-wide fallback diagnostics so one seam's record never leaks.
    monkeypatch.setattr(model_roles, "_last_fallbacks", {})
    monkeypatch.setattr(model_roles, "_streaks", set())


class _RateLimited(Exception):
    """A retryable (429) provider error whose payload must never surface in a log/record."""

    def __init__(self) -> None:
        super().__init__("sensitive provider payload")
        self.status_code = 429


def _chain(monkeypatch, answer):
    """Wire ``aux = [a, b]`` where ``a`` 429s and ``b`` returns ``answer``.

    Returns the ``calls`` list so the test can prove the primary was tried before the
    fallback. ``create_role_llm`` builds each model by name through ``create_llm``.
    """
    calls: list[str] = []

    async def a(_messages):
        calls.append("a")
        raise _RateLimited()

    async def b(_messages):
        calls.append("b")
        return answer

    models = {"a": RunnableLambda(a), "b": RunnableLambda(b)}
    monkeypatch.setattr(model_roles, "create_llm", lambda config, model_name, **kw: models[model_name])
    return SimpleNamespace(aux_model=["a", "b"]), calls


def test_harvest_summarizer_falls_back_to_second_aux_model(monkeypatch):
    config, calls = _chain(monkeypatch, AIMessage(content="summary from b"))
    out = asyncio.run(_default_summarizer("User: still alive?", config))
    assert out == "summary from b"
    assert calls == ["a", "b"]
    record = model_roles.last_fallback("harvest")
    assert record["from"] == ["a"] and record["to"] == "b"


def test_facts_extractor_falls_back_to_second_aux_model(monkeypatch):
    reply = AIMessage(content='[{"subject": "deploy-day", "content": "Deploys happen on Fridays"}]')
    config, calls = _chain(monkeypatch, reply)
    facts = asyncio.run(_default_extractor("User: we deploy on Fridays", config))
    assert facts == [{"subject": "deploy-day", "content": "Deploys happen on Fridays"}]
    assert calls == ["a", "b"]
    assert model_roles.last_fallback("facts")["to"] == "b"


def test_compaction_summarizer_falls_back_to_second_aux_model(monkeypatch):
    config, calls = _chain(monkeypatch, AIMessage(content="notes from b"))
    out = asyncio.run(continuation_summarizer("Summarize the span.", config))
    assert out == "notes from b"
    assert calls == ["a", "b"]
    assert model_roles.last_fallback("compaction")["to"] == "b"


def test_sensitive_provider_payload_never_recorded(monkeypatch, caplog):
    """The fallback record and the warning log carry the error TYPE/status only."""
    config, _ = _chain(monkeypatch, AIMessage(content="summary from b"))
    out = asyncio.run(_default_summarizer("User: still alive?", config))
    assert out == "summary from b"
    assert "sensitive provider payload" not in caplog.text
    assert "sensitive provider payload" not in model_roles.last_fallback("harvest")["reason"]


@pytest.mark.parametrize("aux", ["aux", ""])
@pytest.mark.parametrize(
    "seam, override_model",
    [
        (_default_summarizer, "reserved-harvest"),  # harvest reads memory.harvest_model
        (_default_extractor, "reserved-facts"),     # facts reads memory.facts_model
        (continuation_summarizer, None),            # compaction is untouched (aux → main)
    ],
)
def test_memory_passes_read_their_configured_model(monkeypatch, aux, seam, override_model):
    """ADR 0133 fix: a scalar ``memory.harvest_model`` / ``memory.facts_model`` set in
    Settings now redirects the harvest / facts pass; compaction keeps inheriting aux → main."""
    from graph.config import LangGraphConfig

    monkeypatch.delenv("PROTOAGENT_MODEL", raising=False)
    config = LangGraphConfig(
        model_name="main", aux_model=aux,
        memory_harvest_model="reserved-harvest", memory_facts_model="reserved-facts",
    )
    expected_model = override_model or aux or "main"

    def build(config, model_name, **kwargs):
        return RunnableLambda(lambda _: AIMessage(content=f'["{model_name}"]'))

    monkeypatch.setattr(model_roles, "create_llm", build)
    result = asyncio.run(seam("User: remember this", config))
    expected = [expected_model] if seam is _default_extractor else f'["{expected_model}"]'
    assert result == expected
