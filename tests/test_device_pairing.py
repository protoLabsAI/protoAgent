"""Device pairing + per-device token tests (ADR 0087).

Weighted toward the security properties rather than the happy path — the happy path is one
call, but "single-use", "expires", "hashes only", and "revocation actually revokes" are the
claims the ADR makes and the ones a refactor could silently break.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """A `security.devices` bound to a throwaway instance root."""
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-pairing")

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


def test_claim_issues_a_working_token(devices):
    code, _ = devices.start_pairing()
    result = devices.claim_pairing(code, "Josh's phone")
    assert result is not None
    device, token = result
    assert device["name"] == "Josh's phone"
    assert devices.verify_token(token) is not None


def test_a_code_is_single_use(devices):
    code, _ = devices.start_pairing()
    assert devices.claim_pairing(code, "first") is not None
    # The whole point: a photographed QR can't be replayed after the operator used it.
    assert devices.claim_pairing(code, "second") is None


def test_an_expired_code_is_refused(devices, monkeypatch):
    code, _ = devices.start_pairing()
    real_time = time.time
    monkeypatch.setattr(devices.time, "time", lambda: real_time() + devices.PAIRING_TTL_SECONDS + 1)
    assert devices.claim_pairing(code, "late") is None


def test_the_registry_never_stores_the_token(devices):
    code, _ = devices.start_pairing()
    _, token = devices.claim_pairing(code, "phone")
    raw = devices._registry_path().read_text("utf-8")
    # A leaked registry must not be replayable — hashes only (ADR 0087 D2).
    assert token not in raw
    assert json.loads(raw)[0]["token_sha256"] != token


def test_revoking_stops_the_token_immediately(devices):
    code, _ = devices.start_pairing()
    device, token = devices.claim_pairing(code, "lost phone")
    assert devices.verify_token(token) is not None
    assert devices.revoke_device(device["id"]) is True
    assert devices.verify_token(token) is None


def test_revoking_one_device_leaves_the_others(devices):
    """The entire reason per-device tokens exist instead of one shared bearer."""
    a_code, _ = devices.start_pairing()
    _, a_token = devices.claim_pairing(a_code, "phone")
    b_code, _ = devices.start_pairing()
    b_device, b_token = devices.claim_pairing(b_code, "tablet")

    devices.revoke_device(b_device["id"])
    assert devices.verify_token(b_token) is None
    assert devices.verify_token(a_token) is not None  # untouched


def test_repeated_bad_claims_drop_pending_codes(devices):
    """An unauthenticated endpoint must not allow indefinite probing (ADR 0087 D4)."""
    code, _ = devices.start_pairing()
    for _ in range(devices._MAX_FAILED_CLAIMS):
        assert devices.claim_pairing("wrong-code", "attacker") is None
    # The real code is collateral — deliberately. The operator re-opens the dialog.
    assert devices.claim_pairing(code, "legit") is None


def test_claim_with_no_pending_pairing_is_refused(devices):
    assert devices.claim_pairing("anything", "nobody") is None


def test_unknown_and_garbage_tokens_are_refused(devices):
    code, _ = devices.start_pairing()
    devices.claim_pairing(code, "phone")
    assert devices.verify_token("") is None
    assert devices.verify_token("not-a-real-token") is None


def test_a_corrupt_registry_does_not_break_auth(devices):
    """Auth must fail CLOSED for devices, not fall over — the shared bearer still works."""
    code, _ = devices.start_pairing()
    _, token = devices.claim_pairing(code, "phone")
    devices._registry_path().write_text("{ not json", "utf-8")
    assert devices.verify_token(token) is None
    assert devices.list_devices() == []


def test_pairing_codes_do_not_survive_a_restart(devices):
    """Pending pairings are memory-only by design (ADR 0087 D3)."""
    code, _ = devices.start_pairing()
    importlib.reload(devices)  # stand-in for a process restart
    assert devices.claim_pairing(code, "phone") is None


def test_candidate_hosts_never_offers_loopback():
    """A QR pointing at 127.0.0.1 encodes the PHONE's loopback and can never work."""
    from operator_api.pairing_routes import _candidate_hosts

    for host in _candidate_hosts():
        assert not host["host"].startswith("127.")
        assert host["kind"] in {"tailnet", "lan"}


@pytest.mark.parametrize(
    ("addr", "kind"),
    [
        ("100.119.239.8", "tailnet"),  # RFC 6598 — Tailscale's range
        ("100.64.0.1", "tailnet"),
        ("192.168.5.31", "lan"),
        ("10.1.2.3", "lan"),
    ],
)
def test_tailnet_and_lan_addresses_are_offered(monkeypatch, addr, kind):
    """Regression: `not ip.is_private` silently DROPPED every tailnet address.

    100.64.0.0/10 is neither `is_private` nor `is_global` in Python, so the naive filter
    rejected the single most useful pairing target — a tailnet address reaches the phone
    from any network, a LAN address only from the same Wi-Fi. Caught by driving a real
    server, not by the original test, which only asserted loopback was ABSENT.
    """
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: [addr])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    assert pr._candidate_hosts() == [{"host": addr, "kind": kind}]


@pytest.mark.parametrize("addr", ["127.0.0.1", "169.254.1.1", "8.8.8.8", "1.1.1.1"])
def test_unusable_and_public_addresses_are_rejected(monkeypatch, addr):
    """Loopback/link-local can't work; a PUBLIC address must never be advertised as a
    scan-me target — that is how an instance ends up exposed to the internet."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: [addr])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    assert pr._candidate_hosts() == []


def test_a_loopback_bind_offers_nothing(monkeypatch):
    """The bind filter (ADR 0087 D6): the host HAS a LAN address, but nothing is listening
    on it, so a QR aimed there would fail with no explanation."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["192.168.5.31", "100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    assert pr._candidate_hosts() == []


def test_a_specific_bind_offers_only_that_interface(monkeypatch):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["192.168.5.31", "100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["100.119.239.8"])
    assert pr._candidate_hosts() == [{"host": "100.119.239.8", "kind": "tailnet"}]


def test_tailnet_is_offered_before_lan(monkeypatch):
    """Tailnet works from anywhere the operator's devices are; LAN only on the same Wi-Fi."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["192.168.5.31", "100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    assert [h["kind"] for h in pr._candidate_hosts()] == ["tailnet", "lan"]


def test_claim_path_is_public_but_only_exactly():
    """The credential-minting route is allowlisted; its neighbours must NOT be."""
    from a2a_impl.auth import _is_public

    assert _is_public("/api/pairing/claim") is True
    # Prefix-matching a minting route would exempt anything sharing the string.
    assert _is_public("/api/pairing/claim-extra") is False
    assert _is_public("/api/pairing/start") is False
    assert _is_public("/api/devices") is False


# ── Loopback recovery (ADR 0087 D6) ─────────────────────────────────────────────────────
# The desktop app binds 127.0.0.1 by design, which made pairing unusable in exactly the
# place it was asked for. A loopback-bound instance must still report what it COULD bind to
# so the console can offer the fix instead of dead-ending on an error.


def test_a_loopback_bind_still_reports_what_it_could_use(monkeypatch):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["192.168.5.31", "100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    assert pr._candidate_hosts() == []  # nothing pairable RIGHT NOW…
    # …but the panel needs somewhere to point, tailnet first.
    assert pr._pairable_addresses() == [
        {"host": "100.119.239.8", "kind": "tailnet"},
        {"host": "192.168.5.31", "kind": "lan"},
    ]


def test_pairable_addresses_still_excludes_unusable_ones(monkeypatch):
    """The offer must not include anything a QR could never reach, or anything PUBLIC —
    'make me reachable' must not become 'expose me to the internet'."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["127.0.0.1", "169.254.1.1", "8.8.8.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    assert pr._pairable_addresses() == []


def test_pairable_ignores_the_bind_but_candidates_do_not(monkeypatch):
    """The two must not drift: candidates = pairable ∩ reachable."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["192.168.5.31", "100.119.239.8"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["192.168.5.31"])
    assert pr._pairable_addresses() == [
        {"host": "100.119.239.8", "kind": "tailnet"},
        {"host": "192.168.5.31", "kind": "lan"},
    ]
    assert pr._candidate_hosts() == [{"host": "192.168.5.31", "kind": "lan"}]
