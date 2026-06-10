"""Tests for the A2A auth/origin middleware (#482).

Two correctness fixes, each pinned here:

1. ``configure(bearer_token=...)`` is authoritative — only ``None`` falls back
   to ``A2A_AUTH_TOKEN``; an explicit ``""`` keeps bearer off even if the env
   var is set (an apiKey-only agent must not silently enable bearer auth its
   card never advertises).
2. The origin guard only fires when an ``Origin`` header is actually present —
   server-to-server callers (hub, scheduler loopback) send none and must pass.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import a2a_auth


@pytest.fixture(autouse=True)
def _reset_guard():
    """Each test seeds the guard itself; reset module state around it."""
    a2a_auth._BEARER[0] = None
    a2a_auth._API_KEY[0] = ""
    a2a_auth._ALLOWED_ORIGINS[0] = None
    yield
    a2a_auth._BEARER[0] = None
    a2a_auth._API_KEY[0] = ""
    a2a_auth._ALLOWED_ORIGINS[0] = None


def _client() -> TestClient:
    app = Starlette(routes=[Route("/a2a", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    app.add_middleware(a2a_auth.A2AAuthMiddleware)
    return TestClient(app)


# ── 1. bearer_token is authoritative ─────────────────────────────────────────


def test_empty_bearer_does_not_fall_back_to_env(monkeypatch):
    # apiKey-only agent passes "" explicitly; a stray env var must NOT turn bearer on.
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    a2a_auth.configure(bearer_token="", api_key="", allowed_origins_raw="")
    assert a2a_auth._BEARER[0] is None
    # endpoint is open for bearer — no Authorization header still succeeds.
    assert _client().post("/a2a").status_code == 200


def test_none_bearer_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    a2a_auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    assert a2a_auth._BEARER[0] == "env-secret"
    c = _client()
    assert c.post("/a2a").status_code == 401  # missing header
    assert c.post("/a2a", headers={"Authorization": "Bearer env-secret"}).status_code == 200


def test_explicit_bearer_wins_over_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    a2a_auth.configure(bearer_token="yaml-secret", api_key="", allowed_origins_raw="")
    assert a2a_auth._BEARER[0] == "yaml-secret"


def test_no_bearer_no_env_is_open_mode(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    a2a_auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    assert a2a_auth._BEARER[0] is None


# ── 2. origin guard is browser-only (header-less callers pass) ────────────────


def test_origin_guard_allows_header_less_caller():
    a2a_auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    # server-to-server: no Origin header → must pass (was a 403 before the fix).
    assert _client().post("/a2a").status_code == 200


def test_origin_guard_allows_listed_origin():
    a2a_auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    r = _client().post("/a2a", headers={"Origin": "https://app.example"})
    assert r.status_code == 200


def test_origin_guard_rejects_unlisted_origin():
    a2a_auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    r = _client().post("/a2a", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_origin_guard_disabled_when_unset():
    a2a_auth.configure(bearer_token="", api_key="", allowed_origins_raw="")
    assert _client().post("/a2a", headers={"Origin": "https://anything.example"}).status_code == 200


# ── 3. guard covers the console + OpenAI-compat APIs (prod-readiness) ──────────


def _client_multi() -> TestClient:
    routes = [
        Route(p, lambda r: PlainTextResponse("ok"), methods=["GET", "POST"])
        for p in ("/a2a", "/api/config", "/api/events", "/v1/chat/completions", "/healthz")
    ]
    app = Starlette(routes=routes)
    app.add_middleware(a2a_auth.A2AAuthMiddleware)
    return TestClient(app)


def test_api_and_v1_are_guarded_when_token_set(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    a2a_auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    # operator API + OpenAI-compat now require the bearer (the P0 gap)
    assert c.post("/api/config").status_code == 401
    assert c.post("/v1/chat/completions").status_code == 401
    assert c.post("/a2a").status_code == 401
    hdr = {"Authorization": "Bearer secret"}
    assert c.post("/api/config", headers=hdr).status_code == 200
    assert c.post("/v1/chat/completions", headers=hdr).status_code == 200


def test_events_stream_and_healthz_stay_public_when_token_set(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    a2a_auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    # EventSource can't send a bearer; the read-only event stream is exempt.
    assert c.get("/api/events").status_code == 200
    # /healthz is outside the guarded prefixes (probes/scrapers stay open).
    assert c.get("/healthz").status_code == 200


def test_apis_open_when_no_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    a2a_auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    c = _client_multi()
    # default (no token) → everything open (local console keeps working)
    for p in ("/a2a", "/api/config", "/v1/chat/completions", "/api/events", "/healthz"):
        assert c.post(p).status_code in (200, 405)  # 405 only if method not allowed


# ── 4. boot gate: non-loopback bind without a token ──────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_open_bind_loopback_always_allowed(host):
    allowed, msg = a2a_auth.evaluate_open_bind(host, bearer_configured=False, allow_open=False)
    assert allowed and msg is None


def test_open_bind_with_token_allowed_silently():
    allowed, msg = a2a_auth.evaluate_open_bind("0.0.0.0", bearer_configured=True, allow_open=False)
    assert allowed and msg is None


def test_open_bind_without_token_refused():
    allowed, msg = a2a_auth.evaluate_open_bind("0.0.0.0", bearer_configured=False, allow_open=False)
    assert not allowed
    assert "refusing" in msg and "0.0.0.0" in msg


def test_open_bind_optin_allowed_with_warning():
    allowed, msg = a2a_auth.evaluate_open_bind("0.0.0.0", bearer_configured=False, allow_open=True)
    assert allowed
    assert msg is not None and "0.0.0.0" in msg and "PROTOAGENT_ALLOW_OPEN" in msg
