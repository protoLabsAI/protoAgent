"""Optional extraction through fetch_url, with all HTTP handled in process."""

import json
from types import SimpleNamespace

import httpx
import pytest

from runtime.state import STATE
from security import egress
from tools.lg_tools import fetch_url

URL = "https://example.com/article"
MARKDOWN = "# Article\n\nRendered **page** text."
PROVIDERS = [
    ("firecrawl", "FIRECRAWL_API_KEY", "https://api.firecrawl.dev/v2/scrape"),
    ("tavily", "TAVILY_API_KEY", "https://api.tavily.com/extract"),
]


@pytest.fixture(params=PROVIDERS, ids=[p[0] for p in PROVIDERS])
def provider(request, monkeypatch):
    name, env, endpoint = request.param
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(web_extract_provider=name))
    monkeypatch.setenv(env, "test-extract-key")
    monkeypatch.setattr(egress, "_allowed", ["example.com", "api.firecrawl.dev", "api.tavily.com"])
    return name, env, endpoint


def mock_http(monkeypatch, handler):
    requests = []
    real_client = httpx.AsyncClient

    def wrapped(request):
        requests.append(request)
        return handler(request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


def response_body(provider, content):
    if provider == "firecrawl":
        return {"success": True, "data": {"markdown": content}}
    return {"results": [{"url": URL, "raw_content": content}]}


@pytest.mark.parametrize("max_chars", [12, 8000])
async def test_extracted_markdown(monkeypatch, provider, max_chars):
    name, _, endpoint = provider

    def handler(request):
        assert str(request.url) == endpoint
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer test-extract-key"
        payload = json.loads(request.content)
        assert payload == (
            {"url": URL, "formats": ["markdown"]}
            if name == "firecrawl" else {"urls": [URL], "format": "markdown"}
        )
        return httpx.Response(200, json=response_body(name, MARKDOWN))

    requests = mock_http(monkeypatch, handler)
    result = await fetch_url.ainvoke({"url": URL, "max_chars": max_chars})
    expected = MARKDOWN
    if max_chars < len(MARKDOWN):
        expected = (
            MARKDOWN[:max_chars].rstrip()
            + f"\n…[truncated: showing {max_chars} of {len(MARKDOWN)} chars — "
            "re-call with a larger max_chars to read the rest]"
        )
    assert result == f"[200] {URL}\n\n{expected}"
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["http", "missing_key", "empty", "malformed", "timeout", "denied_host"])
async def test_failure_falls_back(monkeypatch, provider, failure, caplog):
    name, env, endpoint = provider
    if failure == "missing_key":
        monkeypatch.delenv(env)
    if failure == "denied_host":
        monkeypatch.setattr(egress, "_allowed", ["example.com"])

    def handler(request):
        if str(request.url) == URL:
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<main>Plain fetch</main>")
        assert str(request.url) == endpoint
        if failure == "timeout":
            raise httpx.ReadTimeout("provider timed out", request=request)
        if failure == "http":
            return httpx.Response(503, text="unavailable")
        if failure == "malformed":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=response_body(name, "  \n"))

    requests = mock_http(monkeypatch, handler)
    result = await fetch_url.ainvoke({"url": URL})
    assert result == f"[200] {URL}\n\nPlain fetch"
    assert len(requests) == (1 if failure in {"missing_key", "denied_host"} else 2)
    assert "falling back to plain fetch" in caplog.text


@pytest.mark.parametrize("url", ["https://blocked.example/page", "file:///etc/passwd"])
async def test_target_refused_before_provider(monkeypatch, provider, url):
    def handler(request):
        pytest.fail("A refused target must never contact the provider or target")

    requests = mock_http(monkeypatch, handler)
    result = await fetch_url.ainvoke({"url": url})
    assert result.startswith("Error:")
    assert requests == []
