"""Tests for the A2A auth/origin middleware — default-deny posture (#870).

Covers the inverted auth model (default-deny + public allowlist), the
short-lived SSE query-string token, plugin prefix enforcement, and the
boot gate.

Acceptance criteria: AC1–AC14 from the #870 spec.
"""

from __future__ import annotations

import time

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_impl import auth


@pytest.fixture(autouse=True)
def _reset_guard():
    """Each test seeds the guard itself; reset module state around it."""
    auth._BEARER[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None
    yield
    auth._BEARER[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


def _client() -> TestClient:
    app = Starlette(routes=[Route("/a2a", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    app.add_middleware(auth.A2AAuthMiddleware)
    return TestClient(app)


# ── 1. bearer_token is authoritative ─────────────────────────────────────────


def test_empty_bearer_does_not_fall_back_to_env(monkeypatch):
    # apiKey-only agent passes "" explicitly; a stray env var must NOT turn bearer on.
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    auth.configure(bearer_token="", api_key="", allowed_origins_raw="")
    assert auth._BEARER[0] is None
    # endpoint is open for bearer — no Authorization header still succeeds.
    assert _client().post("/a2a").status_code == 200


def test_none_bearer_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    assert auth._BEARER[0] == "env-secret"
    c = _client()
    assert c.post("/a2a").status_code == 401  # missing header
    assert c.post("/a2a", headers={"Authorization": "Bearer env-secret"}).status_code == 200


def test_explicit_bearer_wins_over_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-secret")
    auth.configure(bearer_token="yaml-secret", api_key="", allowed_origins_raw="")
    assert auth._BEARER[0] == "yaml-secret"


def test_no_bearer_no_env_is_open_mode(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    assert auth._BEARER[0] is None


# ── 2. origin guard is browser-only (header-less callers pass) ────────────────


def test_origin_guard_allows_header_less_caller():
    auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    # server-to-server: no Origin header → must pass (was a 403 before the fix).
    assert _client().post("/a2a").status_code == 200


def test_origin_guard_allows_listed_origin():
    auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    r = _client().post("/a2a", headers={"Origin": "https://app.example"})
    assert r.status_code == 200


def test_origin_guard_rejects_unlisted_origin():
    auth.configure(bearer_token="", api_key="", allowed_origins_raw="https://app.example")
    r = _client().post("/a2a", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_origin_guard_disabled_when_unset():
    auth.configure(bearer_token="", api_key="", allowed_origins_raw="")
    assert _client().post("/a2a", headers={"Origin": "https://anything.example"}).status_code == 200


# ── 3. default-deny: non-public paths are guarded ─────────────────────────────

_ALL_ROUTES = [
    Route(p, lambda r: PlainTextResponse("ok"), methods=["GET", "POST"])
    for p in (
        "/a2a",
        "/api/config",
        "/api/events",
        "/api/sse-token",
        "/api/subagents/run",
        "/v1/chat/completions",
        "/healthz",
        "/metrics",
        "/.well-known/agent-card.json",
        "/app",
        "/app/settings",
        "/manifest.json",
        "/sw.js",
        "/favicon.svg",
        "/favicon.ico",
        "/plugins/example/status",
        "/active/foo/api/config",
        "/agents/slug/api/config",
    )
]


def _client_multi() -> TestClient:
    app = Starlette(routes=_ALL_ROUTES)
    app.add_middleware(auth.A2AAuthMiddleware)
    return TestClient(app)


# AC1: non-public paths return 401 without bearer
def test_ac1_default_deny_non_public_paths(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    for p in (
        "/a2a",
        "/api/config",
        "/api/subagents/run",
        "/v1/chat/completions",
        "/plugins/example/status",
        "/active/foo/api/config",
        "/agents/slug/api/config",
    ):
        assert c.get(p).status_code == 401, f"{p} should be 401"
        assert c.post(p).status_code == 401, f"POST {p} should be 401"


# AC2: /healthz is public
def test_ac2_healthz_public(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    assert _client_multi().get("/healthz").status_code == 200


# AC3: /metrics is public
def test_ac3_metrics_public(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    assert _client_multi().get("/metrics").status_code == 200


# AC4: /.well-known/agent-card.json is public
def test_ac4_agent_card_public(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    assert _client_multi().get("/.well-known/agent-card.json").status_code == 200


# A plugin-declared public prefix is exempted; a core path can never be, and the
# set replaces cleanly (reload-safe).
def test_plugin_public_prefix_exempts_only_namespaced(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    try:
        auth.set_public_prefixes(["/plugins/example/status"])
        c = _client_multi()
        assert c.get("/plugins/example/status").status_code == 200   # now exempt (e.g. a webhook)
        assert c.post("/plugins/example/status").status_code == 200
        assert c.get("/api/config").status_code == 401               # core path untouched
        # A plugin cannot exempt a core path — set_public_prefixes drops it.
        auth.set_public_prefixes(["/api/config"])
        assert _client_multi().get("/api/config").status_code == 401
    finally:
        auth.set_public_prefixes([])  # reset module state for other tests
        assert _client_multi().get("/plugins/example/status").status_code == 401


# AC5: /app is public (SPA served without auth)
def test_ac5_app_public(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/app").status_code == 200
    assert c.get("/app/settings").status_code == 200


# AC6: /favicon.svg is public
def test_ac6_favicon_public(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/favicon.svg").status_code == 200
    assert c.get("/favicon.ico").status_code == 200


# AC7: SSE with valid query token passes
def test_ac7_sse_valid_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token("test-session")
    c = _client_multi()
    assert c.get(f"/api/events?token={token}").status_code == 200


# AC8: SSE without token returns 401
def test_ac8_sse_no_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/api/events").status_code == 401


# AC9: SSE with stale/forged token returns 401
def test_ac9_sse_bad_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/api/events?token=forged-garbage").status_code == 401


def test_ac9_sse_expired_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    # Generate a token, then move time forward past the lifetime.
    token = auth.generate_sse_token("test")
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + auth._SSE_TOKEN_LIFETIME + 5)
    c = _client_multi()
    assert c.get(f"/api/events?token={token}").status_code == 401


# AC10: valid bearer on /api/* passes
def test_ac10_bearer_passes_api(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    hdr = {"Authorization": "Bearer secret"}
    assert c.post("/api/subagents/run", headers=hdr).status_code == 200
    assert c.post("/api/config", headers=hdr).status_code == 200


# AC11: plugin routes are guarded by default-deny
def test_ac11_plugin_routes_guarded(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/plugins/example/status").status_code == 401
    # With bearer: passes
    assert c.get("/plugins/example/status", headers={"Authorization": "Bearer secret"}).status_code == 200


# AC12: open mode — no 401 on any path
def test_ac12_open_mode_no_401(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    c = _client_multi()
    for p in (
        "/a2a",
        "/api/config",
        "/api/events",
        "/v1/chat/completions",
        "/healthz",
        "/metrics",
        "/app",
        "/plugins/example/status",
        "/api/subagents/run",
    ):
        resp = c.get(p)
        assert resp.status_code != 401, f"GET {p} should not be 401 in open mode"


# AC13: tested in test_plugin_route_hotmount.py — _mount_plugin_routers warning
# (see test_mount_warns_non_conforming_prefix below for the core logic)


def test_ac13_mount_warns_non_conforming_prefix(monkeypatch, caplog):
    """_mount_plugin_routers logs a WARNING for a non-conforming prefix."""
    import logging

    from fastapi import APIRouter, FastAPI

    from runtime.state import STATE
    from server.agent_init import _mount_plugin_routers

    app = FastAPI()
    monkeypatch.setattr(STATE, "fastapi_app", app)
    monkeypatch.setattr(STATE, "plugin_router_keys", set())

    r = APIRouter()

    @r.get("/ping")
    def _ping():
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        _mount_plugin_routers([{"plugin_id": "myplugin", "router": r, "prefix": "/custom/path"}])

    # The router was still mounted (not rejected).
    assert ("myplugin", "/custom/path") in STATE.plugin_router_keys
    # A warning was logged.
    assert any("does not start with /plugins/myplugin/" in m for m in caplog.messages)


# AC14: /api/sse-token endpoint returns a token
def test_ac14_sse_token_endpoint():
    """generate_sse_token returns a valid token when bearer is configured."""
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token("session-1")
    assert token  # non-empty
    assert auth._validate_sse_token(token)


def test_ac14_sse_token_empty_in_open_mode(monkeypatch):
    """generate_sse_token returns empty string when no bearer is configured."""
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token()
    assert token == ""


# ── 4. SSE token mechanics ──────────────────────────────────────────────────


def test_sse_token_roundtrip():
    auth.configure(bearer_token="my-secret", api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token("sess-42")
    assert auth._validate_sse_token(token)


def test_sse_token_rejects_wrong_key():
    auth.configure(bearer_token="my-secret", api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token("sess")
    # Swap the bearer to a different key — token should no longer validate.
    auth._BEARER[0] = "different-secret"
    assert not auth._validate_sse_token(token)


def test_sse_token_rejects_garbage():
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    assert not auth._validate_sse_token("not-base64-!!!")
    assert not auth._validate_sse_token("")


def test_sse_bearer_header_still_works_for_events(monkeypatch):
    """Server-to-server callers with Authorization header pass /api/events."""
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    assert c.get("/api/events", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_sse_proxied_events_path(monkeypatch):
    """/agents/<slug>/api/events with a valid token passes."""
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    token = auth.generate_sse_token()
    routes = [Route("/agents/myagent/api/events", lambda r: PlainTextResponse("ok"), methods=["GET"])]
    app = Starlette(routes=routes)
    app.add_middleware(auth.A2AAuthMiddleware)
    c = TestClient(app)
    assert c.get(f"/agents/myagent/api/events?token={token}").status_code == 200
    assert c.get("/agents/myagent/api/events").status_code == 401


# ── 5. guard covers the console + OpenAI-compat APIs (prod-readiness) ──────────


def test_api_and_v1_are_guarded_when_token_set(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    # operator API + OpenAI-compat now require the bearer (the P0 gap)
    assert c.post("/api/config").status_code == 401
    assert c.post("/v1/chat/completions").status_code == 401
    assert c.post("/a2a").status_code == 401
    hdr = {"Authorization": "Bearer secret"}
    assert c.post("/api/config", headers=hdr).status_code == 200
    assert c.post("/v1/chat/completions", headers=hdr).status_code == 200


def test_public_paths_stay_public_when_token_set(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client_multi()
    # Public allowlist paths are always accessible.
    assert c.get("/healthz").status_code == 200
    assert c.get("/metrics").status_code == 200
    assert c.get("/.well-known/agent-card.json").status_code == 200
    assert c.get("/app").status_code == 200
    assert c.get("/manifest.json").status_code == 200
    assert c.get("/sw.js").status_code == 200
    assert c.get("/favicon.svg").status_code == 200
    assert c.get("/favicon.ico").status_code == 200


def test_apis_open_when_no_token(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")
    c = _client_multi()
    # default (no token) → everything open (local console keeps working)
    for p in ("/a2a", "/api/config", "/v1/chat/completions", "/api/events", "/healthz"):
        assert c.post(p).status_code in (200, 405)  # 405 only if method not allowed


# ── 6. boot gate: non-loopback bind without a token ──────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_open_bind_loopback_always_allowed(host):
    allowed, msg = auth.evaluate_open_bind(host, bearer_configured=False, allow_open=False)
    assert allowed and msg is None


def test_open_bind_with_token_allowed_silently():
    allowed, msg = auth.evaluate_open_bind("0.0.0.0", bearer_configured=True, allow_open=False)
    assert allowed and msg is None


def test_open_bind_without_token_refused():
    allowed, msg = auth.evaluate_open_bind("0.0.0.0", bearer_configured=False, allow_open=False)
    assert not allowed
    assert "refusing" in msg and "0.0.0.0" in msg


def test_open_bind_optin_allowed_with_warning():
    allowed, msg = auth.evaluate_open_bind("0.0.0.0", bearer_configured=False, allow_open=True)
    assert allowed
    assert msg is not None and "0.0.0.0" in msg and "PROTOAGENT_ALLOW_OPEN" in msg


# ── 7. _is_public coverage ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/healthz", True),
        ("/metrics", True),
        ("/.well-known/agent-card.json", True),
        ("/.well-known/other", True),
        ("/app", True),
        ("/app/settings/auth", True),
        ("/manifest.json", True),
        ("/sw.js", True),
        ("/favicon.svg", True),
        ("/favicon.ico", True),
        ("/static/js/main.js", True),
        ("/_ds/plugin-kit.css", True),
        ("/_ds/plugin-kit.js", True),
        ("/a2a", False),
        ("/api/config", False),
        ("/api/events", False),
        ("/v1/chat/completions", False),
        ("/plugins/foo/bar", False),
        ("/", False),
        ("/random", False),
    ],
)
def test_is_public(path, expected):
    assert auth._is_public(path) is expected
