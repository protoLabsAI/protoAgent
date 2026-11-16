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


def test_empty_role_reports_no_configured_models():
    with pytest.raises(ValueError, match="no configured models"):
        model_roles.create_role_llm(SimpleNamespace(model_name=""), "image_describe")


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


@pytest.mark.parametrize("role", ["aux", "main"])
@pytest.mark.parametrize("bad_index", [0, 1, 2])
def test_unbuildable_backup_is_skipped_but_primary_is_fatal(monkeypatch, caplog, role, bad_index):
    names = ["primary", "middle", "last"]
    cfg = SimpleNamespace(aux_model=names, model_name="primary", routing_fallback_models=names[1:])
    calls = []
    error = ValueError("Missing credentials: sensitive provider payload")

    def factory(config, model_name, **kwargs):
        if model_name == names[bad_index]:
            raise error

        def answer(value):
            calls.append(model_name)
            if model_name == "primary":
                raise TimeoutError()
            return model_name

        return RunnableLambda(answer)

    monkeypatch.setattr(model_roles, "create_llm", factory)
    if bad_index == 0:
        with pytest.raises(ValueError) as caught:
            model_roles.create_role_llm(cfg, role)
        assert caught.value is error
        return
    llm = model_roles.create_role_llm(cfg, role)
    assert llm.names == [name for i, name in enumerate(names) if i != bad_index]
    assert llm.invoke("prompt") == ("last" if bad_index == 1 else "middle")
    assert calls == llm.names
    assert f"[model-roles] {role}[{bad_index}] {names[bad_index]} skipped: ValueError" in caplog.text
    assert "sensitive provider payload" not in caplog.text


@pytest.mark.parametrize("backups", [["bad", "good"], ["bad"]])
def test_main_graph_compiles_with_unbuildable_fallbacks(monkeypatch, caplog, backups):
    import graph.agent as agent
    from graph.config import LangGraphConfig
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    cfg = LangGraphConfig()
    cfg.model_name = "primary"
    cfg.routing_fallback_models = backups
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    error = ValueError("Missing credentials")

    def factory(config, model_name=None, **kwargs):
        if model_name == "bad":
            raise error
        return model

    monkeypatch.setattr(agent, "create_llm", factory)
    captured = []
    real_create = agent.create_agent

    def compile_agent(**kwargs):
        captured.extend(kwargs["middleware"])
        return real_create(**kwargs)

    monkeypatch.setattr(agent, "create_agent", compile_agent)
    graph = agent.create_agent_graph(cfg, include_subagents=False)
    assert graph.get_graph().nodes
    fallbacks = [mw for mw in captured if isinstance(mw, agent.ObservableModelFallbackMiddleware)]
    if "good" in backups:
        assert len(fallbacks) == 1
        assert fallbacks[0].models == [model]
        assert fallbacks[0]._fallback_names == ["good"]
    else:
        assert fallbacks == []
    assert "[model-roles] main[1] bad skipped: ValueError" in caplog.text

    def broken_primary(*args, **kwargs):
        raise error

    monkeypatch.setattr(agent, "create_llm", broken_primary)
    with pytest.raises(ValueError) as caught:
        agent.create_agent_graph(cfg, include_subagents=False)
    assert caught.value is error


def test_main_chain_from_list_model_name_builds_tail_and_aligns_with_status(monkeypatch, caplog):
    """A list-valued ``model.name`` is an ADR 0133 role chain: its TAIL entries must become
    the lead's fallbacks (not just ``routing.fallback_models``), an unbuildable one dropped,
    and the build-time skip index must match the status surface — both read the SAME flattened
    ``role_chain('main')``. The rejected build keyed on raw ``config.model_name`` as one list
    element, so a list's tail was never built and its reported indices diverged from status.
    """
    import graph.agent as agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from graph.config import LangGraphConfig, role_chain

    cfg = LangGraphConfig()
    cfg.model_name = ["primary", "bad", "b"]  # a list chain; NO routing.fallback_models
    cfg.routing_fallback_models = []
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    error = ValueError("Missing credentials")

    def factory(config, model_name=None, **kwargs):
        if model_name == "bad":
            raise error
        return model

    monkeypatch.setattr(agent, "create_llm", factory)
    monkeypatch.setattr(model_roles, "create_llm", factory)

    # The resolved main chain flattens the list; the tail is what should be built as fallbacks.
    assert role_chain(cfg, "main") == ["primary", "bad", "b"]

    captured = []
    real_create = agent.create_agent

    def compile_agent(**kwargs):
        captured.extend(kwargs["middleware"])
        return real_create(**kwargs)

    monkeypatch.setattr(agent, "create_agent", compile_agent)
    graph = agent.create_agent_graph(cfg, include_subagents=False)
    assert graph.get_graph().nodes

    fallbacks = [mw for mw in captured if isinstance(mw, agent.ObservableModelFallbackMiddleware)]
    assert len(fallbacks) == 1
    # The list TAIL became the fallbacks; the unbuildable middle entry was dropped, "b" survives.
    assert fallbacks[0].models == [model]
    assert fallbacks[0]._fallback_names == ["b"]
    assert "[model-roles] main[1] bad skipped: ValueError" in caplog.text

    # The status surface probes the SAME chain, so its reported skip is at the SAME index.
    assert model_roles.skipped_entries(cfg, role_chain(cfg, "main")) == [
        {"index": 1, "model": "bad", "reason": "ValueError"}
    ]
