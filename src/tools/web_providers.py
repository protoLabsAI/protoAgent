"""Keyed / self-hosted web providers behind ``web_search`` (#5783).

``web.search_provider`` picks the backend: ``duckduckgo`` (default, the keyless ``ddgs``
path in ``tools.lg_tools``), ``tavily``, ``firecrawl`` or ``searxng``. The clients here are
plain async httpx calls that each return a list of ``{title, url, body}`` dicts or raise
:class:`WebProviderError` naming the provider — ``web_search`` falls back to DuckDuckGo
on any failure, so a raise here must never be swallowed into an empty result.

API keys come ONLY from env vars (``TAVILY_API_KEY`` / ``FIRECRAWL_API_KEY``) — never the
config file. A missing key is a provider failure, not a silent skip.

Extract clients for ``fetch_url`` (``web.extract_provider``) land beside these in a later
slice; they share :func:`_post_json` / :class:`WebProviderError`.
"""

from __future__ import annotations

import os
from typing import Any

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v1/search"

TAVILY_KEY_ENV = "TAVILY_API_KEY"
FIRECRAWL_KEY_ENV = "FIRECRAWL_API_KEY"

_TIMEOUT_S = 20.0
_USER_AGENT = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"

# Display names used in tool output / errors.
PROVIDER_LABELS = {
    "duckduckgo": "DuckDuckGo",
    "tavily": "Tavily",
    "firecrawl": "Firecrawl",
    "searxng": "SearXNG",
}


class WebProviderError(Exception):
    """A web provider call failed. ``str()`` always names the provider."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{PROVIDER_LABELS.get(provider, provider)}: {reason}")


def _api_key(provider: str, env_var: str) -> str:
    key = (os.environ.get(env_var) or "").strip()
    if not key:
        raise WebProviderError(provider, f"{env_var} is not set (provider keys come from env vars only)")
    return key


def _check_egress(provider: str, url: str) -> None:
    # The egress allowlist fetch_url honours (ADR 0008). These URLs are fixed or
    # operator-configured (never model-chosen), so with no allowlist there is nothing to
    # check; a configured SearXNG often lives on the LAN/loopback, so private hosts pass
    # (link-local/metadata stay blocked).
    from security import egress

    if not egress.is_enabled():
        return
    blocked = egress.check_url(url, allow_private=True, block_unresolvable=False)
    if blocked:
        raise WebProviderError(provider, blocked.removeprefix("Error:").strip())


def _client(transport: Any = None):
    import httpx

    return httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
        transport=transport,
    )


async def _request_json(provider: str, method: str, url: str, *, transport: Any = None, **kwargs) -> Any:
    import httpx

    _check_egress(provider, url)
    try:
        async with _client(transport) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as e:
        raise WebProviderError(provider, f"request failed: {str(e).strip() or type(e).__name__}") from e
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")[:200]
        raise WebProviderError(provider, f"HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
    try:
        return resp.json()
    except ValueError as e:
        raise WebProviderError(provider, "response was not valid JSON") from e


async def _post_json(provider: str, url: str, payload: dict, api_key: str, *, transport: Any = None) -> Any:
    return await _request_json(
        provider,
        "POST",
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        transport=transport,
    )


def _rows(provider: str, items: Any, body_keys: tuple[str, ...]) -> list[dict]:
    if not isinstance(items, list):
        raise WebProviderError(provider, "unexpected response shape (no result list)")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        body = ""
        for k in body_keys:
            if item.get(k):
                body = str(item[k])
                break
        out.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "body": body,
            }
        )
    return out


async def tavily_search(query: str, max_results: int, *, transport: Any = None) -> list[dict]:
    key = _api_key("tavily", TAVILY_KEY_ENV)
    data = await _post_json(
        "tavily",
        TAVILY_SEARCH_URL,
        {"query": query, "max_results": max_results},
        key,
        transport=transport,
    )
    items = data.get("results") if isinstance(data, dict) else None
    return _rows("tavily", items, ("content", "snippet"))[:max_results]


async def firecrawl_search(query: str, max_results: int, *, transport: Any = None) -> list[dict]:
    key = _api_key("firecrawl", FIRECRAWL_KEY_ENV)
    data = await _post_json(
        "firecrawl",
        FIRECRAWL_SEARCH_URL,
        {"query": query, "limit": max_results},
        key,
        transport=transport,
    )
    if not isinstance(data, dict):
        raise WebProviderError("firecrawl", "unexpected response shape")
    if data.get("success") is False:
        raise WebProviderError("firecrawl", str(data.get("error") or "request was not successful"))
    items = data.get("data")
    if isinstance(items, dict):  # v2 shape: {"data": {"web": [...]}}
        items = items.get("web")
    return _rows("firecrawl", items, ("description", "snippet", "markdown"))[:max_results]


async def searxng_search(query: str, max_results: int, base_url: str, *, transport: Any = None) -> list[dict]:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise WebProviderError("searxng", "web.searxng_url is not set")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise WebProviderError("searxng", f"web.searxng_url must be http(s) — got {base!r}")
    data = await _request_json(
        "searxng",
        "GET",
        f"{base}/search",
        params={"q": query, "format": "json"},
        transport=transport,
    )
    items = data.get("results") if isinstance(data, dict) else None
    return _rows("searxng", items, ("content", "snippet"))[:max_results]


async def provider_search(
    provider: str, query: str, max_results: int, *, searxng_url: str = "", transport: Any = None
) -> list[dict]:
    """Dispatch to the configured keyed/self-hosted provider (not DuckDuckGo)."""
    if provider == "tavily":
        return await tavily_search(query, max_results, transport=transport)
    if provider == "firecrawl":
        return await firecrawl_search(query, max_results, transport=transport)
    if provider == "searxng":
        return await searxng_search(query, max_results, searxng_url, transport=transport)
    raise WebProviderError(provider, "unknown search provider")
