"""Role construction exercised through real LangChain fallback dispatch."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIConnectionError as AnthropicConnectionError
from langchain_core.runnables import RunnableLambda
from openai import APIConnectionError

from graph import model_roles


class ProviderError(Exception):
    def __init__(self, status=None, body=None):
        super().__init__("sensitive provider payload")
        self.status_code = status
        self.body = body


@pytest.fixture(autouse=True)
def diagnostics(monkeypatch):
    monkeypatch.setattr(model_roles, "_last_fallbacks", {})
    monkeypatch.setattr(model_roles, "_streaks", set())


def build(monkeypatch, outcomes):
    calls = []
    models = {}
    for name, outcome in outcomes.items():
        def answer(value, name=name, outcome=outcome, **kwargs):
            calls.append(name)
            result = outcome(value) if callable(outcome) else outcome
            if isinstance(result, Exception):
                raise result
            return result
        models[name] = RunnableLambda(answer)
    monkeypatch.setattr(model_roles, "create_llm", lambda config, model_name, **kw: models[model_name])
    config = SimpleNamespace(aux_model=list(outcomes))
    return model_roles.create_role_llm(config, "aux"), calls, models


@pytest.mark.parametrize("error", [
    ProviderError(429), ProviderError(503),
    ProviderError(body={"error": {"type": "rate_limit_error"}}),
    ProviderError(body={"code": "usage_limit_reached"}),
    ValueError("usage_limit_reached: subscription exhausted"),
    ValueError("ResponseError(code='rate_limit_error', message='try later')"),
    TimeoutError(), ConnectionError(), httpx.ReadTimeout("timeout"),
    httpx.ConnectError("connection"),
    APIConnectionError(request=httpx.Request("GET", "https://example.test")),
    AnthropicConnectionError(request=httpx.Request("GET", "https://example.test")),
])
def test_retryable_errors_fall_back_and_record(monkeypatch, error, caplog):
    llm, calls, _ = build(monkeypatch, {"primary": error, "backup": "answer"})
    assert llm.invoke("prompt") == "answer"
    assert calls == ["primary", "backup"]
    record = model_roles.last_fallback("aux")
    assert record["from"] == ["primary"]
    assert record["to"] == "backup"
    assert record["time"]
    assert type(error).__name__ in record["reason"]
    assert "sensitive provider payload" not in caplog.text
    record["from"].clear()
    assert model_roles.last_fallback("aux")["from"] == ["primary"]


@pytest.mark.parametrize("error", [
    ProviderError(400), ProviderError(401),
    ProviderError(400, {"type": "rate_limit_error"}),
    ProviderError(body={"error": {"type": "content_refusal"}}),
    ValueError("rate_limit_error in user text"),
])
def test_permanent_errors_are_not_retried(monkeypatch, error):
    llm, calls, _ = build(monkeypatch, {"primary": error, "backup": "answer"})
    with pytest.raises(type(error)) as caught:
        llm.invoke("prompt")
    assert caught.value is error
    assert calls == ["primary"]
    assert model_roles.last_fallback("aux") is None


def test_single_model_is_original_model(monkeypatch):
    llm, calls, models = build(monkeypatch, {"primary": "answer"})
    assert llm is models["primary"]
    assert llm.invoke("prompt") == "answer"
    assert calls == ["primary"]


def test_logging_once_per_streak_and_recovery(monkeypatch, caplog):
    outcomes = iter([ProviderError(429), ProviderError(429), "recovered", ProviderError(429)])
    llm, _, _ = build(monkeypatch, {
        "primary": lambda _: next(outcomes), "second": ProviderError(502), "third": "answer",
    })
    assert [llm.invoke("prompt") for _ in range(4)] == ["answer", "answer", "recovered", "answer"]
    messages = [r.message for r in caplog.records if r.name == model_roles.__name__]
    assert len(messages) == 2
    assert all(all(name in message for name in ("aux", "primary", "second", "third")) for message in messages)
    assert model_roles.last_fallback("aux")["from"] == ["primary", "second"]


def test_exhaustion_preserves_original_error(monkeypatch):
    first = ProviderError(429)
    llm, calls, _ = build(monkeypatch, {"primary": first, "backup": TimeoutError()})
    with pytest.raises(ProviderError) as caught:
        llm.invoke("prompt")
    assert caught.value is first
    assert calls == ["primary", "backup"]
    assert model_roles.last_fallback("aux") is None


@pytest.mark.asyncio
async def test_async_batch_and_stream_isolate_calls(monkeypatch):
    llm, _, _ = build(monkeypatch, {
        "primary": lambda value: ProviderError(429) if value == "fail" else "primary answer",
        "backup": "backup answer",
    })
    assert await llm.abatch(["fail", "ok"]) == ["backup answer", "primary answer"]
    assert await llm.ainvoke("fail") == "backup answer"
    assert [chunk async for chunk in llm.astream("fail")] == ["backup answer"]
    assert list(llm.stream("fail")) == ["backup answer"]
    assert llm.batch(["fail", "ok"]) == ["backup answer", "primary answer"]


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_mix_failed_models(monkeypatch):
    async def primary(value):
        await asyncio.sleep(0)
        raise ProviderError(429)

    async def second(value):
        await asyncio.sleep(0)
        if value == "third":
            raise ProviderError(503)
        return "second"

    models = {"primary": RunnableLambda(primary), "second": RunnableLambda(second),
              "third": RunnableLambda(lambda _: "third")}
    monkeypatch.setattr(model_roles, "create_llm", lambda config, model_name: models[model_name])
    llm = model_roles.create_role_llm(SimpleNamespace(aux_model=list(models)), "aux")
    assert await llm.abatch(["second", "third"]) == ["second", "third"]
    record = model_roles.last_fallback("aux")
    assert record["from"] == ["primary", "second"]
    assert record["to"] == "third"


def test_empty_role_does_not_enable_main(monkeypatch):
    with pytest.raises(ValueError, match="no configured models"):
        model_roles.create_role_llm(SimpleNamespace(model_name="primary"), "image_describe")


@pytest.mark.parametrize("partial", [False, True])
def test_stream_failure_never_replays_partial_answer(monkeypatch, partial):
    error = ProviderError(429)

    def primary(value):
        if partial:
            yield "partial answer"
        raise error

    models = {"primary": RunnableLambda(primary), "backup": RunnableLambda(lambda _: "backup")}
    monkeypatch.setattr(model_roles, "create_llm", lambda config, model_name: models[model_name])
    llm = model_roles.create_role_llm(SimpleNamespace(aux_model=list(models)), "aux")
    stream = llm.stream("prompt")
    if partial:
        assert next(stream) == "partial answer"
        with pytest.raises(ProviderError) as caught:
            next(stream)
        assert caught.value is error
        assert model_roles.last_fallback("aux") is None
    else:
        assert list(stream) == ["backup"]
        assert model_roles.last_fallback("aux")["to"] == "backup"
