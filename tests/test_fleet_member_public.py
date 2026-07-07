"""Member-public deferral (#1890) — graph/fleet/member_public.py.

The hub answers "is this slug-prefixed path public on the MEMBER?" from the
member's own live public-prefix list, fetched off its public /.well-known
endpoint and TTL-cached per slug. Fail-closed: unreachable member, bad payload,
or a non-conforming prefix all yield "not public" (normal bearer auth applies).
"""

from __future__ import annotations

import httpx
import pytest

from graph.fleet import member_public, proxy


@pytest.fixture(autouse=True)
def _clear_caches():
    member_public._cache.clear()
    proxy._slug_cache.clear()
    yield
    member_public._cache.clear()
    proxy._slug_cache.clear()


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    async def get(self, url, timeout=None):
        self.calls.append(url)
        if self.exc:
            raise self.exc
        return self.resp


def _wire(monkeypatch, client, target=("http://127.0.0.1:7001", {})):
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: target)
    monkeypatch.setattr(proxy, "_get_client", lambda: client)


async def test_fetches_and_matches_member_prefixes(monkeypatch):
    client = FakeClient(resp=FakeResp(payload={"public_paths": ["/plugins/content/view"]}))
    _wire(monkeypatch, client)
    assert await member_public.is_member_public("matt", "/plugins/content/view") is True
    assert await member_public.is_member_public("matt", "/plugins/content/view/assets/x.js") is True
    assert await member_public.is_member_public("matt", "/plugins/content/secret") is False
    assert client.calls == [f"http://127.0.0.1:7001{member_public.WELL_KNOWN_PATH}"]  # cached after 1 fetch


async def test_non_conforming_member_prefixes_are_dropped(monkeypatch):
    # A hostile/buggy member can't exempt anything outside a plugin namespace on the hub.
    client = FakeClient(
        resp=FakeResp(payload={"public_paths": ["/", "/api/config", "/api/plugins/install", 42]})
    )
    _wire(monkeypatch, client)
    assert await member_public.member_public_prefixes("matt") == ()


async def test_unreachable_member_fails_closed(monkeypatch):
    client = FakeClient(exc=httpx.ConnectError("refused"))
    _wire(monkeypatch, client)
    assert await member_public.is_member_public("matt", "/plugins/content/view") is False
    # negative-cached — no refetch inside the window
    assert await member_public.is_member_public("matt", "/plugins/content/view") is False
    assert len(client.calls) == 1


async def test_non_200_fails_closed(monkeypatch):
    client = FakeClient(resp=FakeResp(status_code=502, payload={}))
    _wire(monkeypatch, client)
    assert await member_public.member_public_prefixes("matt") == ()


async def test_bad_payload_shape_fails_closed(monkeypatch):
    client = FakeClient(resp=FakeResp(payload={"public_paths": "not-a-list"}))
    _wire(monkeypatch, client)
    assert await member_public.member_public_prefixes("matt") == ()


async def test_unresolvable_slug_fails_closed(monkeypatch):
    client = FakeClient(resp=FakeResp(payload={"public_paths": ["/plugins/x/view"]}))
    _wire(monkeypatch, client, target=None)
    assert await member_public.is_member_public("ghost", "/plugins/x/view") is False
    assert client.calls == []  # never fetched — no base URL to ask


async def test_cache_expiry_refetches(monkeypatch):
    client = FakeClient(resp=FakeResp(payload={"public_paths": ["/plugins/content/view"]}))
    _wire(monkeypatch, client)
    assert await member_public.is_member_public("matt", "/plugins/content/view") is True
    # Force the cache entry stale; the next call re-fetches.
    exp, prefixes = member_public._cache["matt"]
    member_public._cache["matt"] = (exp - member_public._TTL - 1, prefixes)
    assert await member_public.is_member_public("matt", "/plugins/content/view") is True
    assert len(client.calls) == 2


async def test_never_sends_stored_remote_credentials(monkeypatch):
    # The well-known endpoint is public; the stored remote bearer must not be sprayed at it.
    seen = {}

    class SpyClient(FakeClient):
        async def get(self, url, timeout=None, headers=None):
            seen["headers"] = headers
            return FakeResp(payload={"public_paths": []})

    client = SpyClient()
    _wire(monkeypatch, client, target=("http://remote:7870", {"authorization": "Bearer sekrit"}))
    await member_public.member_public_prefixes("matt")
    assert seen["headers"] is None
