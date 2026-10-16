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
    from operator_api.fleet_routes import register_fleet_routes

    app = FastAPI()
    register_fleet_routes(app)  # the REAL /agents/{slug}/{path} route (methods incl. HEAD)
    auth.install(app, bearer_token="tok", api_key="", allowed_origins_raw="")
    return TestClient(app)


def _link(ref, ttl_s: int = 600, **kw) -> str:
    """A member's expiring link as the hub sees it (the only kind the hub forwards anonymously)."""
    return "/agents/m" + media.media_url(ref.path.name, ttl_s=ttl_s, **kw)


def _exp() -> str:
    return str(int(time.time()) + 600)


# ── forwarded: the member decides ───────────────────────────────────────────────────────


@pytest.mark.parametrize("remote", [False, True])
def test_an_expiring_link_is_fetched_through_the_hub_without_its_bearer(monkeypatch, member, remote):
    base, seen = member
    ref = media.save_media(b"chart-bytes", "image/png")
    hub = _hub(monkeypatch, base, remote=remote)
    r = hub.get(_link(ref))
    assert r.status_code == 200, r.text
    assert r.content == b"chart-bytes" and r.headers["content-type"] == "image/png"
    # The hub lent no credential — not its fleet token, not a remote's stored bearer.
    assert [h for _, _, h in seen.requests] == [None]


def test_a_permanent_link_is_never_exempt(monkeypatch, member):
    """A permanent ``?sig=`` link (console inline links — in text, history, exports, logs) keeps
    the hub's auth: exempting it would make every link a member ever minted fetchable forever."""
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    assert hub.get("/agents/m" + ref.url).status_code == 401
    assert seen.requests == []
    proxy._client = None
    # With the hub's bearer it still proxies (the operator's own console path).
    assert hub.get("/agents/m" + ref.url, headers={"Authorization": "Bearer tok"}).status_code == 200


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
    exp = _exp()
    r = hub.get(f"/agents/m/media/{ref.path.name}?exp={exp}&sig={SIG}")
    assert r.status_code == 401, r.text
    assert seen.requests == [(f"/media/{ref.path.name}", f"exp={exp}&sig={SIG}", None)]


def test_an_expired_link_is_a_403_from_the_member(monkeypatch, member):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    r = hub.get(_link(ref, ttl_s=10, now=time.time() - 100))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "Forbidden: this media link has expired — ask the agent for a fresh link"}
    assert "expired" in r.json()["detail"]
    assert len(seen.requests) == 1


# ── refused at the hub: normal auth, the member never sees it ───────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "/agents/m/media/{name}",  # unsigned
        "/agents/m/media/{name}?sig={sig}",  # permanent (no exp) — never exempt
        "/agents/m/media/{name}?exp={exp}",  # no signature
        "/agents/m/media/{name}?exp={exp}&sig=",  # empty signature
        "/agents/m/media/{name}?exp={exp}&sig=abc123",  # short signature
        "/agents/m/media/{name}?exp={exp}&sig=" + "A" * 64,  # not lowercase hex
        "/agents/m/media/{name}?exp={exp}&sig={sig}&sig={sig}",  # duplicated
        "/agents/m/media/{name}?exp={exp}&exp={exp}&sig={sig}",  # duplicated expiry
        "/agents/m/media/{name}?exp={exp}&sig={sig}&token=x",  # an extra parameter
        "/agents/m/media/{name}?sig={sig}&exp=",  # empty expiry
        "/agents/m/media/{name}?exp=01&sig={sig}",  # non-canonical expiry
        "/agents/m/media/{name}?exp=-5&sig={sig}",  # signed expiry
        "/agents/m/media/{name}?exp&sig={sig}",  # a bare key
        "/agents/m/media/.signing-key?exp={exp}&sig={sig}",  # a store internal
        "/agents/m/media/a/{name}?exp={exp}&sig={sig}",  # nested path
        "/agents/m/media/%61{name}?exp={exp}&sig={sig}",  # any percent-encoding in the name
        "/agents/m/media/{name}%2f?exp={exp}&sig={sig}",  # an encoded slash
        "/agents/m/media/%2e%2e?exp={exp}&sig={sig}",  # an encoded dot-dot
        "/agents/%6d/media/{name}?exp={exp}&sig={sig}",  # an encoded slug
        "/agents/%256d/media/{name}?exp={exp}&sig={sig}",  # a double-encoded slug
        "/agents/m/api/config?exp={exp}&sig={sig}",  # not a media path
        "/agents/m/plugins/x/media/{name}?exp={exp}&sig={sig}",  # media below another prefix
        "/agents/m/media?exp={exp}&sig={sig}",  # no file
    ],
)
def test_anything_but_the_exact_signed_shape_takes_the_hubs_auth(monkeypatch, member, target):
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    exp = _exp()
    r = hub.get(target.format(name=ref.path.name, exp=exp, sig=media.sign_name(ref.path.name, exp)))
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


def test_head_through_the_real_routes(monkeypatch, member):
    """HEAD is real on both the hub's ``/agents`` route and the member's ``/media`` route: the
    same status and headers as GET, no body."""
    base, seen = member
    ref = media.save_media(b"chart-bytes", "image/png")
    hub = _hub(monkeypatch, base)
    r = hub.head(_link(ref))
    assert r.status_code == 200 and r.content == b""
    # The member's file headers (the proxy re-frames the body, so content-length isn't relayed).
    assert r.headers["content-type"] == "image/png" and r.headers.get("etag")
    proxy._client = None
    r = hub.head(_link(ref, ttl_s=10, now=time.time() - 100))
    assert r.status_code == 403 and r.content == b""
    assert len(seen.requests) == 2 and all(h is None for _, _, h in seen.requests)


@pytest.mark.parametrize("slug", ["nobody", "down"])
def test_an_unknown_or_unreachable_slug_answers_the_same_401(monkeypatch, member, slug):
    """No slug probing on the anonymous path: an unknown slug (never dialled) and a registered but
    unreachable one answer exactly what an unauthenticated request does."""
    base, _ = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    dialled: list = []
    real_target = proxy._target_for_slug

    def _target(s):
        dialled.append(s)
        if s == "down":
            return ("http://127.0.0.1:9", {})  # nothing listens on the discard port
        return real_target(s)

    monkeypatch.setattr(proxy, "_target_for_slug", _target)
    path = media.media_url(ref.path.name, ttl_s=600)
    r = hub.get(f"/agents/{slug}{path}")
    unauth = hub.get(f"/agents/m/media/{ref.path.name}")  # what any unauthenticated request gets
    assert r.status_code == 401 and r.json() == unauth.json(), r.text
    assert dialled[0] == slug


def test_the_hub_route_keeps_its_methods():
    from fastapi import FastAPI

    from operator_api.fleet_routes import register_fleet_routes

    app = FastAPI()
    register_fleet_routes(app)
    (route,) = [
        r for r in app.routes if getattr(r, "path", "") == "/agents/{slug}/{path:path}" and hasattr(r, "methods")
    ]
    assert {"GET", "HEAD", "POST"} <= route.methods
