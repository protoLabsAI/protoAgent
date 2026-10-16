"""A fleet member's signed media links work through the hub without the hub's bearer (#4258).

A member's public base is ``{hub}/agents/<slug>``, so the file links its answers carry are
``{hub}/agents/<slug>/media/<file>?[exp=…&]sig=…``. The hub can't verify them (the signing key
never leaves the member), so it forwards EXACTLY that shape — GET/HEAD, no percent-encoding, a
plain store name, a well-formed ``sig`` (+ optional ``exp``) and nothing else — without its own
auth and without lending any credential, and the member's own ``/media/`` gate decides. Every
other shape keeps the hub's default-deny 401.

These drive the REAL proxy + auth middleware against a REAL member (its real media route behind
its real auth middleware) on a socket, like ``test_fleet_proxy_path_confusion.py``.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from a2a_impl import auth
from graph.fleet import member_public, proxy
from infra import media

SIG = "a" * 64


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    import runtime.state as rs

    monkeypatch.setenv("PROTOAGENT_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    proxy._client = None
    member_public._cache.clear()
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()
    yield
    proxy._client = None
    member_public._cache.clear()
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()
    auth.set_member_public_resolver(None)
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


class _Seen:
    """What reached the member: (path, query, Authorization header) per request."""

    def __init__(self):
        self.requests: list[tuple[str, str, str | None]] = []


@pytest.fixture
def member():
    """A real member — the core media route behind the real default-deny middleware — served on
    a free port. Yields ``(base_url, seen)``."""
    from server.media import register_media_routes

    seen = _Seen()
    app = FastAPI()
    register_media_routes(app)
    auth.install(app, bearer_token="tok", api_key="", allowed_origins_raw="")

    @app.middleware("http")
    async def _record(request: Request, call_next):  # outermost: sees requests auth refuses too
        seen.requests.append((request.url.path, request.url.query, request.headers.get("authorization")))
        return await call_next(request)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", loop="asyncio")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.started and server.servers and server.servers[0].sockets:
                break
            time.sleep(0.02)
        assert server.started, "member server didn't start"
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", seen
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _hub(monkeypatch, member_base: str, *, remote: bool = False) -> TestClient:
    """A token-gated hub proxying slug ``m`` to the member. ``remote`` makes it a registered
    remote member with a stored bearer (one the member would accept)."""
    extra = {"authorization": "Bearer tok"} if remote else {}
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (member_base, dict(extra)) if slug == "m" else None)
    kind = "remote" if remote else "local"
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: (kind, member_base, None) if slug == "m" else None)
    if remote:
        proxy._remote_slugs.add("m")
    auth.set_member_public_resolver(member_public.is_member_public)
    app = FastAPI()

    @app.api_route("/agents/{slug}/{path:path}", methods=["GET", "HEAD", "POST"])
    async def _p(slug: str, path: str, request: Request):
        return await proxy.forward_to(slug, request, path)

    auth.install(app, bearer_token="tok", api_key="", allowed_origins_raw="")
    return TestClient(app)


def _link(ref, **kw) -> str:
    return "/agents/m" + media.media_url(ref.path.name, **kw)


# ── forwarded: the member decides ───────────────────────────────────────────────────────


@pytest.mark.parametrize("remote", [False, True])
def test_a_signed_link_is_fetched_through_the_hub_without_its_bearer(monkeypatch, member, remote):
    base, seen = member
    ref = media.save_media(b"chart-bytes", "image/png")
    hub = _hub(monkeypatch, base, remote=remote)
    for url in (_link(ref), _link(ref, ttl_s=600)):  # permanent and expiring links
        proxy._client = None  # the cached client is bound to the previous request's loop
        r = hub.get(url)
        assert r.status_code == 200, r.text
        assert r.content == b"chart-bytes" and r.headers["content-type"] == "image/png"
    # The hub lent no credential — not its fleet token, not a remote's stored bearer.
    assert [h for _, _, h in seen.requests] == [None, None]


def test_the_callers_bearer_never_rides_to_a_remote_member(monkeypatch, member):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base, remote=True)
    assert hub.get(_link(ref), headers={"Authorization": "Bearer tok"}).status_code == 200
    assert seen.requests[-1][2] is None


@pytest.mark.parametrize("remote", [False, True])
def test_a_forged_signature_is_forwarded_and_refused_by_the_member(monkeypatch, member, remote):
    """The hub doesn't verify (it can't) — the member does, with no borrowed credential to fall
    back on, so a well-formed but wrong signature is a 401 from the member."""
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base, remote=remote)
    r = hub.get(f"/agents/m/media/{ref.path.name}?sig={SIG}")
    assert r.status_code == 401, r.text
    assert seen.requests == [(f"/media/{ref.path.name}", f"sig={SIG}", None)]


def test_an_expired_link_is_a_403_from_the_member(monkeypatch, member):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    r = hub.get(_link(ref, ttl_s=10, now=time.time() - 100))
    assert r.status_code == 403, r.text
    assert "expired" in r.json()["detail"]
    assert len(seen.requests) == 1


# ── refused at the hub: normal auth, the member never sees it ───────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "/agents/m/media/{name}",  # unsigned
        "/agents/m/media/{name}?sig=",  # empty signature
        "/agents/m/media/{name}?sig=abc123",  # short signature
        "/agents/m/media/{name}?sig=" + "A" * 64,  # not lowercase hex
        "/agents/m/media/{name}?sig={sig}&sig={sig}",  # duplicated
        "/agents/m/media/{name}?sig={sig}&token=x",  # an extra parameter
        "/agents/m/media/{name}?sig={sig}&exp=",  # empty expiry
        "/agents/m/media/{name}?exp=01&sig={sig}",  # non-canonical expiry
        "/agents/m/media/{name}?exp=-5&sig={sig}",  # signed expiry
        "/agents/m/media/{name}?exp&sig={sig}",  # a bare key
        "/agents/m/media/.signing-key?sig={sig}",  # a store internal
        "/agents/m/media/a/{name}?sig={sig}",  # nested path
        "/agents/m/media/%61{name}?sig={sig}",  # any percent-encoding in the name
        "/agents/m/media/{name}%2f?sig={sig}",  # an encoded slash
        "/agents/m/media/%2e%2e?sig={sig}",  # an encoded dot-dot
        "/agents/%6d/media/{name}?sig={sig}",  # an encoded slug
        "/agents/%256d/media/{name}?sig={sig}",  # a double-encoded slug
        "/agents/m/api/config?sig={sig}",  # not a media path
        "/agents/m/plugins/x/media/{name}?sig={sig}",  # media below another prefix
        "/agents/m/media?sig={sig}",  # no file
    ],
)
def test_anything_but_the_exact_signed_shape_takes_the_hubs_auth(monkeypatch, member, target):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    r = hub.get(target.format(name=ref.path.name, sig=media.sign_name(ref.path.name)))
    assert r.status_code == 401, (target, r.status_code, r.text)
    # Nothing reached the member's media route (a plugin-namespace path may make the hub's
    # member-public resolver read the member's public-paths list — that's all).
    assert [p for p, _, _ in seen.requests if p != "/.well-known/protoagent/public-paths"] == []


def test_a_non_get_signed_request_takes_the_hubs_auth(monkeypatch, member):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    assert hub.post(_link(ref)).status_code == 401
    assert seen.requests == []


def test_head_is_forwarded_like_get(monkeypatch, member):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    r = hub.head(_link(ref))
    assert r.status_code != 401, r.status_code
    assert seen.requests and seen.requests[0][2] is None
