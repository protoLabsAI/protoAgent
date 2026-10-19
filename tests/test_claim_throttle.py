"""The pairing-claim throttle (ADR 0121 D3), which replaced the 5-miss lockout.

The properties that matter, each pinned here:

* an empty bucket is a ``429`` + ``Retry-After`` and the code is NOT evaluated (even the
  right one is refused, and ``_match`` never runs);
* the per-client bucket (5 misses / 5 min) refills, and a different client is unaffected
  until the per-class bucket (30 misses / min) empties;
* no number of misses cancels a pending code (the old lockout, inverted);
* successes never draw; a miss with nothing pending draws only per-client;
* ``X-Forwarded-For`` is honoured only from a trusted proxy;
* agent codes (ADR 0113) get the same throttle.

The clock is injected, so "after the window" is a step, not a sleep.
"""

from __future__ import annotations

import importlib

import pytest

from security import claim_throttle

# Imports security.devices, whose registry I/O branches on os.name (Windows share retries).
pytestmark = pytest.mark.platform_sensitive


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    c = _Clock()
    claim_throttle.reset(clock=c)
    return c


@pytest.fixture
def devices(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-claim-throttle")
    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


def _client_for(peer: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    return TestClient(app, base_url="http://testserver:7931", client=(peer, 50000))


def _claim(http, code: str, headers: dict | None = None):
    return http.post("/api/pairing/claim", json={"code": code, "name": "phone"}, headers=headers or {})


@pytest.fixture
def match_calls(devices, monkeypatch):
    """Every code comparison, so a test can prove a throttled claim never reached one."""
    calls: list[str] = []
    real = devices._match

    def spy(code):
        calls.append(code)
        return real(code)

    monkeypatch.setattr(devices, "_match", spy)
    return calls


# ── The route: 429, Retry-After, not evaluated ──────────────────────────────────────────


@pytest.mark.parametrize("kind", ["device", "agent"])
def test_five_misses_from_one_ip_then_429_and_the_right_code_is_not_evaluated(devices, match_calls, kind):
    code, _ = devices.start_pairing(kind)
    http = _client_for("203.0.113.7")
    for _ in range(5):
        assert _claim(http, "ZZZZZ-ZZZZZ").status_code == 403
    assert len(match_calls) == 5

    r = _claim(http, code)  # the CORRECT code, while the bucket is empty
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"  # one miss back every 300/5 s
    assert r.json()["ok"] is False and r.json()["retry_after"] == 60
    assert "try again in 60 s" in r.json()["error"]
    assert len(match_calls) == 5  # never compared — a throttled request is not a guess
    assert code.replace("-", "") in devices._PENDING  # and the code is still pending


def test_after_the_window_the_same_client_can_claim(devices, clock):
    code, _ = devices.start_pairing()
    http = _client_for("203.0.113.7")
    for _ in range(5):
        _claim(http, "ZZZZZ-ZZZZZ")
    assert _claim(http, code).status_code == 429

    clock.now += 59
    r = _claim(http, code)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    clock.now += 1  # one token back: one attempt — here, the right code
    assert _claim(http, code).status_code == 200


def test_the_bucket_refills_fully_after_five_minutes(devices, clock):
    devices.start_pairing()
    http = _client_for("203.0.113.7")
    for _ in range(5):
        _claim(http, "ZZZZZ-ZZZZZ")
    clock.now += 300
    for _ in range(5):
        assert _claim(http, "ZZZZZ-ZZZZZ").status_code == 403
    assert _claim(http, "ZZZZZ-ZZZZZ").status_code == 429


def test_a_different_ip_is_unaffected_until_the_class_bucket_empties(devices, clock):
    code, _ = devices.start_pairing()
    blocked = _client_for("203.0.113.7")
    for _ in range(5):
        _claim(blocked, "ZZZZZ-ZZZZZ")
    assert _claim(blocked, code).status_code == 429

    # 25 more misses from 5 more addresses: 30 in the minute, the `private` class budget.
    for i in range(5):
        other = _client_for(f"198.51.100.{i}")
        for _ in range(5):
            assert _claim(other, "ZZZZZ-ZZZZZ").status_code == 403

    fresh = _client_for("192.0.2.1")
    r = _claim(fresh, code)  # a clean client, but the class is out
    assert r.status_code == 429 and r.headers["Retry-After"] == "2"  # 60/30 s per miss
    clock.now += 2
    assert _claim(fresh, code).status_code == 200


def test_a_different_ip_claims_while_another_is_throttled(devices):
    code, _ = devices.start_pairing()
    for _ in range(5):
        _claim(_client_for("203.0.113.7"), "ZZZZZ-ZZZZZ")
    assert _claim(_client_for("192.0.2.1"), code).status_code == 200


# ── No request can cancel a pending code (the old lockout, inverted) ───────────────────


def test_pending_codes_survive_any_number_of_misses(devices, clock):
    phone, _ = devices.start_pairing()
    agent, _ = devices.start_pairing("agent")
    for minute in range(4):  # far more than the old 5-miss lockout, across buckets refilling
        for i in range(40):
            try:
                devices.claim_pairing("ZZZZZ-ZZZZZ", "attacker", client=f"10.{minute}.0.{i}")
            except claim_throttle.ClaimThrottled:
                pass
        clock.now += 61
    assert set(devices._PENDING) == {phone.replace("-", ""), agent.replace("-", "")}
    assert devices.claim_pairing(phone, "phone", client="192.0.2.1") is not None
    assert devices.claim_pairing(agent, "hub", client="192.0.2.1") is not None


# ── What draws ──────────────────────────────────────────────────────────────────────────


def test_a_success_does_not_draw(devices):
    gate = claim_throttle.throttle()
    for _ in range(8):  # more successes than either budget, from one client
        code, _ = devices.start_pairing()
        assert devices.claim_pairing(code, "phone", client="10.0.0.1") is not None
    assert len(gate.per_client) == 0 and len(gate.per_class) == 0
    devices.start_pairing()
    for _ in range(5):  # the full miss budget is still there
        assert devices.claim_pairing("ZZZZZ-ZZZZZ", "x", client="10.0.0.1") is None


def test_a_miss_with_nothing_pending_draws_only_per_client(devices, match_calls):
    gate = claim_throttle.throttle()
    for _ in range(5):
        assert devices.claim_pairing("ZZZZZ-ZZZZZ", "prober", client="10.0.0.1") is None
    assert match_calls == []  # rejected before any comparison
    assert len(gate.per_class) == 0  # the shared class budget is untouched
    with pytest.raises(claim_throttle.ClaimThrottled) as ei:
        devices.claim_pairing("ZZZZZ-ZZZZZ", "prober", client="10.0.0.1")
    assert ei.value.bucket == "client"  # …but the prober is throttled all the same


def test_a_drained_class_bucket_is_429_even_with_nothing_pending(devices):
    """Both buckets are checked before the pending set is looked at, so 403 vs 429 can't tell
    a prober whether any code is pending."""
    devices.start_pairing()
    for i in range(30):
        devices.claim_pairing("ZZZZZ-ZZZZZ", "x", client=f"10.0.0.{i}")
    devices.cancel_pairings()
    with pytest.raises(claim_throttle.ClaimThrottled) as ei:
        devices.claim_pairing("ZZZZZ-ZZZZZ", "x", client="10.9.9.9")
    assert ei.value.bucket == "ingress"


# ── Ingress class ───────────────────────────────────────────────────────────────────────


def test_everything_is_private_until_a_public_origin_is_set():
    assert claim_throttle.ingress_class("agent.example.com") == "private"
    assert claim_throttle.ingress_class(None) == "private"
    claim_throttle.set_public_origin("https://Agent.Example.com/")
    assert claim_throttle.ingress_class("agent.example.com") == "public"
    assert claim_throttle.ingress_class("192.168.1.5:7870") == "private"


def test_draining_the_public_class_leaves_private_claims_working(devices):
    """D3: a forged or flooded public Host drains only the public bucket."""
    claim_throttle.set_public_origin("https://agent.example.com")
    code, _ = devices.start_pairing()
    for i in range(30):
        devices.claim_pairing("ZZZZZ-ZZZZZ", "x", client=f"10.0.0.{i}", ingress="public")
    with pytest.raises(claim_throttle.ClaimThrottled):
        devices.claim_pairing(code, "x", client="10.9.9.9", ingress="public")
    assert devices.claim_pairing(code, "lan phone", client="192.168.1.20", ingress="private") is not None


# ── trusted_proxies decides which IP is keyed ──────────────────────────────────────────


def test_x_forwarded_for_is_ignored_from_an_untrusted_peer(devices):
    """A direct client can't dodge its bucket by inventing a new X-Forwarded-For each time."""
    devices.start_pairing()
    http = _client_for("203.0.113.7")
    for i in range(5):
        _claim(http, "ZZZZZ-ZZZZZ", {"X-Forwarded-For": f"10.0.0.{i}"})
    assert _claim(http, "ZZZZZ-ZZZZZ", {"X-Forwarded-For": "10.0.0.99"}).status_code == 429


def test_x_forwarded_for_is_honoured_from_a_trusted_proxy(devices):
    claim_throttle.set_trusted_proxies(["172.18.0.0/16"])
    code, _ = devices.start_pairing()
    proxy = _client_for("172.18.0.2")
    for _ in range(5):
        _claim(proxy, "ZZZZZ-ZZZZZ", {"X-Forwarded-For": "203.0.113.7"})
    assert _claim(proxy, code, {"X-Forwarded-For": "203.0.113.7"}).status_code == 429
    # Another client behind the same proxy has its own bucket.
    assert _claim(proxy, code, {"X-Forwarded-For": "198.51.100.4"}).status_code == 200


def test_without_trusted_proxies_a_remote_proxy_is_one_client(devices):
    """The failure trusted_proxies exists to fix: every client behind an untrusted proxy
    shares the proxy's bucket."""
    code, _ = devices.start_pairing()
    proxy = _client_for("172.18.0.2")
    for i in range(5):
        _claim(proxy, "ZZZZZ-ZZZZZ", {"X-Forwarded-For": f"203.0.113.{i}"})
    assert _claim(proxy, code, {"X-Forwarded-For": "198.51.100.4"}).status_code == 429


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        ("203.0.113.7", ["1.2.3.4"], [], "203.0.113.7"),  # untrusted peer: header ignored
        ("127.0.0.1", ["1.2.3.4"], [], "1.2.3.4"),  # loopback always trusted (uvicorn default)
        ("::1", ["1.2.3.4"], [], "1.2.3.4"),
        ("127.0.0.1", [], [], "127.0.0.1"),  # no header: the peer itself
        ("10.0.0.2", ["9.9.9.9, 5.5.5.5, 10.0.0.3"], ["10.0.0.0/8"], "5.5.5.5"),  # rightmost untrusted
        ("10.0.0.2", ["9.9.9.9", "5.5.5.5"], ["10.0.0.0/8"], "5.5.5.5"),  # repeated headers join
        ("10.0.0.2", ["10.0.0.9"], ["10.0.0.0/8"], "10.0.0.9"),  # all hops trusted: leftmost
        ("5.5.5.5", ["1.1.1.1"], ["*"], "1.1.1.1"),  # uvicorn's trust-everyone
        ("10.0.0.2", ["1.1.1.1"], ["not-an-ip"], "10.0.0.2"),  # junk entries are skipped
    ],
)
def test_resolve_client_ip(peer, xff, trusted, expected):
    assert claim_throttle.resolve_client_ip(peer, xff, trusted) == expected


def test_resolving_twice_is_a_no_op():
    """uvicorn already rewrote the client from the same list; the route resolving again must
    land on the same address."""
    trusted = ["10.0.0.0/8"]
    xff = ["9.9.9.9, 5.5.5.5, 10.0.0.3"]
    once = claim_throttle.resolve_client_ip("10.0.0.2", xff, trusted)
    assert claim_throttle.resolve_client_ip(once, xff, trusted) == once


def test_server_passes_the_same_list_to_uvicorn():
    from types import SimpleNamespace

    from server import _trusted_proxies

    got = _trusted_proxies(SimpleNamespace(network_trusted_proxies=["172.18.0.0/16", "127.0.0.1"]))
    assert got == ["127.0.0.1", "::1", "172.18.0.0/16"]
    assert claim_throttle.trusted_proxies() == got
    assert _trusted_proxies(None) == ["127.0.0.1", "::1"]


# ── Config ──────────────────────────────────────────────────────────────────────────────


def test_trusted_proxies_config_key(monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert LangGraphConfig.from_dict({}).network_trusted_proxies == []
    cfg = LangGraphConfig.from_dict({"network": {"trusted_proxies": ["172.18.0.0/16", " ", "10.0.0.5"]}})
    assert cfg.network_trusted_proxies == ["172.18.0.0/16", "10.0.0.5"]
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.1.0.0/16, 10.2.0.1")
    assert LangGraphConfig.from_dict({}).network_trusted_proxies == ["10.1.0.0/16", "10.2.0.1"]
    # The file wins over the env, as for network.bind.
    assert LangGraphConfig.from_dict({"network": {"trusted_proxies": []}}).network_trusted_proxies == []


def test_the_per_client_table_is_bounded(clock, monkeypatch):
    monkeypatch.setattr(claim_throttle, "_MAX_CLIENT_ENTRIES", 50)
    gate = claim_throttle.throttle()
    for i in range(200):
        gate.miss(f"10.0.{i // 256}.{i % 256}", "private", anything_pending=False)
    assert len(gate.per_client) <= 50
