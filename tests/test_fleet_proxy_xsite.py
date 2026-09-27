"""Open-hub browser gates for REMOTE members on the fleet HTTP proxy (#3662).

On an open hub (no bearer, no X-API-Key — the desktop default) every caller is operator, so
``forward_to`` lends a paired remote's stored operator token to whatever reaches it. A foreign
page can't READ the answer (CORS), but it can SEND a simple request, and a DNS-rebinding page
is same-origin with its own attacker name. These pin the two gates that close both — and that
the desktop webview, the console, the Vite dev proxy, curl and local members are unaffected.
Every refusal must happen before any upstream is contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from a2a_impl import auth
from graph.fleet import proxy

_STORED = "remote-operator-device-token"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()
    monkeypatch.delenv("PROTOAGENT_TRUSTED_HOSTS", raising=False)
    monkeypatch.setattr(auth, "_ALLOWED_ORIGINS", [None])
    monkeypatch.setattr(proxy, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(proxy, "_own_names", lambda: {"joshs-mbp", "joshs-mbp.local"})
    yield
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()


@pytest.fixture
def open_hub(monkeypatch):
    monkeypatch.setattr(auth, "_BEARER", [None])
    monkeypatch.setattr(auth, "_API_KEY", [""])
    assert auth.open_mode()


@pytest.fixture
def gated_hub(monkeypatch):
    monkeypatch.setattr(auth, "_BEARER", ["hub-bearer"])
    monkeypatch.setattr(auth, "_API_KEY", [""])
    assert not auth.open_mode()


class _Req:
    def __init__(self, method="GET", headers=None, tier="operator"):
        self.method = method
        self.headers = Headers(headers={"host": "127.0.0.1:7870", **(headers or {})})
        self.query_params = {}
        self.state = SimpleNamespace(trust_tier=tier)

    async def body(self):
        return b""


class _Upstream:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aiter_raw(self):
        yield b"{}"

    async def aclose(self):
        pass


class _Client:
    def __init__(self):
        self.built = None

    def build_request(self, method, url, headers=None, content=None, params=None, timeout=None):
        self.built = {"url": url, "headers": headers}
        return object()

    async def send(self, req, stream=True):
        return _Upstream()


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(proxy, "_get_client", lambda: c)
    return c


def _remote(monkeypatch, token=_STORED):
    monkeypatch.setattr(proxy.supervisor, "_load_state", lambda: {})
    monkeypatch.setattr(
        proxy.supervisor,
        "remote_for_slug",
        lambda s: {"id": "r1", "name": "r", "url": "http://100.64.0.9:7870", "token": token},
    )


async def _send(slug, method="POST", headers=None, path="api/chat"):
    return await proxy.forward_to(slug, _Req(method, headers), path)


def _auth_sent(client):
    return [v for k, v in client.built["headers"].items() if k.lower() == "authorization"]


# ── Fetch Metadata / Origin ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, headers",
    [
        ("POST", {"sec-fetch-site": "cross-site"}),  # blind form / text/plain POST
        ("GET", {"sec-fetch-site": "cross-site"}),  # no-cors subresource, no mode
        ("GET", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}),
        ("GET", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "object"}),
        ("POST", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}),
        ("POST", {"origin": "https://evil.example"}),
        ("POST", {"origin": "https://evil.example", "sec-fetch-site": "same-origin"}),  # Origin decides
        ("POST", {"origin": "null"}),  # sandboxed / opaque page
        ("POST", {"origin": "tauri://evil"}),
        ("POST", {"origin": "http://127.0.0.1:7870.evil.example"}),
        ("POST", {"origin": "http://localhost:5173", "sec-fetch-site": "same-site"}),  # sibling port ≠ hub
        ("GET", {"sec-fetch-site": "cross-site", "referer": "https://evil.example/x"}),
    ],
)
async def test_open_hub_refuses_cross_site_to_remote_without_dialling(monkeypatch, open_hub, client, method, headers):
    _remote(monkeypatch)
    resp = await _send("r1", method, headers)
    assert resp.status_code == 403
    assert b"Forbidden" in resp.body
    assert client.built is None  # the remote was never contacted


@pytest.mark.parametrize(
    "method, headers",
    [
        ("POST", {}),  # curl / the D4 delegate path: no Fetch Metadata, no Origin
        ("POST", {"sec-fetch-site": "same-origin", "origin": "http://127.0.0.1:7870"}),  # the console
        ("POST", {"origin": "https://127.0.0.1:7870"}),  # scheme-agnostic (TLS front)
        ("POST", {"sec-fetch-site": "cross-site", "origin": "tauri://localhost"}),  # desktop (macOS/Linux)
        ("POST", {"sec-fetch-site": "cross-site", "origin": "http://tauri.localhost"}),  # desktop (Windows)
        ("GET", {"sec-fetch-site": "none", "sec-fetch-mode": "navigate"}),  # typed URL
        ("GET", {"sec-fetch-site": "same-site"}),  # sibling port, no Origin
        # desktop plugin-view iframe: a cross-site GET navigation
        ("GET", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}),
        # desktop chat <img> of the remote's media: no Origin, trusted Referer
        ("GET", {"sec-fetch-site": "cross-site", "sec-fetch-dest": "image", "referer": "tauri://localhost/"}),
    ],
)
async def test_open_hub_lets_its_own_console_and_non_browsers_through(monkeypatch, open_hub, client, method, headers):
    _remote(monkeypatch)
    resp = await _send("r1", method, headers)
    assert resp.status_code == 200
    assert _auth_sent(client) == [f"Bearer {_STORED}"]  # the operator's own console still gets the swap


async def test_vite_dev_proxy_is_same_origin(monkeypatch, open_hub, client):
    """Vite's string proxy keeps the browser's Host (no changeOrigin): Host and Origin are both
    the dev server's ``localhost:5173``."""
    _remote(monkeypatch)
    resp = await _send("r1", "POST", {"host": "localhost:5173", "origin": "http://localhost:5173"})
    assert resp.status_code == 200


async def test_allowed_origins_list_admits_its_origin(monkeypatch, open_hub, client):
    _remote(monkeypatch)
    monkeypatch.setattr(auth, "_ALLOWED_ORIGINS", [["https://console.example"]])
    assert (await _send("r1", "POST", {"origin": "https://Console.example"})).status_code == 200
    assert (await _send("r1", "POST", {"origin": "https://other.example"})).status_code == 403


async def test_tokenless_remote_is_gated_too(monkeypatch, open_hub, client):
    _remote(monkeypatch, token="")
    assert (await _send("r1", "POST", {"sec-fetch-site": "cross-site"})).status_code == 403
    assert client.built is None


async def test_refusal_log_carries_no_credential(monkeypatch, open_hub, client, caplog):
    import logging

    _remote(monkeypatch)
    with caplog.at_level(logging.INFO, logger="protoagent.server"):
        await _send("r1", "POST", {"origin": "https://evil.example", "authorization": "Bearer HUB-SECRET"})
    text = caplog.text
    assert "refusing proxied request to remote member 'r1'" in text
    assert "evil.example" in text
    assert "HUB-SECRET" not in text and _STORED not in text


# ── Host allowlist (DNS rebinding) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:7870", "localhost.evil.example", "127.0.0.1.evil.example", "joshs-mbp.evil"],
)
async def test_open_hub_refuses_a_rebound_host(monkeypatch, open_hub, client, host):
    """A rebound page is same-origin with its own attacker name, so Origin and Fetch Metadata
    both look clean — the Host is the tell."""
    _remote(monkeypatch)
    resp = await _send("r1", "POST", {"host": host, "origin": f"http://{host}", "sec-fetch-site": "same-origin"})
    assert resp.status_code == 403
    assert b"Host" in resp.body
    assert client.built is None


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:7870",
        "127.8.9.1:7870",
        "localhost:7870",
        "LOCALHOST.",
        "hub.localhost:7870",
        "[::1]:7870",
        "192.168.1.20:7870",  # an IP literal can't be rebound
        "100.101.189.45:7870",
        "hub.tail1234.ts.net",  # tailscale serve forwards its MagicDNS name
        "joshs-mbp.local:7870",
        "joshs-mbp",
    ],
)
async def test_open_hub_accepts_its_own_hosts(monkeypatch, open_hub, client, host):
    _remote(monkeypatch)
    assert (await _send("r1", "POST", {"host": host})).status_code == 200


async def test_trusted_hosts_env_bind_name_and_allowed_origins(monkeypatch, open_hub, client):
    _remote(monkeypatch)
    assert (await _send("r1", "POST", {"host": "agents.example.com"})).status_code == 403
    monkeypatch.setenv("PROTOAGENT_TRUSTED_HOSTS", " Agents.Example.com , other.example")
    assert (await _send("r1", "POST", {"host": "agents.example.com:443"})).status_code == 200
    monkeypatch.delenv("PROTOAGENT_TRUSTED_HOSTS")
    monkeypatch.setattr(auth, "_ALLOWED_ORIGINS", [["https://agents.example.com"]])
    assert (await _send("r1", "POST", {"host": "agents.example.com"})).status_code == 200
    monkeypatch.setattr(auth, "_ALLOWED_ORIGINS", [None])
    proxy.set_bind_host("Hub.Lan")
    assert (await _send("r1", "POST", {"host": "hub.lan:7870"})).status_code == 200


# ── Scope: open hub + remote only ──────────────────────────────────────────────

_HOSTILE = {"host": "evil.example", "origin": "https://evil.example", "sec-fetch-site": "cross-site"}


async def test_gated_hub_does_not_apply_the_gate(monkeypatch, gated_hub, client):
    """A token-gated hub's credential is a header no foreign or rebound page holds (nothing is a
    cookie), so the middleware already 401s them; the gate stays out of reverse-proxy setups."""
    _remote(monkeypatch)
    assert (await _send("r1", "POST", _HOSTILE)).status_code == 200


@pytest.mark.parametrize("slug", ["host", "alice"])
async def test_local_member_and_host_slug_are_unaffected(monkeypatch, open_hub, client, slug):
    monkeypatch.setattr(proxy, "_target_for_slug", lambda s: ("http://127.0.0.1:7001", {}))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-tok", raising=False)
    assert (await _send(slug, "POST", _HOSTILE)).status_code == 200
    assert client.built is not None


def test_open_mode_tracks_both_credentials(monkeypatch):
    monkeypatch.setattr(auth, "_BEARER", [None])
    monkeypatch.setattr(auth, "_API_KEY", ["k"])
    assert not auth.open_mode()
    monkeypatch.setattr(auth, "_API_KEY", [""])
    assert auth.open_mode()
