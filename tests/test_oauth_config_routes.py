"""Route-level contract for the operator OAuth sign-in surface (ADR 0097).

``GET /api/config/oauth-status`` and ``POST /api/config/oauth/{import,start,poll,complete}``
in ``operator_api/config_routes.py`` — the HTTP layer over ``graph.providers.oauth`` +
``graph.providers.oauth_login``. These routes move credentials, so the pins here are:

* no response ever carries token material (access/refresh/id tokens, the device-auth
  id, the PKCE verifier);
* bad input is a 4xx (or, for poll/complete, the documented ``{status: "error"}``
  envelope the console renders) — never a 500;
* a completed flow writes protoAgent's own store where the resolver reads it, owner-only
  (0600), and ``oauth-status`` flips to signed in;
* the flow store binds a ``flow_id`` to its provider and to its ``state``.

The network boundary is faked at ``httpx.post`` (both provider modules call it through
the shared ``httpx`` module); every credential path — protoAgent's box store, the Codex
CLI auth file, Claude Code's credentials file and Keychain — is pinned under tmp_path.
"""

from __future__ import annotations

import base64
import json
import stat
import sys
import time
import types

import httpx
import pytest

from graph.providers import oauth as oauth_mod
from graph.providers import oauth_login as login_mod
from runtime.state import STATE

# Asserts on the stored file's POSIX mode (skipped on Windows via sys.platform).
pytestmark = pytest.mark.platform_sensitive


# ── harness ───────────────────────────────────────────────────────────────────


def _jwt(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{seg}.s"


_ACCT = "acct-1234567890"
_LIVE_ACCESS = _jwt({"exp": time.time() + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": _ACCT}})
_ROTATED_ACCESS = _jwt({"exp": time.time() + 7200, "https://api.openai.com/auth": {"chatgpt_account_id": _ACCT}})


class _FakeNet:
    """Stands in for ``httpx.post``: routes by URL, records every call, and fails loudly
    on anything it wasn't told about (so a test can never reach a real endpoint)."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    def on(self, url: str, status: int = 200, body: dict | None = None, *, exc: Exception | None = None) -> None:
        self.routes[url] = exc if exc is not None else (status, body or {})

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if url not in self.routes:
            raise AssertionError(f"unexpected outbound POST {url}")
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        status, body = route
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


_USERCODE_URL = f"{login_mod._OPENAI_ISSUER}/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = f"{login_mod._OPENAI_ISSUER}/api/accounts/deviceauth/token"


@pytest.fixture
def net(monkeypatch):
    fake = _FakeNet()
    monkeypatch.setattr(httpx, "post", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch, net):
    """A TestClient over ``register_config_routes`` with every credential source
    isolated: box store under the conftest's tmp box root, the vendor CLI files pointed
    into tmp_path, the Keychain stubbed out, the flow store emptied, and the post-sign-in
    graph rebuild disabled (setup incomplete → ``_rebuild_graph_after_reconnect`` no-ops)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.config_routes import register_config_routes

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "vendor" / "codex-auth.json")
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "vendor" / "claude-credentials.json")
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", lambda: None)
    monkeypatch.setattr(login_mod, "_FLOWS", {})
    monkeypatch.setattr("graph.config_io.is_setup_complete", lambda: False)
    monkeypatch.setattr(STATE, "graph", None)

    app = FastAPI()
    register_config_routes(app)
    return TestClient(app)


def _status(client, provider: str) -> dict:
    res = client.get("/api/config/oauth-status")
    assert res.status_code == 200
    return next(p for p in res.json()["providers"] if p["provider"] == provider)


def _codex_store():
    from infra.paths import instance_paths

    return oauth_mod._codex_store_path(instance_paths())


def _assert_owner_only(path) -> None:
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)


def _assert_no_secrets(text: str, *secrets: str) -> None:
    for s in secrets:
        assert s and s not in text, f"secret material leaked into the response: {s[:12]}…"


# ── GET /api/config/oauth-status ──────────────────────────────────────────────


def test_status_signed_out_shape(client):
    res = client.get("/api/config/oauth-status")
    assert res.status_code == 200
    providers = res.json()["providers"]
    assert sorted(p["provider"] for p in providers) == ["anthropic-oauth", "openai-codex"]
    for p in providers:
        assert set(p) == {
            "provider", "signed_in", "source", "detail", "hint", "expires_at", "refreshable", "durability",
        }  # fmt: skip
        assert p["signed_in"] is False
        assert p["source"] == ""
        assert "/api/config/oauth/start" in p["hint"]


def test_status_signed_in_never_returns_token_material(client):
    refresh_c = "rt-codex-SECRET"
    store = _codex_store()
    oauth_mod._write_codex_store(store, {"access_token": _LIVE_ACCESS, "refresh_token": refresh_c, "id_token": "idtok-SECRET"})
    oauth_mod._write_anthropic_store({"access_token": "sk-ant-oat-SECRET", "refresh_token": "sk-ant-ort-SECRET", "expires_in": 3600})

    res = client.get("/api/config/oauth-status")
    assert res.status_code == 200
    by = {p["provider"]: p for p in res.json()["providers"]}
    assert by["openai-codex"]["signed_in"] is True
    assert by["openai-codex"]["refreshable"] is True
    assert by["anthropic-oauth"]["signed_in"] is True
    assert by["anthropic-oauth"]["source"] == "instance_store"
    _assert_no_secrets(res.text, _LIVE_ACCESS, refresh_c, "idtok-SECRET", "sk-ant-oat-SECRET", "sk-ant-ort-SECRET")
    # The account is shown only as a short suffix, never the whole id.
    assert _ACCT not in res.text and _ACCT[-6:] in by["openai-codex"]["detail"]


def test_status_disconnect_marker_reads_signed_out_even_with_a_store(client):
    from infra.paths import instance_paths

    oauth_mod._write_codex_store(_codex_store(), {"access_token": _LIVE_ACCESS, "refresh_token": "rt"})
    oauth_mod._mark_disconnected(instance_paths(), "openai-codex")
    p = _status(client, "openai-codex")
    assert p["signed_in"] is False and p["detail"] == "disconnected"


# ── POST /api/config/oauth/import ─────────────────────────────────────────────


def _write_cli_auth(tmp_path, tokens: dict | None = None, raw: str | None = None) -> None:
    path = tmp_path / "vendor" / "codex-auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"tokens": tokens}), encoding="utf-8")


@pytest.mark.parametrize("provider", ["", "anthropic-oauth", "openai", "OPENAI-CODEX-X"])
def test_import_rejects_any_provider_but_codex(client, net, provider):
    res = client.post("/api/config/oauth/import", json={"provider": provider})
    assert res.status_code == 400
    assert "only available for openai-codex" in res.json()["detail"]
    assert net.calls == []


def test_import_malformed_body_is_422_not_500(client, net):
    assert client.post("/api/config/oauth/import", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/api/config/oauth/import", json={"provider": ["openai-codex"]}).status_code == 422
    assert net.calls == []


@pytest.mark.parametrize(
    "cli",
    [
        None,  # no CLI auth file at all
        "{not json",  # corrupt file
        {"access_token": _LIVE_ACCESS},  # no refresh token
        {"access_token": _jwt({"exp": time.time() - 60}), "refresh_token": "rt"},  # already expired
    ],
    ids=["missing", "corrupt", "no-refresh", "expired"],
)
def test_import_without_a_usable_cli_login_is_400_and_writes_nothing(client, net, tmp_path, cli):
    if isinstance(cli, str):
        _write_cli_auth(tmp_path, raw=cli)
    elif isinstance(cli, dict):
        _write_cli_auth(tmp_path, cli)
    res = client.post("/api/config/oauth/import", json={"provider": "openai-codex"})
    assert res.status_code == 400
    assert "no usable login to import" in res.json()["detail"]
    assert net.calls == []  # never spends a refresh token it can't use
    assert not _codex_store().exists()


def test_import_happy_path_rotates_stores_and_signs_in(client, net, tmp_path):
    from infra.paths import instance_paths

    cli_refresh, new_refresh = "rt-cli-SECRET", "rt-rotated-SECRET"
    _write_cli_auth(tmp_path, {"access_token": _LIVE_ACCESS, "refresh_token": cli_refresh})
    oauth_mod._mark_disconnected(instance_paths(), "openai-codex")
    net.on(oauth_mod._CODEX_TOKEN_URL, body={"access_token": _ROTATED_ACCESS, "refresh_token": new_refresh, "id_token": "id-SECRET"})

    res = client.post("/api/config/oauth/import", json={"provider": " OpenAI-Codex "})
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "complete", "account_id": _ACCT, "cli_needs_relogin": True}
    _assert_no_secrets(res.text, _LIVE_ACCESS, _ROTATED_ACCESS, cli_refresh, new_refresh, "id-SECRET")

    # Rotated immediately (the handover), spending the CLI's refresh token exactly once.
    assert net.urls() == [oauth_mod._CODEX_TOKEN_URL]
    assert net.calls[0][1]["data"]["refresh_token"] == cli_refresh

    # Stored where the resolver reads it, owner-only, with import provenance.
    store = _codex_store()
    doc = json.loads(store.read_text())
    assert doc["tokens"]["access_token"] == _ROTATED_ACCESS
    assert doc["tokens"]["refresh_token"] == new_refresh
    assert doc["provenance"] == oauth_mod.PROVENANCE_CLI_BOOTSTRAP
    _assert_owner_only(store)
    assert oauth_mod.resolve_codex_oauth().access_token == _ROTATED_ACCESS

    # An explicit import clears a prior disconnect; status flips to signed in.
    assert not oauth_mod.is_disconnected("openai-codex")
    assert _status(client, "openai-codex")["signed_in"] is True


@pytest.mark.parametrize(
    "route",
    [(401, None), (500, None), (200, "no-access"), (0, httpx.ConnectError("boom"))],
    ids=["rejected-401", "upstream-500", "missing-access-token", "unreachable"],
)
def test_import_refresh_failure_is_400_and_writes_nothing(client, net, tmp_path, route):
    status, exc = route
    _write_cli_auth(tmp_path, {"access_token": _LIVE_ACCESS, "refresh_token": "rt-cli-SECRET"})
    if isinstance(exc, Exception):
        net.on(oauth_mod._CODEX_TOKEN_URL, exc=exc)
    else:
        net.on(oauth_mod._CODEX_TOKEN_URL, status, {"refresh_token": "x"} if exc == "no-access" else {"error": "invalid_grant"})
    res = client.post("/api/config/oauth/import", json={"provider": "openai-codex"})
    assert res.status_code == 400
    _assert_no_secrets(res.text, "rt-cli-SECRET", _LIVE_ACCESS)
    assert not _codex_store().exists()
    assert _status(client, "openai-codex")["signed_in"] is False


# ── POST /api/config/oauth/start ──────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["", "openai", "anthropic", "gateway", "../../etc"])
def test_start_unknown_provider_is_400(client, net, provider):
    res = client.post("/api/config/oauth/start", json={"provider": provider})
    assert res.status_code == 400
    assert "no sign-in flow for provider" in res.json()["detail"]
    assert net.calls == [] and login_mod._FLOWS == {}


def test_start_codex_returns_device_code_without_the_device_auth_id(client, net):
    net.on(_USERCODE_URL, body={"user_code": "ABCD-1234", "device_auth_id": "dev-auth-SECRET", "interval": 1})
    res = client.post("/api/config/oauth/start", json={"provider": "openai-codex"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"flow_id", "mode", "user_code", "verification_uri", "interval"}
    assert body["mode"] == "device"
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_uri"] == "https://auth.openai.com/codex/device"
    assert body["interval"] == 3  # floored so the console can't hammer OpenAI
    # The device_auth_id is what redeems the approval — it stays server-side.
    _assert_no_secrets(res.text, "dev-auth-SECRET")
    assert login_mod._FLOWS[body["flow_id"]].provider == "openai-codex"


@pytest.mark.parametrize(
    "route, detail",
    [
        ((429, {}, None), "rate-limiting"),
        ((500, {}, None), "HTTP 500"),
        ((200, {"user_code": "X"}, None), "incomplete device code"),
        ((0, None, httpx.ConnectError("down")), "Could not reach OpenAI"),
    ],
    ids=["429", "500", "incomplete", "unreachable"],
)
def test_start_codex_upstream_failures_are_400(client, net, route, detail):
    status, body, exc = route
    net.on(_USERCODE_URL, status, body, exc=exc)
    res = client.post("/api/config/oauth/start", json={"provider": "openai-codex"})
    assert res.status_code == 400
    assert detail in res.json()["detail"]
    assert login_mod._FLOWS == {}


def test_start_claude_returns_authorize_url_without_the_pkce_verifier(client, net):
    from urllib.parse import parse_qs, urlparse

    res = client.post("/api/config/oauth/start", json={"provider": "anthropic-oauth"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"flow_id", "mode", "authorize_url"}
    assert body["mode"] == "redirect"
    assert net.calls == []  # nothing leaves the box until the code is pasted

    flow = login_mod._FLOWS[body["flow_id"]]
    q = parse_qs(urlparse(body["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == [flow.data["state"]]
    # The challenge is public; the verifier that proves it must never be sent out.
    _assert_no_secrets(res.text, flow.data["code_verifier"])
    assert q["code_challenge"] != [flow.data["code_verifier"]]


def test_start_sweeps_flows_older_than_the_ttl(client, net, monkeypatch):
    first = client.post("/api/config/oauth/start", json={"provider": "anthropic-oauth"}).json()["flow_id"]
    later = time.time() + login_mod._FLOW_TTL_S + 1
    monkeypatch.setattr(login_mod, "_now", lambda: later)
    client.post("/api/config/oauth/start", json={"provider": "anthropic-oauth"})
    assert first not in login_mod._FLOWS
    res = client.post("/api/config/oauth/complete", json={"flow_id": first, "code": "c#s"})
    assert res.json() == {"status": "error", "error": "Sign-in session expired — start again."}


# ── POST /api/config/oauth/poll (Codex device flow) ───────────────────────────


def _start_codex(client, net) -> str:
    net.on(_USERCODE_URL, body={"user_code": "ABCD-1234", "device_auth_id": "dev-auth-SECRET"})
    return client.post("/api/config/oauth/start", json={"provider": "openai-codex"}).json()["flow_id"]


@pytest.mark.parametrize("flow_id", ["", "not-a-flow"])
def test_poll_unknown_flow_is_an_error_envelope(client, net, flow_id):
    res = client.post("/api/config/oauth/poll", json={"flow_id": flow_id})
    assert res.status_code == 200
    assert res.json() == {"status": "error", "error": "Sign-in session expired — start again."}
    assert net.calls == []


def test_poll_rejects_a_claude_flow_id(client, net):
    """A flow_id is bound to its provider — a Claude PKCE flow can't be polled as Codex."""
    fid = client.post("/api/config/oauth/start", json={"provider": "anthropic-oauth"}).json()["flow_id"]
    res = client.post("/api/config/oauth/poll", json={"flow_id": fid})
    assert res.json()["status"] == "error"
    assert net.calls == []


@pytest.mark.parametrize("pending_status", [403, 404])
def test_poll_pending_writes_nothing(client, net, pending_status):
    fid = _start_codex(client, net)
    net.on(_DEVICE_TOKEN_URL, pending_status, {})
    res = client.post("/api/config/oauth/poll", json={"flow_id": fid})
    assert res.json() == {"status": "pending"}
    assert net.calls[-1][1]["json"] == {"device_auth_id": "dev-auth-SECRET", "user_code": "ABCD-1234"}
    assert not _codex_store().exists()
    assert fid in login_mod._FLOWS  # still pollable
    assert _status(client, "openai-codex")["signed_in"] is False


def test_poll_success_stores_tokens_and_flips_status(client, net):
    from infra.paths import instance_paths

    fid = _start_codex(client, net)
    oauth_mod._mark_disconnected(instance_paths(), "openai-codex")
    net.on(_DEVICE_TOKEN_URL, body={"authorization_code": "authcode-SECRET", "code_verifier": "verifier-SECRET"})
    net.on(oauth_mod._CODEX_TOKEN_URL, body={"access_token": _LIVE_ACCESS, "refresh_token": "rt-SECRET", "id_token": "id-SECRET"})

    res = client.post("/api/config/oauth/poll", json={"flow_id": fid})
    assert res.status_code == 200
    assert res.json() == {"status": "complete"}
    _assert_no_secrets(res.text, _LIVE_ACCESS, "rt-SECRET", "id-SECRET", "authcode-SECRET", "verifier-SECRET")

    exchange = net.calls[-1][1]["data"]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "authcode-SECRET" and exchange["code_verifier"] == "verifier-SECRET"

    store = _codex_store()
    doc = json.loads(store.read_text())
    assert doc["tokens"] == {"access_token": _LIVE_ACCESS, "refresh_token": "rt-SECRET", "id_token": "id-SECRET"}
    assert doc["provenance"] == oauth_mod.PROVENANCE_DEVICE_LOGIN  # ours → revocable on disconnect
    _assert_owner_only(store)
    assert not oauth_mod.is_disconnected("openai-codex")
    assert _status(client, "openai-codex")["signed_in"] is True

    # The flow is single-use: a replayed poll can't redeem the approval twice.
    assert fid not in login_mod._FLOWS
    assert client.post("/api/config/oauth/poll", json={"flow_id": fid}).json()["status"] == "error"


@pytest.mark.parametrize(
    "device, exchange",
    [
        ((500, {}), None),
        ((200, {"authorization_code": "a"}), None),  # approved but no verifier
        ((200, {"authorization_code": "a", "code_verifier": "v"}), (400, {"error": "invalid_grant"})),
        ((200, {"authorization_code": "a", "code_verifier": "v"}), (200, {"refresh_token": "r"})),
    ],
    ids=["poll-500", "incomplete-approval", "exchange-rejected", "exchange-no-access"],
)
def test_poll_failures_are_error_envelopes_and_write_nothing(client, net, device, exchange):
    fid = _start_codex(client, net)
    net.on(_DEVICE_TOKEN_URL, *device)
    if exchange:
        net.on(oauth_mod._CODEX_TOKEN_URL, *exchange)
    res = client.post("/api/config/oauth/poll", json={"flow_id": fid})
    assert res.status_code == 200
    assert res.json()["status"] == "error" and res.json()["error"]
    assert not _codex_store().exists()


def test_poll_network_error_is_an_error_envelope(client, net):
    fid = _start_codex(client, net)
    net.on(_DEVICE_TOKEN_URL, exc=httpx.ConnectError("down"))
    res = client.post("/api/config/oauth/poll", json={"flow_id": fid})
    assert res.status_code == 200 and res.json()["status"] == "error"
    assert "Poll failed" in res.json()["error"]


# ── POST /api/config/oauth/complete (Claude PKCE) ─────────────────────────────


def _start_claude(client) -> tuple[str, dict]:
    fid = client.post("/api/config/oauth/start", json={"provider": "anthropic-oauth"}).json()["flow_id"]
    return fid, dict(login_mod._FLOWS[fid].data)


@pytest.mark.parametrize("flow_id", ["", "bogus"])
def test_complete_unknown_flow_is_an_error_envelope(client, net, flow_id):
    res = client.post("/api/config/oauth/complete", json={"flow_id": flow_id, "code": "c#s"})
    assert res.status_code == 200
    assert res.json() == {"status": "error", "error": "Sign-in session expired — start again."}
    assert net.calls == []


def test_complete_rejects_a_codex_flow_id(client, net):
    fid = _start_codex(client, net)
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": "c#s"})
    assert res.json()["status"] == "error"
    assert net.urls() == [_USERCODE_URL]  # no token exchange attempted


def test_complete_state_mismatch_is_rejected_before_any_exchange(client, net):
    """CSRF guard: a pasted code whose ``#state`` isn't this flow's never reaches the
    token endpoint and stores nothing."""
    fid, _ = _start_claude(client)
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": "authcode#attacker-state"})
    assert res.json() == {"status": "error", "error": "Sign-in state mismatch — start again."}
    assert net.calls == []
    assert not oauth_mod._anthropic_store_path().exists()


@pytest.mark.parametrize("code", ["", "   "])
def test_complete_empty_code_is_rejected(client, net, code):
    fid, _ = _start_claude(client)
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": code})
    assert res.json()["status"] == "error"
    assert "Paste the code" in res.json()["error"]
    assert net.calls == []


@pytest.mark.parametrize(
    "route",
    [(400, {"error": "invalid_grant"}, None), (200, {"refresh_token": "r"}, None), (0, None, httpx.ConnectError("x"))],
    ids=["expired-code", "no-access-token", "unreachable"],
)
def test_complete_exchange_failure_is_an_error_envelope(client, net, route):
    status, body, exc = route
    fid, data = _start_claude(client)
    net.on(oauth_mod._ANTHROPIC_TOKEN_URL, status, body, exc=exc)
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": f"authcode#{data['state']}"})
    assert res.status_code == 200
    assert res.json()["status"] == "error"
    _assert_no_secrets(res.text, data["code_verifier"])
    assert not oauth_mod._anthropic_store_path().exists()
    assert _status(client, "anthropic-oauth")["signed_in"] is False


def test_complete_success_stores_tokens_and_flips_status(client, net):
    from infra.paths import instance_paths

    fid, data = _start_claude(client)
    oauth_mod._mark_disconnected(instance_paths(), "anthropic-oauth")
    net.on(
        oauth_mod._ANTHROPIC_TOKEN_URL,
        body={"access_token": "sk-ant-oat-SECRET", "refresh_token": "sk-ant-ort-SECRET", "expires_in": 3600, "account": {"uuid": "acct-uuid"}},
    )
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": f"  authcode-XYZ#{data['state']}  "})
    assert res.status_code == 200
    assert res.json() == {"status": "complete"}
    _assert_no_secrets(res.text, "sk-ant-oat-SECRET", "sk-ant-ort-SECRET", data["code_verifier"])

    # The exchange proves the PKCE verifier + echoes this flow's state, with the bare code.
    sent = net.calls[-1][1]["json"]
    assert sent["code"] == "authcode-XYZ"
    assert sent["state"] == data["state"] and sent["code_verifier"] == data["code_verifier"]

    store = oauth_mod._anthropic_store_path()
    doc = json.loads(store.read_text())
    assert doc["access_token"] == "sk-ant-oat-SECRET" and doc["refresh_token"] == "sk-ant-ort-SECRET"
    assert doc["account_uuid"] == "acct-uuid"
    _assert_owner_only(store)
    assert not oauth_mod.is_disconnected("anthropic-oauth")
    status = _status(client, "anthropic-oauth")
    assert status["signed_in"] is True and status["source"] == "instance_store"

    # Single-use: the same code can't be replayed against the flow.
    assert fid not in login_mod._FLOWS
    replay = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": f"authcode-XYZ#{data['state']}"})
    assert replay.json()["status"] == "error"


def test_completed_signin_reload_failure_is_reported_not_raised(client, net, monkeypatch):
    """Tokens are stored even when the post-sign-in graph rebuild fails — the route
    reports the reload error alongside ``complete`` rather than 500ing."""
    monkeypatch.setattr("graph.config_io.is_setup_complete", lambda: True)
    monkeypatch.setattr(STATE, "graph_config", types.SimpleNamespace(model_provider="anthropic-oauth"))
    monkeypatch.setattr("server.agent_init._reload_langgraph_agent", lambda: (False, "reload exploded"))
    fid, data = _start_claude(client)
    net.on(oauth_mod._ANTHROPIC_TOKEN_URL, body={"access_token": "sk-ant-oat-SECRET", "refresh_token": "r"})
    res = client.post("/api/config/oauth/complete", json={"flow_id": fid, "code": f"c#{data['state']}"})
    assert res.status_code == 200
    assert res.json() == {"status": "complete", "graph_reloaded": False, "graph_reload_error": "reload exploded"}
    assert _status(client, "anthropic-oauth")["signed_in"] is True
