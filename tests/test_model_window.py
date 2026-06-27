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
        return _Resp(200, {"data": [
            {"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}},
            {"model_name": "protolabs/fast", "model_info": {"max_input_tokens": 32768}},
        ]})

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
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"data": [
        {"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}},
    ]}))
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
