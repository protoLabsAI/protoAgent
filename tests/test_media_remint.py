"""Re-minting an EXPIRED signed media link this box minted (#4274 item 4, server half).

A member's images in its hub-console history use expiring links
(``…/media/<file>?exp=…&sig=…``, ``media.remote_link_ttl_s``, default 7 days). Once ``exp``
passes the inline image 403s. ``infra.media.remint_link`` + ``POST /api/media/remint`` hand
back a FRESH link for the same file: unlike ``open_link`` (which refuses expired links on
purpose) expiry is deliberately ignored — but the HMAC must still verify, so a caller can only
re-mint a link it was handed, never conjure one for an arbitrary name.

The route is operator-authenticated and NOT a media-exempt path; through the fleet hub it rides
the normal ``/agents/<slug>/api/…`` proxy (operator caller → the fleet-token swap → the member),
exercised here against a REAL member behind its REAL auth on a socket, like
``tests/test_fleet_media_links.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from a2a_impl import auth
from graph.fleet import member_public, proxy
from infra import media

# The fleet service token the hub presents a LOCAL member with (ADR 0089): the hub swaps the
# operator caller's credential for it, and the member accepts it as operator.
FLEET = "fleet-service-token-for-tests"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Per-test instance root + clean fleet/auth module state (mirrors test_fleet_media_links)."""
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
    auth._FLEET[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


def _set_media_config(monkeypatch, *, public: bool = False, retention_days: int = 0, remote_link_ttl_s: int = 3600):
    from runtime.state import STATE

    monkeypatch.setattr(
        STATE,
        "graph_config",
        SimpleNamespace(
            media_public=public, media_retention_days=retention_days, media_remote_link_ttl_s=remote_link_ttl_s
        ),
    )


# ── infra.media.remint_link ─────────────────────────────────────────────────────


def test_remint_revives_an_expired_link(monkeypatch):
    """r1: an expired link this box signed → a fresh, unexpired link for the SAME file that
    ``check_access`` admits as ok."""
    _set_media_config(monkeypatch, remote_link_ttl_s=3600)
    ref = media.save_media(b"png", "image/png")
    name = ref.path.name
    minted = 1_000_000_000
    expired = media.media_url(name, ttl_s=10, now=minted)  # exp = minted+10
    now = minted + 50_000  # long past the original expiry

    # Sanity: the original link really IS expired at `now` (this is why open_link would refuse it).
    oexp, osig = media._query_value(expired, "exp"), media._query_value(expired, "sig")
    assert media.check_access(f"/media/{name}", osig, oexp, now=now) == "expired"

    fresh = media.remint_link(expired, now=now)
    exp = str(int(now) + 3600)  # remote_link_ttl_s(), minted at `now`
    # A fresh EXPIRING link for the same file, made absolute (base-agnostic: STATE.active_port's
    # default would otherwise bake a host into the expected string).
    assert fresh == media.absolute_media_url(f"/media/{name}?exp={exp}&sig={media.sign_name(name, exp)}")
    assert media.check_access(f"/media/{name}", media.sign_name(name, exp), exp, now=now + 1) == "ok"

    # A member's history link is hub-prefixed and absolute; only the /media/<name> path + sig/exp
    # are read, so those forms re-mint identically.
    assert media.remint_link(f"http://hub/agents/roxy-1a2b{expired}", now=now) == fresh


def test_remint_refuses_forged_unsigned_missing_traversal_and_empty():
    """r2: forged sig, missing file, traversal, unsigned, and empty input each return None."""
    ref = media.save_media(b"png", "image/png")
    name = ref.path.name
    assert media.remint_link(f"/media/{name}") is None  # unsigned — never conjure a link
    assert media.remint_link(f"/media/{name}?sig=bogus") is None  # forged (malformed sig)
    assert media.remint_link(f"/media/{name}?exp=1000&sig={'a' * 64}") is None  # forged (well-formed shape)
    assert media.remint_link(f"/media/missing.png?sig={media.sign_name('missing.png')}") is None  # file gone
    assert media.remint_link(f"/media/.signing-key?sig={media.sign_name('.signing-key')}") is None  # store internal
    assert media.remint_link(f"/media/../secrets.yaml?sig={'a' * 64}") is None  # traversal
    assert media.remint_link("/api/x?sig=f") is None  # not a media path
    assert media.remint_link("") is None  # empty input


# ── the route (server/media.py) ──────────────────────────────────────────────────


def _route_client():
    from server.media import register_media_routes

    app = FastAPI()
    register_media_routes(app)
    return TestClient(app)


def _expired_link(name: str) -> str:
    return media.media_url(name, ttl_s=10, now=time.time() - 100)


def test_remint_route_returns_fresh_link_or_404():
    ref = media.save_media(b"png", "image/png")
    name = ref.path.name
    c = _route_client()
    r = c.post("/api/media/remint", json={"url": _expired_link(name)})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"url"}  # exactly {url} — no expires_in (unlike open-link)
    rel = body["url"][body["url"].index("/media/") :]  # strip any absolute base
    assert rel.startswith(f"/media/{name}?exp=")
    assert c.get(rel).content == b"png"  # the fresh link really serves the file
    # forged / missing / empty → 404
    assert c.post("/api/media/remint", json={"url": f"/media/{name}?sig=bogus"}).status_code == 404
    assert c.post("/api/media/remint", json={"url": f"/media/missing.png?sig={media.sign_name('missing.png')}"}).status_code == 404
    assert c.post("/api/media/remint", json={}).status_code == 404


@pytest.fixture()
def _bearer_guard():
    auth.configure(bearer_token="secret-token", api_key="", allowed_origins_raw="")
    yield auth
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._FLEET[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


def test_remint_route_is_behind_the_bearer_gate(_bearer_guard):
    """r3: the route needs the operator bearer, and a valid signature in the body does not
    exempt it (it is not a media path)."""
    ref = media.save_media(b"png", "image/png")
    app = FastAPI()
    from server.media import register_media_routes

    register_media_routes(app)
    app.add_middleware(_bearer_guard.A2AAuthMiddleware)
    c = TestClient(app)
    link = _expired_link(ref.path.name)
    assert c.post("/api/media/remint", json={"url": link}).status_code == 401
    ok = c.post("/api/media/remint", json={"url": link}, headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
    assert f"/media/{ref.path.name}?exp=" in ok.json()["url"]


def test_remint_never_logs_the_signature(caplog):
    """r5: no sig value (nor the full signed URL) reaches the logs, on success OR refusal."""
    ref = media.save_media(b"png", "image/png")
    name = ref.path.name
    exp = str(int(time.time()) - 100)
    sig = media.sign_name(name, exp)
    c = _route_client()
    with caplog.at_level(logging.DEBUG):
        assert c.post("/api/media/remint", json={"url": f"/media/{name}?exp={exp}&sig={sig}"}).status_code == 200
        assert c.post("/api/media/remint", json={"url": f"/media/{name}?exp={exp}&sig={'f' * 64}"}).status_code == 404
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert sig not in blob
    assert f"/media/{name}" not in blob  # the full signed URL/path is never logged either


# ── through the fleet hub (as in tests/test_fleet_media_links.py) ─────────────────


class _Seen:
    """What reached the member: (path, query, Authorization header) per request."""

    def __init__(self):
        self.requests: list[tuple[str, str, str | None]] = []


@pytest.fixture
def member():
    """A real member — the core media routes behind the real default-deny middleware, which
    accepts the fleet service token — served on a free port. Yields ``(base_url, seen)``."""
    from server.media import register_media_routes

    seen = _Seen()
    app = FastAPI()
    register_media_routes(app)
    auth.install(app, bearer_token="tok", api_key="", allowed_origins_raw="", fleet_token=FLEET)

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


def _hub(monkeypatch, member_base: str) -> TestClient:
    """A token-gated hub proxying slug ``m`` to the local member, swapping the operator caller's
    credential for the fleet service token the member expects."""
    import graph.fleet.service_token as st

    monkeypatch.setattr(st, "resolve_service_token", lambda: FLEET)
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (member_base, {}) if slug == "m" else None)
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", member_base, None) if slug == "m" else None)
    from operator_api.fleet_routes import register_fleet_routes

    app = FastAPI()
    register_fleet_routes(app)
    auth.install(app, bearer_token="tok", api_key="", allowed_origins_raw="", fleet_token=FLEET)
    return TestClient(app)


def test_remint_through_the_hub_reaches_the_member(monkeypatch, member):
    """r4: POST /agents/<slug>/api/media/remint rides the normal operator proxy to the member and
    returns its freshly re-minted link."""
    base, seen = member
    ref = media.save_media(b"chart", "image/png")
    name = ref.path.name
    hub = _hub(monkeypatch, base)
    past = str(int(time.time()) - 100)
    # The expiring link as it sits in the member's hub-console history (hub-prefixed, past its exp).
    expired = f"/agents/m/media/{name}?exp={past}&sig={media.sign_name(name, past)}"
    r = hub.post("/agents/m/api/media/remint", json={"url": expired}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200, r.text
    fresh = r.json()["url"]
    assert f"/media/{name}?exp=" in fresh
    sig, exp = media._query_value(fresh, "sig"), media._query_value(fresh, "exp")
    assert media.check_access(f"/media/{name}", sig, exp) == "ok"  # fresh + unexpired

    # It really reached the member's /api route, carrying the swapped fleet token — never the
    # caller's hub bearer.
    assert seen.requests[-1][0] == "/api/media/remint"
    assert seen.requests[-1][2] == f"Bearer {FLEET}"


def test_remint_through_the_hub_needs_the_hub_bearer(monkeypatch, member):
    """r3/r4: unauthenticated, the hub 401s before the member is ever dialled — a valid signature
    in the body does not exempt an /api path."""
    base, seen = member
    ref = media.save_media(b"x", "image/png")
    hub = _hub(monkeypatch, base)
    past = str(int(time.time()) - 100)
    expired = f"/agents/m/media/{ref.path.name}?exp={past}&sig={media.sign_name(ref.path.name, past)}"
    assert hub.post("/agents/m/api/media/remint", json={"url": expired}).status_code == 401
    assert seen.requests == []
