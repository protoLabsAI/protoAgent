"""The primary breaker in ObservableModelFallbackMiddleware (#5257).

A primary that is unavailable (stall / transport / timeout) used to cost its full
reconnect budget on EVERY call before the fallback ran. After one such failure the
breaker sends calls for that primary straight to the fallbacks for a cool-off.
"""

from __future__ import annotations

import pytest

import graph.agent as agent
from graph.agent import ObservableModelFallbackMiddleware, reset_primary_breakers
from graph.llm import StreamStallTimeout


class _Model:
    def __init__(self, name: str):
        self.model_name = name


class _Req:
    def __init__(self, model):
        self.model = model

    def override(self, **kw):
        return _Req(kw.get("model", self.model))


class _Err(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_primary_breakers()
    yield
    reset_primary_breakers()


def _handler(order, *, primary_exc=None, fail=()):
    def handler(req):
        name = req.model.model_name
        order.append(name)
        if name == "primary" and primary_exc is not None:
            raise primary_exc
        if name in fail:
            raise RuntimeError(f"{name} down")
        return name

    return handler


def test_stall_trips_and_next_call_skips_the_primary():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    h = _handler(order, primary_exc=StreamStallTimeout("silent"))
    assert mw.wrap_model_call(_Req(_Model("primary")), h) == "cloud"
    assert order == ["primary", "cloud"]
    order.clear()
    # a NEW middleware instance (every subagent step builds its own) still sees the trip
    mw2 = ObservableModelFallbackMiddleware(_Model("cloud"))
    assert mw2.wrap_model_call(_Req(_Model("primary")), h) == "cloud"
    assert order == ["cloud"]


def test_quota_or_4xx_does_not_trip():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    h = _handler(order, primary_exc=_Err(429))
    mw.wrap_model_call(_Req(_Model("primary")), h)
    mw.wrap_model_call(_Req(_Model("primary")), h)
    assert order == ["primary", "cloud", "primary", "cloud"]


def test_tripped_but_fallbacks_fail_tries_primary_and_success_closes():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    mw.wrap_model_call(_Req(_Model("primary")), _handler(order, primary_exc=StreamStallTimeout("silent")))
    order.clear()
    # fallback now down, primary recovered: still served, by the primary
    assert mw.wrap_model_call(_Req(_Model("primary")), _handler(order, fail=("cloud",))) == "primary"
    assert order == ["cloud", "primary"]
    order.clear()
    assert mw.wrap_model_call(_Req(_Model("primary")), _handler(order)) == "primary"
    assert order == ["primary"]  # breaker closed by the success


def test_tripped_and_everything_fails_reraises_primary_error():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    stall = StreamStallTimeout("silent")
    mw.wrap_model_call(_Req(_Model("primary")), _handler([], primary_exc=stall))
    order: list[str] = []
    with pytest.raises(StreamStallTimeout):
        mw.wrap_model_call(_Req(_Model("primary")), _handler(order, primary_exc=stall, fail=("cloud",)))
    assert order == ["cloud", "primary"]


def test_cooloff_expires_into_a_probe_and_doubles_on_repeat(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    h = _handler(order, primary_exc=StreamStallTimeout("silent"))
    mw.wrap_model_call(_Req(_Model("primary")), h)
    assert agent._breaker["primary"][1] == agent._BREAKER_BASE_S
    clock[0] += agent._BREAKER_BASE_S + 1
    order.clear()
    mw.wrap_model_call(_Req(_Model("primary")), h)  # expired: probes the primary, which fails again
    assert order == ["primary", "cloud"]
    assert agent._breaker["primary"][1] == 2 * agent._BREAKER_BASE_S
    for _ in range(10):
        agent._breaker_trip("primary", StreamStallTimeout("silent"))
    assert agent._breaker["primary"][1] == agent._BREAKER_MAX_S


def test_no_fallbacks_never_trips():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    mw.models = []  # routing.fallback_models empty
    order: list[str] = []
    h = _handler(order, primary_exc=StreamStallTimeout("silent"))
    for _ in range(2):
        with pytest.raises(StreamStallTimeout):
            mw.wrap_model_call(_Req(_Model("primary")), h)
    assert order == ["primary", "primary"]
    assert agent._breaker == {}


def test_string_models_key_by_name_not_type():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))

    def handler(req):
        if req.model == "primary-a":
            raise StreamStallTimeout("silent")
        return "ok"

    assert mw.wrap_model_call(_Req("primary-a"), handler) == "ok"
    assert set(agent._breaker) == {"primary-a"}


@pytest.mark.asyncio
async def test_async_path_trips_and_skips():
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    sync = _handler(order, primary_exc=StreamStallTimeout("silent"))

    async def h(req):
        return sync(req)

    assert await mw.awrap_model_call(_Req(_Model("primary")), h) == "cloud"
    order.clear()
    assert await mw.awrap_model_call(_Req(_Model("primary")), h) == "cloud"
    assert order == ["cloud"]


def test_local_queue_timeout_does_not_trip():
    """GatewayQueueTimeout is a TimeoutError, but it means our own in-flight queue is
    congested, not that the primary is unavailable (review on #5258)."""
    from graph.llm_limiter import GatewayQueueTimeout

    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    order: list[str] = []
    h = _handler(order, primary_exc=GatewayQueueTimeout("protolabs/smart", 300.0, 4))
    mw.wrap_model_call(_Req(_Model("primary")), h)
    mw.wrap_model_call(_Req(_Model("primary")), h)
    assert order == ["primary", "cloud", "primary", "cloud"]
    assert agent._breaker == {}


def test_sdk_timeout_trips():
    import httpx
    import openai

    exc = openai.APITimeoutError(request=httpx.Request("POST", "http://gateway/v1/chat/completions"))
    mw = ObservableModelFallbackMiddleware(_Model("cloud"))
    mw.wrap_model_call(_Req(_Model("primary")), _handler([], primary_exc=exc))
    assert "primary" in agent._breaker
