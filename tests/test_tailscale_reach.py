"""#4336 S4 — Tailscale MagicDNS/HTTPS pairing addresses, `tailscale serve` control, binding
the picked interface (plus loopback), and the panel's restart facts.

The tailscale CLI is NEVER run: every test replaces ``infra.tailscale._run`` with a fake that
records argv, so nothing here can change the real machine's serve config.
"""

from __future__ import annotations

import errno
import json

import pytest

from infra import tailscale

# infra.tailscale branches on sys.platform (the macOS app-bundle CLI) and server/bind.py opens
# real sockets — run on the Windows lane too.
pytestmark = pytest.mark.platform_sensitive

STATUS = {
    "BackendState": "Running",
    "Self": {
        "DNSName": "Box.tail1234.ts.net.",
        "TailscaleIPs": ["100.119.239.8", "fd7a:115c:a1e0::1"],
        "CapMap": {"https": None},
    },
    "CurrentTailnet": {"MagicDNSSuffix": "tail1234.ts.net", "MagicDNSEnabled": True},
    "CertDomains": ["box.tail1234.ts.net"],
}

SERVED_443 = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7931"}}}},
}


class FakeCLI:
    """Stands in for the tailscale binary. ``serve`` is the current serve config (mutated by
    enable/disable like the real one would be)."""

    def __init__(self, status=STATUS, serve=None, fail_serve: tuple[int, str, str] | None = None):
        self.status = status
        self.serve = json.loads(json.dumps(serve or {}))
        self.fail_serve = fail_serve
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args == ["status", "--json"]:
            if self.status is None:
                return -1, "", "not found"
            return 0, json.dumps(self.status), ""
        if args == ["serve", "status", "--json"]:
            return 0, json.dumps(self.serve), ""
        if args[0] == "serve":
            if self.fail_serve:
                return self.fail_serve
            https = int(next(a for a in args if a.startswith("--https=")).split("=", 1)[1])
            if args[-1] == "off":
                self.serve.get("Web", {}).pop(f"box.tail1234.ts.net:{https}", None)
                self.serve.get("TCP", {}).pop(str(https), None)
            else:
                self.serve.setdefault("TCP", {})[str(https)] = {"HTTPS": True}
                self.serve.setdefault("Web", {})[f"box.tail1234.ts.net:{https}"] = {
                    "Handlers": {"/": {"Proxy": args[-1]}}
                }
            return 0, "", ""
        raise AssertionError(f"unexpected tailscale call {args}")

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] == "serve" and c[1] != "status"]


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCLI()
    monkeypatch.setattr(tailscale, "_run", fake)
    tailscale.clear_cache()
    yield fake
    tailscale.clear_cache()


# ── discovery ───────────────────────────────────────────────────────────────────────────────


def test_parse_status_reads_name_magicdns_https_and_ips():
    st = tailscale.parse_status(json.dumps(STATUS))
    assert st.available and st.running
    assert st.dns_name == "box.tail1234.ts.net"  # trailing dot dropped, lowercased
    assert st.magicdns and st.https
    assert st.ips[0] == "100.119.239.8"


def test_parse_status_without_https_or_running():
    raw = dict(STATUS, BackendState="Stopped", CertDomains=None, Self=dict(STATUS["Self"], CapMap={}))
    st = tailscale.parse_status(json.dumps(raw))
    assert st.available and not st.running and not st.https
    assert "Stopped" in st.error


def test_absent_cli_is_graceful():
    """The conftest sets PROTOAGENT_TAILSCALE_BIN="" — no binary at all."""
    tailscale.clear_cache()
    st = tailscale.status()
    assert not st.available and "isn't installed" in st.error
    assert tailscale.served_url(7870) is None


def test_a_hung_cli_is_bounded_and_graceful(monkeypatch):
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout: (-2, "", ""))
    tailscale.clear_cache()
    assert not tailscale.status().available


def test_status_is_cached(cli):
    tailscale.status()
    tailscale.status()
    assert cli.calls.count(["status", "--json"]) == 1


@pytest.mark.parametrize(
    "proxy, ours",
    [
        ("http://127.0.0.1:7931", True),
        ("http://localhost:7931/", True),
        ("127.0.0.1:7931", True),
        ("http://127.0.0.1:7870", False),  # another instance
        ("http://127.0.0.1:7931/app", False),  # a sub-path mount isn't the console at /
        ("https+insecure://localhost:7931", False),
        ("http://10.0.0.5:7931", False),
    ],
)
def test_parse_serve_recognises_only_our_root_handler(proxy, ours):
    raw = json.dumps({"Web": {"box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": proxy}}}}})
    state = tailscale.parse_serve(raw, 7931)
    assert (443 in state["ours"]) is ours
    assert 443 in state["taken"]


def test_served_url_reports_https_and_funnel(cli):
    cli.serve = dict(SERVED_443, AllowFunnel={"box.tail1234.ts.net:443": True})
    assert tailscale.served_url(7931) == {"url": "https://box.tail1234.ts.net", "https_port": 443, "funnel": True}


# ── enable / disable ────────────────────────────────────────────────────────────────────────


def test_enable_serve_uses_443_with_the_current_cli_syntax(cli):
    res = tailscale.enable_serve(7931)
    assert res.ok and res.url == "https://box.tail1234.ts.net"
    assert cli.mutations == [["serve", "--bg", "--https=443", "http://127.0.0.1:7931"]]


def test_enable_serve_never_overwrites_another_service(cli):
    cli.serve = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7870"}}}},
    }
    res = tailscale.enable_serve(7931)
    assert res.ok and res.url == "https://box.tail1234.ts.net:8443"
    assert cli.mutations == [["serve", "--bg", "--https=8443", "http://127.0.0.1:7931"]]
    # the other instance's handler is untouched
    assert cli.serve["Web"]["box.tail1234.ts.net:443"]["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:7870"


def test_enable_serve_is_idempotent(cli):
    cli.serve = SERVED_443
    assert tailscale.enable_serve(7931).url == "https://box.tail1234.ts.net"
    assert cli.mutations == []


def test_enable_serve_without_https_certs_links_the_admin_console_and_never_runs_serve(cli):
    cli.status = dict(STATUS, CertDomains=[], Self=dict(STATUS["Self"], CapMap={}))
    res = tailscale.enable_serve(7931)
    assert not res.ok and res.reason == "https_disabled"
    assert res.enable_url == tailscale.HTTPS_ADMIN_URL
    assert cli.mutations == []  # a newer CLI would BLOCK waiting for consent


def test_enable_serve_surfaces_the_consent_url_when_serve_isnt_enabled(cli):
    cli.fail_serve = (
        -2,
        "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n  https://login.tailscale.com/f/serve?node=abc\n",
        "",
    )
    res = tailscale.enable_serve(7931)
    assert res.reason == "https_disabled" and res.enable_url == "https://login.tailscale.com/f/serve?node=abc"


def test_enable_serve_permission_denied_says_how_to_fix(cli):
    cli.fail_serve = (1, "", "Access denied: serve config denied")
    res = tailscale.enable_serve(7931)
    assert res.reason == "permission" and "--operator=$USER" in res.error


def test_enable_serve_when_tailscale_is_absent_or_stopped(cli):
    cli.status = None
    assert tailscale.enable_serve(7931).reason == "no_cli"
    cli.status = dict(STATUS, BackendState="NeedsLogin")
    assert tailscale.enable_serve(7931).reason == "not_running"


def test_disable_serve_turns_off_only_our_port(cli):
    cli.serve = {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            "box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7870"}}},
            "box.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7931"}}},
        },
    }
    assert tailscale.disable_serve(7931).ok
    assert cli.mutations == [["serve", "--https=8443", "off"]]
    assert "reset" not in sum(cli.calls, [])


# ── the pairing routes ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(monkeypatch, cli):
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


def test_start_offers_https_then_magicdns_then_ips(app_client, cli, devices):
    cli.serve = SERVED_443
    body = app_client.post("/api/pairing/start").json()
    origins = [(h["via"], h["base_url"]) for h in body["hosts"]]
    assert origins == [
        ("serve", "https://box.tail1234.ts.net/app/"),
        ("magicdns", "http://box.tail1234.ts.net:7931/app/"),
        ("ip", "http://100.119.239.8:7931/app/"),
        ("ip", "http://192.168.5.31:7931/app/"),
    ]
    best = body["hosts"][0]
    assert best["secure"] is True and best["url"] == f"https://box.tail1234.ts.net/app/#pair={body['code']}"
    assert best["qr"].startswith("<svg")
    assert body["reach"]["tailscale"]["served"]["url"] == "https://box.tail1234.ts.net"


def test_a_loopback_bound_instance_pairs_over_tailscale_serve(app_client, monkeypatch, cli, devices):
    """The preferred path: stay on localhost, let tailscaled terminate HTTPS."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    cli.serve = SERVED_443
    r = app_client.post("/api/pairing/start")
    assert r.status_code == 200
    assert [h["via"] for h in r.json()["hosts"]] == ["serve"]  # no http name: the tailnet IP isn't bound


def test_loopback_bound_without_serve_still_409s_with_the_tailscale_facts(app_client, monkeypatch, devices):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    r = app_client.post("/api/pairing/start")
    assert r.status_code == 409
    ts = r.json()["reach"]["tailscale"]
    assert ts["running"] and ts["https"] and ts["served"] is None and ts["dns_name"] == "box.tail1234.ts.net"


def test_agent_codes_use_the_https_origin_too(app_client, cli, devices):
    cli.serve = SERVED_443
    hosts = app_client.post("/api/pairing/start", json={"kind": "agent"}).json()["hosts"]
    assert hosts[0]["url"] == "https://box.tail1234.ts.net"


def test_enable_serve_is_refused_without_a_token(app_client, monkeypatch, cli):
    """SECURITY: serve's requests come from 127.0.0.1; a tokenless instance would be open to
    the whole tailnet. Refused before the CLI is ever asked to change anything."""
    import a2a_impl.auth as auth

    monkeypatch.setattr(auth, "_BEARER", [None])
    r = app_client.post("/api/pairing/tailscale/serve")
    assert r.status_code == 409 and r.json()["reason"] == "no_token"
    assert cli.mutations == []


def test_enable_serve_with_a_token_serves_our_listening_port(app_client, cli):
    r = app_client.post("/api/pairing/tailscale/serve")
    assert r.status_code == 200 and r.json() == {"ok": True, "url": "https://box.tail1234.ts.net"}
    assert cli.mutations == [["serve", "--bg", "--https=443", "http://127.0.0.1:7931"]]


def test_enable_serve_needs_loopback_served(app_client, monkeypatch, cli):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", ["192.168.5.31"])
    r = app_client.post("/api/pairing/tailscale/serve")
    assert r.status_code == 409 and r.json()["reason"] == "no_loopback"
    monkeypatch.setattr(pr, "_EXTRA_LISTEN", ["127.0.0.1"])  # bind.py co-binds loopback
    assert app_client.post("/api/pairing/tailscale/serve").status_code == 200


def test_disable_serve_route(app_client, cli):
    cli.serve = SERVED_443
    assert app_client.delete("/api/pairing/tailscale/serve").json() == {"ok": True}
    assert cli.mutations == [["serve", "--https=443", "off"]]


def test_reach_route_reports_bind_token_tailscale_and_restart(app_client, monkeypatch):
    import operator_api.pairing_routes as pr
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "uvicorn_server", object(), raising=False)
    body = app_client.get("/api/pairing/reach").json()
    assert body["bind"] == "0.0.0.0" and body["port"] == 7931 and body["auth_configured"] is True
    assert body["tailscale"]["dns_name"] == "box.tail1234.ts.net"
    assert body["restart"]["supported"] is True

    monkeypatch.setattr(pr, "_BIND_PINNED", ["127.0.0.1"])
    hint = app_client.get("/api/pairing/reach").json()["restart"]
    assert "--host 127.0.0.1" in hint["hint"] and hint["pending"] is False


def test_restart_pending_when_saved_bind_differs(app_client, monkeypatch):
    from types import SimpleNamespace

    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(bind_host="100.119.239.8"), raising=False)
    assert app_client.get("/api/pairing/reach").json()["restart"]["pending"] is True


def test_a_loopback_fallback_is_not_a_pending_restart(app_client, monkeypatch):
    """The picked IP was gone at boot (bind.py fell back to loopback): restarting won't fix
    that, so the panel mustn't keep offering it."""
    from types import SimpleNamespace

    import operator_api.pairing_routes as pr
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(bind_host="100.119.239.8"), raising=False)
    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(pr, "_BIND_REQUESTED", ["100.119.239.8"])
    assert app_client.get("/api/pairing/reach").json()["restart"]["pending"] is False


def test_reachable_counts_the_co_bound_loopback(monkeypatch):
    import operator_api.pairing_routes as pr

    pr.set_bind_host("100.119.239.8", extra=["127.0.0.1"], pinned_by_flag=False)
    try:
        assert pr._reachable("100.119.239.8") and pr._loopback_served()
        assert not pr._reachable("192.168.5.31")
    finally:
        pr.set_bind_host("127.0.0.1")


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """A `security.devices` bound to a throwaway instance root (as test_device_pairing does)."""
    import importlib

    import infra.paths

    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-s4")
    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


# ── binding the picked interface (server/bind.py) ───────────────────────────────────────────


def test_loopback_wildcard_and_hostname_binds_are_left_to_uvicorn():
    from server.bind import plan_listen

    for host in ("127.0.0.1", "0.0.0.0", "::", "localhost", "box.local"):
        plan = plan_listen(host, 7931)
        assert plan.sockets == [] and plan.host == host and plan.warnings == []


def test_a_picked_ip_listens_there_and_on_loopback(monkeypatch):
    import server.bind as b

    opened = []

    class _S:
        def close(self):
            pass

    monkeypatch.setattr(b, "_listen_socket", lambda host, port, family: opened.append((host, port)) or _S())
    plan = b.plan_listen("100.119.239.8", 7931)
    assert opened == [("100.119.239.8", 7931), ("127.0.0.1", 7931)]
    assert plan.host == "100.119.239.8" and plan.extra == ["127.0.0.1"] and plan.loopback_served
    assert len(plan.sockets) == 2 and plan.warnings == []


def test_an_address_gone_from_this_machine_falls_back_to_loopback():
    """Real sockets: 192.0.2.0/24 is TEST-NET-1, never assigned to a local interface."""
    from server.bind import plan_listen

    plan = plan_listen("192.0.2.123", 0)
    assert plan.host == "127.0.0.1" and plan.sockets == []
    assert plan.warnings and "isn't on this machine" in plan.warnings[0]


def test_a_busy_port_is_left_for_uvicorn_to_report(monkeypatch):
    import server.bind as b

    def _busy(host, port, family):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(b, "_listen_socket", _busy)
    plan = b.plan_listen("100.119.239.8", 7931)
    assert plan.host == "100.119.239.8" and plan.sockets == []


def test_loopback_unavailable_serves_the_picked_ip_alone_with_a_warning(monkeypatch):
    import server.bind as b

    class _S:
        def close(self):
            pass

    def _sock(host, port, family):
        if host == "127.0.0.1":
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return _S()

    monkeypatch.setattr(b, "_listen_socket", _sock)
    plan = b.plan_listen("100.119.239.8", 7931)
    assert len(plan.sockets) == 1 and not plan.loopback_served
    assert "can't reach this instance" in plan.warnings[0]


def test_real_loopback_sockets_bind_and_close():
    """The socket helper itself, on an ephemeral loopback port."""
    import socket

    from server.bind import _listen_socket

    s = _listen_socket("127.0.0.1", 0, socket.AF_INET)
    try:
        assert s.getsockname()[0] == "127.0.0.1" and not s.get_inheritable()
    finally:
        s.close()


def test_main_hands_bind_sockets_to_uvicorn():
    """Source pin: _main plans the listen sockets and passes them to Server.run."""
    import inspect

    import server as server_pkg

    code = inspect.getsource(server_pkg._main)
    assert "plan_listen(args.host, args.port)" in code
    assert "uvicorn_server.run(sockets=listen.sockets or None)" in code
    assert "pinned_by_flag=host_from_flag, requested=requested_host" in code
