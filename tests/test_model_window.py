"""graph/model_window.py — resolving a model's context window from the LiteLLM gateway (#1378).

Verifies the /v1/model/group/info parse, the un-versioned fallback, caching (one fetch per
base, safe to call per turn), and graceful None on an unknown model / unreachable gateway.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from graph import model_window


def _cfg(**kw):
    return SimpleNamespace(
        api_base=kw.get("api_base", "https://gw.example/v1"),
        api_key=kw.get("api_key", "sk-test"),
        model_name=kw.get("model_name", "protolabs/smart"),
    )


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    model_window.reset_window_cache()
    yield
    model_window.reset_window_cache()


def test_resolves_window_from_model_info_and_caches(monkeypatch):
    calls: list[str] = []

    # The real gateway shape: /v1/model/info with the window nested under model_info.
    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(
            200,
            {
                "data": [
                    {"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}},
                    {"model_name": "protolabs/fast", "model_info": {"max_input_tokens": 32768}},
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    assert model_window.context_window_for(_cfg()) == 196608
    assert model_window.context_window_for(_cfg(), "protolabs/fast") == 32768
    # Hits /v1/model/info on the /v1-stripped root, exactly once (cached after).
    assert calls == ["https://gw.example/v1/model/info"]


def test_also_parses_top_level_group_info_shape(monkeypatch):
    # The grouped /model/group/info view carries the window top-level on model_group — and
    # /v1/model/info 404s on a proxy that only exposes the grouped endpoint.
    def fake_get(url, headers=None, timeout=None):
        if "group/info" in url:
            return _Resp(200, {"data": [{"model_group": "protolabs/smart", "max_input_tokens": 196608}]})
        return _Resp(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert model_window.context_window_for(_cfg()) == 196608


def test_unknown_model_is_none(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _Resp(
            200,
            {
                "data": [
                    {"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}},
                ]
            },
        ),
    )
    assert model_window.context_window_for(_cfg(model_name="claude-opus-4-8")) is None


def test_unreachable_gateway_is_none_and_not_refetched(monkeypatch):
    n = {"calls": 0}

    def boom(*a, **k):
        n["calls"] += 1
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    assert model_window.context_window_for(_cfg()) is None
    assert model_window.context_window_for(_cfg()) is None  # cached miss → no second fetch
    assert n["calls"] == 1


def _window_row_get(seen: list):
    def fake_get(url, headers=None, timeout=None):
        seen.append((headers or {}).get("Authorization"))
        return _Resp(200, {"data": [{"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 262144}}]})

    return fake_get


def test_authenticates_with_the_env_key_when_the_config_key_is_blank(monkeypatch):
    # A fleet agent's gateway key arrives through the environment (its stack env or
    # secrets manager) and `model.api_key` stays blank by design. The lookup read only
    # the config field, went out keyless, and the gateway answered 401 "No api key
    # passed in" — the window stayed unknown for the process's whole life (#3502).
    seen: list = []
    monkeypatch.setattr(httpx, "get", _window_row_get(seen))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert model_window.context_window_for(_cfg(api_key="")) == 262144
    assert seen == ["Bearer sk-from-env"]


def test_the_config_key_still_wins_over_the_env_key(monkeypatch):
    # Same precedence as the gateway client itself (graph.llm._build_llm_kwargs).
    seen: list = []
    monkeypatch.setattr(httpx, "get", _window_row_get(seen))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert model_window.context_window_for(_cfg(api_key="sk-config")) == 262144
    assert seen == ["Bearer sk-config"]


def test_no_key_anywhere_still_probes_without_auth(monkeypatch):
    # A keyless local endpoint (vLLM, Ollama) serves model info unauthenticated.
    seen: list = []
    monkeypatch.setattr(httpx, "get", _window_row_get(seen))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert model_window.context_window_for(_cfg(api_key="")) == 262144
    assert seen == [None]


def test_a_provider_qualified_slot_resolves_its_window_on_that_providers_route(monkeypatch):
    # #3576 / review on #3577: a subagent on `local-vllm:qwen` lives on ANOTHER gateway; only
    # that gateway reports the window. The default route does not list the model at all.
    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.startswith("https://gw.example"):
            return _Resp(200, {"data": [{"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}}]})
        return _Resp(200, {"data": [{"model_name": "qwen", "model_info": {"max_input_tokens": 32768}}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    vllm = SimpleNamespace(id="local-vllm", base_url="https://vllm.example/v1", api_key="k2")
    cfg = SimpleNamespace(
        api_base="https://gw.example/v1",
        api_key="sk-test",
        model_name="protolabs/smart",
        providers=[vllm],
        providers_declared=True,
        provider_ids=lambda: ["local-vllm"],
    )
    assert model_window.context_window_for_slot(cfg, "local-vllm:qwen") == 32768
    assert model_window.context_window_for_slot(cfg, "protolabs/smart") == 196608  # unqualified: default route
    assert model_window.context_window_for_slot(cfg, "nope:qwen") is None  # not a registered provider: plain lookup
    assert sorted(set(calls)) == ["https://gw.example/v1/model/info", "https://vllm.example/v1/model/info"]


def test_two_keys_on_one_base_are_cached_apart(monkeypatch):
    # Review on #3577: keyed by base alone, the first key's model list answered for the
    # second key — which may see a different list on the same gateway.
    seen: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        key = (headers or {}).get("Authorization", "")
        seen.append(key)
        if key.endswith("k-one"):
            return _Resp(200, {"data": [{"model_name": "m", "model_info": {"max_input_tokens": 1000}}]})
        return _Resp(200, {"data": [{"model_name": "m", "model_info": {"max_input_tokens": 2000}}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert model_window.context_window_for(_cfg(api_key="k-one"), "m") == 1000
    assert model_window.context_window_for(_cfg(api_key="k-two"), "m") == 2000
    assert model_window.context_window_for(_cfg(api_key="k-one"), "m") == 1000  # still cached, still its own
    assert len(seen) == 2
