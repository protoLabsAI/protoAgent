"""ADR 0121 S7.4 — ``network.public_console_url``: pair a phone without Tailscale through the
operator's own HTTPS origin.

Pinned here:

* validation at load (https, origin only, not loopback) with the key named;
* the open-mode boot refusal, which ``PROTOAGENT_ALLOW_OPEN=1`` does NOT override, and the
  same rule on a config reload;
* the origin folded into ``A2A_ALLOWED_ORIGINS`` (only when that allowlist is set) and the
  trusted Host names; ``CONSOLE_ORIGIN_REGEX`` unchanged;
* claims whose Host is the public host are classed ``public`` (and only drain that bucket);
* a public-Host claim must be https as resolved through ``trusted_proxies``;
* ``/api/pairing/start`` order: tailnet HTTPS → public → tailnet name/IP → LAN;
* discovered addresses are never public.

The tailscale CLI is never run (``FakeCLI`` from the S4 suite records argv instead).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from infra import tailscale
from security import claim_throttle, public_console
from tests.test_tailscale_reach import SERVED_443, FakeCLI

# Patches infra.tailscale (which branches on sys.platform) — run on the Windows lane too.
pytestmark = pytest.mark.platform_sensitive

PUBLIC = "https://agent.example.com"


# ── Validation ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("https://agent.example.com", "https://agent.example.com"),
        ("https://agent.example.com/", "https://agent.example.com"),
        ("  HTTPS://Agent.Example.COM/  ", "https://agent.example.com"),
        ("https://agent.example.com:443", "https://agent.example.com"),  # browsers omit :443
        ("https://agent.example.com:8443", "https://agent.example.com:8443"),
        ("https://203.0.113.9", "https://203.0.113.9"),
    ],
)
def test_good_forms_normalise(raw, expected):
    assert public_console.normalize_public_console_url(raw) == expected


@pytest.mark.parametrize(
    "raw, why",
    [
        ("http://agent.example.com", "https://"),
        ("agent.example.com", "https://"),
        ("ftp://agent.example.com", "https://"),
        ("https://agent.example.com/app", "origin only"),
        ("https://agent.example.com/app/", "origin only"),
        ("https://agent.example.com?x=1", "origin only"),
        ("https://agent.example.com/?x=1", "origin only"),
        ("https://agent.example.com#frag", "origin only"),
        ("https://agent.example.com?", "origin only"),
        ("https://localhost", "loopback"),
        ("https://LOCALHOST:7870", "loopback"),
        ("https://app.localhost", "loopback"),
        ("https://127.0.0.1", "loopback"),
        ("https://127.1.2.3:8443", "loopback"),
        ("https://[::1]", "loopback"),
        ("https://0.0.0.0", "loopback"),
        ("https://user:pw@agent.example.com", "user name or password"),
        ("https://", "no host"),
        ("https://agent.example.com:notaport", "not a valid URL"),
    ],
)
def test_bad_forms_are_refused_naming_the_key(raw, why):
    with pytest.raises(ValueError) as exc:
        public_console.normalize_public_console_url(raw)
    assert "network.public_console_url" in str(exc.value)
    assert why in str(exc.value)


def test_non_string_is_refused():
    with pytest.raises(ValueError, match="network.public_console_url"):
        public_console.normalize_public_console_url(["https://agent.example.com"])


def test_config_load_validates_and_normalises(monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.delenv("PROTOAGENT_PUBLIC_CONSOLE_URL", raising=False)
    assert LangGraphConfig.from_dict({}).public_console_url == ""
    cfg = LangGraphConfig.from_dict({"network": {"public_console_url": "https://Agent.Example.com/"}})
    assert cfg.public_console_url == PUBLIC
    with pytest.raises(ValueError, match="network.public_console_url must start with https://"):
        LangGraphConfig.from_dict({"network": {"public_console_url": "http://agent.example.com"}})


def test_env_fallback_and_its_validation(monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.setenv("PROTOAGENT_PUBLIC_CONSOLE_URL", "https://phone.example.com")
    assert LangGraphConfig.from_dict({}).public_console_url == "https://phone.example.com"
    # The file wins over the env (file > env > default).
    cfg = LangGraphConfig.from_dict({"network": {"public_console_url": PUBLIC}})
    assert cfg.public_console_url == PUBLIC
    monkeypatch.setenv("PROTOAGENT_PUBLIC_CONSOLE_URL", "https://localhost")
    with pytest.raises(ValueError, match="loopback"):
        LangGraphConfig.from_dict({})


def test_settings_and_config_writes_refuse_a_bad_url_before_disk():
    from graph.config_io import validate_config_dict
    from graph.settings_schema import validate_flat

    ok, err = validate_flat({"network.public_console_url": "http://agent.example.com"})
    assert not ok and "network.public_console_url" in err and "https://" in err
    assert validate_flat({"network.public_console_url": PUBLIC}) == (True, None)
    assert validate_flat({"network.public_console_url": ""}) == (True, None)
    ok, err = validate_config_dict({"network": {"public_console_url": "https://agent.example.com/app"}})
    assert not ok and "origin only" in err
    assert validate_config_dict({"network": {"public_console_url": PUBLIC}}) == (True, "")


def test_the_field_is_agent_scoped_credential_plane_and_described():
    from graph.settings_schema import FIELDS, is_credential_plane_key

    f = next(f for f in FIELDS if f.key == "network.public_console_url")
    assert f.attr == "public_console_url" and f.type == "string" and f.scope == "agent"
    assert f.credential_plane and is_credential_plane_key(f.key)
    assert "https" in f.description and "PROTOAGENT_PUBLIC_CONSOLE_URL" in f.description


# ── Boot refusal ────────────────────────────────────────────────────────────────────────


def test_open_mode_refusal_rule():
    assert public_console.open_mode_refusal("", open_mode=True) is None
    assert public_console.open_mode_refusal(PUBLIC, open_mode=False) is None
    msg = public_console.open_mode_refusal(PUBLIC, open_mode=True)
    assert msg and "network.public_console_url" in msg and "auth.token" in msg
    assert "PROTOAGENT_ALLOW_OPEN does not override" in msg


@pytest.fixture
def open_auth(monkeypatch):
    from a2a_impl import auth

    monkeypatch.setattr(auth, "_BEARER", [None])
    monkeypatch.setattr(auth, "_API_KEY", [""])
    return auth


@pytest.mark.parametrize("allow_open", [None, "1"])
def test_boot_refuses_open_mode_with_a_public_url_even_with_allow_open(open_auth, monkeypatch, allow_open):
    import server

    if allow_open:
        monkeypatch.setenv("PROTOAGENT_ALLOW_OPEN", allow_open)
    else:
        monkeypatch.delenv("PROTOAGENT_ALLOW_OPEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        server._public_console_gate(SimpleNamespace(public_console_url=PUBLIC))
    assert exc.value.code == 2
    assert public_console.public_origin() == ""  # nothing was published


def test_boot_with_a_token_publishes_the_origin(open_auth, monkeypatch):
    import server

    monkeypatch.setattr(open_auth, "_BEARER", ["op-secret"])
    server._public_console_gate(SimpleNamespace(public_console_url=PUBLIC))
    assert public_console.public_origin() == PUBLIC
    assert claim_throttle.ingress_class("agent.example.com") == "public"


def test_boot_with_x_api_key_only_is_not_open(open_auth, monkeypatch):
    import server

    monkeypatch.setattr(open_auth, "_API_KEY", ["legacy-key"])
    server._public_console_gate(SimpleNamespace(public_console_url=PUBLIC))
    assert public_console.public_origin() == PUBLIC


def test_boot_open_without_the_key_is_untouched(open_auth):
    import server

    server._public_console_gate(SimpleNamespace(public_console_url=""))
    assert public_console.public_origin() == ""


def test_reload_refuses_a_config_that_would_be_open_and_public(open_auth, monkeypatch):
    from server.agent_init import _public_console_reload_refusal

    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    cfg = SimpleNamespace(public_console_url=PUBLIC, auth_token="")
    assert "network.public_console_url" in _public_console_reload_refusal(cfg)
    cfg = SimpleNamespace(public_console_url=PUBLIC, auth_token=None)  # falls back to env: unset
    assert _public_console_reload_refusal(cfg)
    monkeypatch.setenv("A2A_AUTH_TOKEN", "env-token")
    assert _public_console_reload_refusal(cfg) is None
    assert _public_console_reload_refusal(SimpleNamespace(public_console_url=PUBLIC, auth_token="t")) is None
    assert _public_console_reload_refusal(SimpleNamespace(public_console_url="", auth_token="")) is None


# ── Allowlists ──────────────────────────────────────────────────────────────────────────


def test_origin_folds_into_a_set_allowlist():
    from a2a_impl import auth

    auth.configure(bearer_token="op", api_key="", allowed_origins_raw="https://other.example.com")
    try:
        assert auth.allowed_origins() == ["https://other.example.com"]
        public_console.set_public_console_url(PUBLIC)
        assert auth.allowed_origins() == ["https://other.example.com", PUBLIC]
        public_console.set_public_console_url("")
        assert auth.allowed_origins() == ["https://other.example.com"]
    finally:
        auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")


def test_no_allowlist_stays_no_allowlist():
    from a2a_impl import auth

    auth.configure(bearer_token="op", api_key="", allowed_origins_raw="")
    try:
        public_console.set_public_console_url(PUBLIC)
        assert auth.allowed_origins() is None  # verification stays off, as configured
    finally:
        auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")


def test_the_middleware_admits_the_public_origin_when_an_allowlist_is_set():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth

    app = FastAPI()

    @app.get("/api/ping")
    async def _ping():  # noqa: ANN202
        return {"ok": True}

    auth.install(app, bearer_token="op", api_key="", allowed_origins_raw="https://other.example.com")
    try:
        c = TestClient(app, base_url=PUBLIC)
        hdr = {"Authorization": "Bearer op", "Origin": PUBLIC}
        assert c.get("/api/ping", headers=hdr).status_code == 403  # not declared yet
        public_console.set_public_console_url(PUBLIC)
        assert c.get("/api/ping", headers=hdr).status_code == 200
        assert c.get("/api/ping", headers={**hdr, "Origin": "https://evil.example.com"}).status_code == 403
    finally:
        auth.configure(bearer_token=None, api_key="", allowed_origins_raw="")


def test_public_host_is_a_trusted_host_name_and_the_cors_regex_is_unchanged(monkeypatch):
    from a2a_impl import hosts

    monkeypatch.delenv("PROTOAGENT_TRUSTED_HOSTS", raising=False)
    assert not hosts.host_allowed("agent.example.com")
    public_console.set_public_console_url("https://Agent.Example.com:8443")
    assert "agent.example.com" in hosts._trusted_host_names()
    assert hosts.host_allowed("agent.example.com:8443")
    assert hosts.CONSOLE_ORIGIN_REGEX == (
        r"^(tauri://localhost|http://tauri\.localhost|https?://(localhost|127\.0\.0\.1)(:\d+)?)$"
    )
    assert not hosts.CONSOLE_ORIGIN_RE.match("https://agent.example.com:8443")


# ── The claim route: ingress class and HTTPS on the public path ─────────────────────────


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-s7-4")
    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


@pytest.fixture
def match_calls(devices, monkeypatch):
    calls: list[str] = []
    real = devices._match

    def spy(code):
        calls.append(code)
        return real(code)

    monkeypatch.setattr(devices, "_match", spy)
    return calls


def _claimer(peer: str, base_url: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _claim(http, code, headers=None):
    return http.post("/api/pairing/claim", json={"code": code, "name": "phone"}, headers=headers or {})


def test_ingress_class_follows_the_declared_host():
    public_console.set_public_console_url(PUBLIC)
    assert claim_throttle.ingress_class("agent.example.com") == "public"
    assert claim_throttle.ingress_class("Agent.Example.Com") == "public"
    assert claim_throttle.ingress_class("192.168.1.20:7870") == "private"
    assert claim_throttle.ingress_class("box.tail1234.ts.net") == "private"
    assert claim_throttle.ingress_class("agent.example.com.") == "public"
    assert claim_throttle.ingress_class("agent.example.com.evil.net") == "private"
    # A non-default port matches by name too: nginx's `$host` drops the port.
    public_console.set_public_console_url("https://agent.example.com:8443")
    assert claim_throttle.ingress_class("agent.example.com:8443") == "public"
    assert claim_throttle.ingress_class("agent.example.com") == "public"
    public_console.set_public_console_url("")
    assert claim_throttle.ingress_class("agent.example.com") == "private"


def test_a_spoofed_public_host_drains_only_the_public_bucket(devices):
    """A LAN host forging the public Host lands in the public bucket; private pairing keeps
    working (D3)."""
    public_console.set_public_console_url(PUBLIC)
    code, _ = devices.start_pairing()
    for i in range(30):  # 30 different LAN peers, each forging the public Host over https
        _claim(_claimer(f"192.168.1.{i + 10}", PUBLIC), "ZZZZZ-ZZZZZ")
    r = _claim(_claimer("192.168.1.99", PUBLIC), code)
    assert r.status_code == 429  # the public class is empty
    # The private class is untouched: the right code still pairs over the LAN.
    assert _claim(_claimer("192.168.1.100", "http://192.168.1.5:7870"), code).status_code == 200


def test_https_public_claim_from_a_trusted_proxy_pairs(devices):
    """The Docker-network case done right: the proxy is listed, its X-Forwarded-Proto counts."""
    public_console.set_public_console_url(PUBLIC)
    claim_throttle.set_trusted_proxies(["172.18.0.0/16"])
    code, _ = devices.start_pairing()
    proxy = _claimer("172.18.0.2", "http://agent.example.com")  # plain http on the inner hop
    r = _claim(proxy, code, {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_https_public_claim_through_a_same_machine_proxy_pairs(devices):
    """cloudflared on the same host: loopback is always trusted."""
    public_console.set_public_console_url(PUBLIC)
    code, _ = devices.start_pairing()
    r = _claim(_claimer("127.0.0.1", "http://agent.example.com"), code, {"X-Forwarded-Proto": "https"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "peer, headers",
    [
        # The proxy's address isn't in trusted_proxies, so its X-Forwarded-Proto is ignored.
        ("172.18.0.2", {"X-Forwarded-Proto": "https"}),
        # A trusted proxy that says the edge was plain http.
        ("127.0.0.1", {"X-Forwarded-Proto": "http"}),
        # Nothing forwarded at all: a direct plain-http request with the public Host.
        ("203.0.113.7", {}),
    ],
)
def test_a_public_claim_that_was_not_https_is_refused_naming_trusted_proxies(devices, match_calls, peer, headers):
    public_console.set_public_console_url(PUBLIC)
    code, _ = devices.start_pairing()
    r = _claim(_claimer(peer, "http://agent.example.com"), code, headers)
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False and body["reason"] == "public_not_https"
    assert "network.trusted_proxies" in body["error"]
    assert match_calls == []  # refused before any code was compared
    assert devices.claim_pairing(code, "x", client="192.168.1.5", ingress="private") is not None  # still pending


def test_a_refused_http_claim_draws_nothing(devices):
    public_console.set_public_console_url(PUBLIC)
    devices.start_pairing()
    http = _claimer("203.0.113.7", "http://agent.example.com")
    for _ in range(10):
        assert _claim(http, "ZZZZZ-ZZZZZ").status_code == 400
    # The same client over https still has its whole budget.
    https = _claimer("203.0.113.7", PUBLIC)
    for _ in range(5):
        assert _claim(https, "ZZZZZ-ZZZZZ").status_code == 403
    assert _claim(https, "ZZZZZ-ZZZZZ").status_code == 429


def test_private_claims_need_no_https(devices):
    public_console.set_public_console_url(PUBLIC)
    code, _ = devices.start_pairing()
    assert _claim(_claimer("192.168.1.20", "http://192.168.1.5:7870"), code).status_code == 200


@pytest.mark.parametrize(
    "peer, scheme, xfp, trusted, expected",
    [
        ("203.0.113.7", "http", ["https"], [], "http"),  # untrusted peer: header ignored
        ("127.0.0.1", "http", ["https"], [], "https"),  # loopback is always trusted
        ("172.18.0.2", "http", ["https"], ["172.18.0.0/16"], "https"),
        ("172.18.0.2", "http", ["http", "https"], ["172.18.0.0/16"], "https"),  # last wins, as uvicorn
        ("172.18.0.2", "http", ["https, http"], ["172.18.0.0/16"], "http"),  # a list isn't a scheme
        ("172.18.0.2", "https", [], ["172.18.0.0/16"], "https"),  # already rewritten by uvicorn
        ("10.0.0.1", "http", ["https"], ["*"], "https"),
    ],
)
def test_resolve_scheme(peer, scheme, xfp, trusted, expected):
    assert claim_throttle.resolve_scheme(peer, scheme, xfp, trusted) == expected


# ── /api/pairing/start ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCLI()
    monkeypatch.setattr(tailscale, "_run", fake)
    tailscale.clear_cache()
    yield fake
    tailscale.clear_cache()


@pytest.fixture
def start_client(monkeypatch, cli):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import a2a_impl.auth as auth
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["100.119.239.8", "192.168.5.31"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    monkeypatch.setattr(pr, "_EXTRA_LISTEN", [])
    monkeypatch.setattr(pr, "_BIND_PINNED", [""])
    monkeypatch.setattr(pr, "_BIND_REQUESTED", [""])
    monkeypatch.setattr(auth, "_BEARER", ["operator-token"])
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    return TestClient(app, base_url="http://testserver:7931")


def test_start_without_the_key_offers_nothing_public(start_client, cli, devices):
    cli.serve = SERVED_443
    body = start_client.post("/api/pairing/start").json()
    assert [h["via"] for h in body["hosts"]] == ["serve", "magicdns", "ip", "ip"]
    assert all(h["kind"] != "public" for h in body["hosts"])
    assert body["reach"]["public_console_url"] == ""


def test_start_orders_tailnet_https_then_public_then_tailnet_then_lan(start_client, cli, devices):
    cli.serve = SERVED_443
    public_console.set_public_console_url(PUBLIC)
    body = start_client.post("/api/pairing/start").json()
    assert [(h["kind"], h["via"], h["base_url"]) for h in body["hosts"]] == [
        ("tailnet", "serve", "https://box.tail1234.ts.net/app/"),
        ("public", "public", f"{PUBLIC}/app/"),
        ("tailnet", "magicdns", "http://box.tail1234.ts.net:7931/app/"),
        ("tailnet", "ip", "http://100.119.239.8:7931/app/"),
        ("lan", "ip", "http://192.168.5.31:7931/app/"),
    ]
    public = body["hosts"][1]
    assert public["secure"] is True and public["host"] == "agent.example.com"
    assert public["url"] == f"{PUBLIC}/app/#pair={body['code']}"
    assert public["qr"].startswith("<svg")
    assert body["reach"]["public_console_url"] == PUBLIC


def test_without_tailscale_the_public_url_leads(start_client, monkeypatch, devices):
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout: (-1, "", "not found"))
    tailscale.clear_cache()
    public_console.set_public_console_url(PUBLIC)
    hosts = start_client.post("/api/pairing/start").json()["hosts"]
    assert [h["kind"] for h in hosts] == ["public", "tailnet", "lan"]


def test_a_loopback_bound_instance_pairs_through_the_public_url(start_client, monkeypatch, devices):
    """cloudflared on this machine forwards to 127.0.0.1; no bind change is needed."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout: (-1, "", "not found"))
    tailscale.clear_cache()
    assert start_client.post("/api/pairing/start").status_code == 409  # no key: today's answer
    public_console.set_public_console_url(PUBLIC)
    r = start_client.post("/api/pairing/start")
    assert r.status_code == 200 and [h["kind"] for h in r.json()["hosts"]] == ["public"]


def test_agent_codes_offer_the_public_url_as_a_base_url(start_client, cli, devices):
    public_console.set_public_console_url(PUBLIC)
    hosts = start_client.post("/api/pairing/start", json={"kind": "agent"}).json()["hosts"]
    public = next(h for h in hosts if h["kind"] == "public")
    assert public["url"] == PUBLIC and "qr" not in public


def test_reach_and_devices_report_the_public_url(start_client, devices):
    assert start_client.get("/api/pairing/reach").json()["public_console_url"] == ""
    public_console.set_public_console_url(PUBLIC)
    assert start_client.get("/api/pairing/reach").json()["public_console_url"] == PUBLIC
    assert start_client.get("/api/devices").json()["public_console_url"] == PUBLIC


def test_discovered_addresses_are_never_public(monkeypatch):
    """Even with the key set, discovery's allowlist (tailnet + RFC 1918) is unchanged: a
    routable interface address is dropped, and the declared URL is never 'discovered'."""
    import operator_api.pairing_routes as pr

    public_console.set_public_console_url(PUBLIC)
    monkeypatch.setattr(pr, "_local_addresses", lambda: ["93.184.216.34", "8.8.8.8", "100.64.0.5", "192.168.1.5"])
    found = pr._pairable_addresses()
    assert [(h["host"], h["kind"]) for h in found] == [("100.64.0.5", "tailnet"), ("192.168.1.5", "lan")]
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    assert all(h["kind"] in ("tailnet", "lan") for h in pr._candidate_hosts())


def test_set_public_console_url_ignores_an_invalid_value():
    public_console.set_public_console_url("http://agent.example.com")
    assert public_console.public_origin() == "" and public_console.public_host() == ""
    assert claim_throttle.ingress_class("agent.example.com") == "private"
