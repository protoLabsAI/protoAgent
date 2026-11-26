"""web_search provider selection + DuckDuckGo fallback (#5783).

No live network: provider HTTP goes through an ``httpx.MockTransport`` (injected by
patching ``web_providers._client``), and DuckDuckGo is a fake ``ddgs.DDGS``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from ddgs.exceptions import DDGSException

import tools.web_providers as wp
from runtime.state import STATE
from tools.lg_tools import web_search


@pytest.fixture
def no_sleep(monkeypatch):
    async def fake_sleep(secs, *a, **kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def _use_provider(monkeypatch, provider, searxng_url=""):
    monkeypatch.setattr(
        STATE,
        "graph_config",
        SimpleNamespace(web_search_provider=provider, web_searxng_url=searxng_url),
    )


def _mock_http(monkeypatch, handler):
    requests: list[httpx.Request] = []

    def wrapped(request):
        requests.append(request)
        return handler(request)

    real_client = wp._client
    monkeypatch.setattr(wp, "_client", lambda transport=None: real_client(httpx.MockTransport(wrapped)))
    return requests


def _fake_ddgs(monkeypatch, behavior):
    calls: list[str] = []

    def text(self, query, max_results=5, **kwargs):  # noqa: ANN001
        calls.append(query)
        if isinstance(behavior, BaseException):
            raise behavior
        return list(behavior)

    cls = type(
        "FakeDDGS",
        (),
        {
            "__init__": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
            "text": text,
        },
    )
    monkeypatch.setattr("ddgs.DDGS", cls)
    return calls


async def test_tavily_results_formatted(monkeypatch):
    _use_provider(monkeypatch, "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    ddg_calls = _fake_ddgs(monkeypatch, AssertionError("ddg must not run"))

    def handler(request):
        assert str(request.url) == wp.TAVILY_SEARCH_URL
        assert request.headers["authorization"] == "Bearer tvly-test"
        assert json.loads(request.content) == {"query": "langgraph", "max_results": 2}
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "LangGraph", "url": "https://ex.com/lg", "content": "a graph runtime"},
                    {"title": "Docs", "url": "https://ex.com/docs", "content": ""},
                ]
            },
        )

    reqs = _mock_http(monkeypatch, handler)
    out = await web_search.ainvoke({"query": "langgraph", "max_results": 2})

    assert out == (
        "2 result(s) for 'langgraph':\n"
        "1. LangGraph — https://ex.com/lg\n"
        "   a graph runtime\n"
        "2. Docs — https://ex.com/docs"
    )
    assert len(reqs) == 1
    assert ddg_calls == []


async def test_firecrawl_results_formatted(monkeypatch):
    _use_provider(monkeypatch, "firecrawl")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    _fake_ddgs(monkeypatch, AssertionError("ddg must not run"))

    def handler(request):
        assert str(request.url) == wp.FIRECRAWL_SEARCH_URL
        assert request.headers["authorization"] == "Bearer fc-test"
        assert json.loads(request.content) == {"query": "q", "limit": 3}
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"title": "Fire", "url": "https://ex.com/f", "description": "crawl snippet"}],
            },
        )

    _mock_http(monkeypatch, handler)
    out = await web_search.ainvoke({"query": "q", "max_results": 3})

    assert out == "1 result(s) for 'q':\n1. Fire — https://ex.com/f\n   crawl snippet"


async def test_searxng_results_formatted(monkeypatch):
    _use_provider(monkeypatch, "searxng", searxng_url="http://searx.local:8888/")
    _fake_ddgs(monkeypatch, AssertionError("ddg must not run"))

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "searx.local"
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        assert request.url.params["q"] == "self hosted"
        return httpx.Response(
            200,
            json={
                "results": [{"title": f"R{i}", "url": f"https://ex.com/{i}", "content": f"snip {i}"} for i in range(5)]
            },
        )

    _mock_http(monkeypatch, handler)
    out = await web_search.ainvoke({"query": "self hosted", "max_results": 2})

    # Capped at max_results even when the instance returns more.
    assert out.startswith("2 result(s) for 'self hosted':\n1. R0 — https://ex.com/0\n   snip 0\n")
    assert "3. " not in out


async def test_missing_key_and_ddg_failure_is_error_naming_provider(monkeypatch, no_sleep):
    _use_provider(monkeypatch, "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    reqs = _mock_http(monkeypatch, lambda r: httpx.Response(200, json={}))
    _fake_ddgs(monkeypatch, DDGSException("No results found."))

    out = await web_search.ainvoke({"query": "x", "max_results": 1})

    assert out.startswith("Error:")
    assert "Tavily" in out
    assert "TAVILY_API_KEY is not set" in out
    assert "DuckDuckGo" in out and "No results found" in out
    assert reqs == []  # never called the API without a key


async def test_http_error_and_ddg_failure_is_error_naming_provider(monkeypatch, no_sleep):
    _use_provider(monkeypatch, "firecrawl")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    _mock_http(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    _fake_ddgs(monkeypatch, TimeoutError("read timed out"))

    out = await web_search.ainvoke({"query": "x", "max_results": 1})

    assert out.startswith("Error:")
    assert "Firecrawl" in out
    assert "HTTP 502" in out
    assert "read timed out" in out
    assert "No results for" not in out


async def test_searxng_unset_url_falls_back_to_ddg(monkeypatch):
    _use_provider(monkeypatch, "searxng", searxng_url="")
    ddg_calls = _fake_ddgs(monkeypatch, [{"title": "Duck", "href": "https://ddg.example/1", "body": "quack"}])

    out = await web_search.ainvoke({"query": "fallback", "max_results": 1})

    assert out.startswith("1 result(s) for 'fallback':\n1. Duck — https://ddg.example/1\n   quack")
    assert "answered by DuckDuckGo" in out
    assert "SearXNG" in out and "web.searxng_url is not set" in out
    assert ddg_calls == ["fallback"]


async def test_provider_http_error_falls_back_to_ddg(monkeypatch):
    _use_provider(monkeypatch, "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    _mock_http(monkeypatch, lambda r: httpx.Response(401, json={"detail": "bad key"}))
    _fake_ddgs(monkeypatch, [{"title": "Duck", "href": "https://ddg.example/1", "body": ""}])

    out = await web_search.ainvoke({"query": "q", "max_results": 1})

    assert out.splitlines()[:2] == ["1 result(s) for 'q':", "1. Duck — https://ddg.example/1"]
    assert "answered by DuckDuckGo" in out
    assert "Tavily failed: HTTP 401" in out


async def test_default_provider_never_touches_http(monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", None)  # nothing configured → DuckDuckGo
    reqs = _mock_http(monkeypatch, lambda r: httpx.Response(500))
    _fake_ddgs(monkeypatch, [{"title": "Duck", "href": "https://ddg.example/1", "body": "b"}])

    out = await web_search.ainvoke({"query": "q", "max_results": 1})

    assert out == "1 result(s) for 'q':\n1. Duck — https://ddg.example/1\n   b"
    assert reqs == []


async def test_egress_allowlist_blocks_provider_host(monkeypatch):
    from security import egress

    monkeypatch.setattr(egress, "_allowed", ["example.org"])
    _use_provider(monkeypatch, "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    reqs = _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    _fake_ddgs(monkeypatch, [{"title": "Duck", "href": "https://ddg.example/1", "body": ""}])

    out = await web_search.ainvoke({"query": "q", "max_results": 1})

    assert reqs == []
    assert "answered by DuckDuckGo" in out
    assert "api.tavily.com is blocked" in out


def test_provider_error_names_provider():
    err = wp.WebProviderError("searxng", "boom")
    assert str(err) == "SearXNG: boom"
    assert err.provider == "searxng"
