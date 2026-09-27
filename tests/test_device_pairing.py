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


# ── Agent codes (ADR 0113 D2/D3) ────────────────────────────────────────────────────────
# A second code kind for pairing a hub with a remote protoAgent. The properties that matter:
# typeable (short, forgiving), still single-use and time-boxed, under the SAME lockout as
# phone codes, and the device's kind comes from the code — never from the claimer.

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_device_codes_are_unchanged(devices):
    """Phone pairing must not move: 32 url-safe chars, 120s, kind 'device'."""
    before = time.time()
    code, expires_at = devices.start_pairing()
    assert len(code) == 32
    assert devices.PAIRING_TTL_SECONDS == 120
    assert before + 120 - 1 <= expires_at <= time.time() + 120
    device, _ = devices.claim_pairing(code, "phone")
    assert device["kind"] == "device"


def test_device_code_is_exact_match_only(devices):
    """Normalization is for agent codes; a device code is case-sensitive url-safe text and
    folding it could merge distinct codes."""
    code, _ = devices.start_pairing()
    if code.swapcase() != code:  # 32 random url-safe chars: effectively always has a letter
        assert devices.claim_pairing(code.swapcase(), "phone") is None
    assert devices.claim_pairing(code, "phone") is not None


def test_agent_code_format_and_ttl(devices):
    before = time.time()
    code, expires_at = devices.start_pairing("agent")
    assert len(code) == 11 and code[5] == "-"
    raw = code.replace("-", "")
    assert len(raw) == 10 and set(raw) <= _CROCKFORD
    assert devices.AGENT_PAIRING_TTL_SECONDS == 300
    assert before + 300 - 1 <= expires_at <= time.time() + 300
    # Stored normalized (no dash), so a claim compares like with like.
    assert raw in devices._PENDING and devices._PENDING[raw][1] == "agent"


def test_unknown_kind_is_refused(devices):
    with pytest.raises(ValueError):
        devices.start_pairing("admin")


def test_agent_code_expires(devices, monkeypatch):
    code, _ = devices.start_pairing("agent")
    real_time = time.time
    # Still good past the phone window…
    monkeypatch.setattr(devices.time, "time", lambda: real_time() + devices.PAIRING_TTL_SECONDS + 1)
    devices._prune(devices.time.time())
    assert devices.normalize_agent_code(code) in devices._PENDING
    # …gone past its own.
    monkeypatch.setattr(devices.time, "time", lambda: real_time() + devices.AGENT_PAIRING_TTL_SECONDS + 1)
    assert devices.claim_pairing(code, "late hub") is None


def test_agent_claim_yields_an_agent_device(devices):
    code, _ = devices.start_pairing("agent")
    device, token = devices.claim_pairing(code, "hub on the mac mini")
    assert device["kind"] == "agent"
    assert devices.verify_token(token).kind == "agent"
    assert devices.list_devices()[0]["kind"] == "agent"


def _mangle(code: str, how: str) -> str:
    raw = code.replace("-", "")
    if how == "lower":
        return code.lower()
    if how == "nodash":
        return raw
    if how == "lower-nodash":
        return raw.lower()
    if how == "spaces":
        return f" {raw[:5]} {raw[5:]} "
    if how == "underscore":
        return f"{raw[:5]}_{raw[5:]}"
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["lower", "nodash", "lower-nodash", "spaces", "underscore"])
def test_agent_codes_are_forgiving_to_type(devices, how):
    code, _ = devices.start_pairing("agent")
    assert devices.claim_pairing(_mangle(code, how), "hub") is not None


def test_look_alikes_fold_to_digits(devices, monkeypatch):
    """O→0 and I/L→1: a code shown as 0…1 still claims when typed with the letters."""
    monkeypatch.setattr(devices, "_new_agent_code", lambda: "01ABCDE011")
    code, _ = devices.start_pairing("agent")
    assert code == "01ABC-DE011"
    assert devices.claim_pairing("oi-abcde-OlI", "hub") is not None


def test_agent_code_is_single_use(devices):
    code, _ = devices.start_pairing("agent")
    assert devices.claim_pairing(code.lower(), "first") is not None
    assert devices.claim_pairing(code, "second") is None


def test_racing_claims_mint_exactly_one_device(devices, monkeypatch):
    """Consume-before-mint under the lock: N threads on one code → one token, not N."""
    import threading

    code, _ = devices.start_pairing("agent")
    real_match = devices._match

    def slow_match(candidate):
        found = real_match(candidate)
        # Widen the window BETWEEN match and pop: without `_LOCK` every thread matches the
        # still-pending code here before any of them consumes it.
        time.sleep(0.05)
        return found

    monkeypatch.setattr(devices, "_match", slow_match)
    results: list = []
    errors: list = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        try:
            results.append(devices.claim_pairing(code, "racer"))
        except Exception as exc:  # noqa: BLE001 — a loser must get a clean None, not a 500
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Without the lock every thread matches, then all but one die on the pop (a KeyError the
    # route would surface as a 500) — so "every loser returned None" is what pins the lock.
    assert errors == []
    assert len(results) == 8
    assert sum(r is not None for r in results) == 1
    assert len(devices.list_devices()) == 1


def test_agent_code_never_matches_a_device_code_by_folding(devices):
    """Folding is scoped to agent codes: a device code's uppercase/dashless variant must not
    redeem it."""
    code, _ = devices.start_pairing()
    folded = devices.normalize_agent_code(code)
    if folded != code:
        assert devices.claim_pairing(folded, "x") is None


def test_lockout_is_shared_across_kinds(devices):
    """Misses aimed at either kind count toward ONE counter, and the lockout drops BOTH kinds —
    otherwise each kind would be a separate 5-guess budget."""
    phone, _ = devices.start_pairing()
    agent, _ = devices.start_pairing("agent")
    for i in range(devices._MAX_FAILED_CLAIMS):
        guess = "ZZZZZ-ZZZZZ" if i % 2 else "not-a-device-code"
        assert devices.claim_pairing(guess, "attacker") is None
    assert devices.claim_pairing(phone, "legit phone") is None
    assert devices.claim_pairing(agent, "legit hub") is None


def test_a_success_resets_the_shared_counter(devices):
    for _ in range(devices._MAX_FAILED_CLAIMS - 1):
        devices.claim_pairing("nope", "attacker")
    code, _ = devices.start_pairing("agent")
    assert devices.claim_pairing(code, "hub") is not None
    later, _ = devices.start_pairing()
    devices.claim_pairing("nope", "attacker")  # one miss after a reset must not trip it
    assert devices.claim_pairing(later, "phone") is not None


@pytest.mark.parametrize("bad_kind", [["agent"], {"k": "agent"}, 7, None])
def test_a_malformed_kind_keeps_the_device(devices, bad_kind):
    """An unhashable hand-edited kind must not skip the entry — the next save would then
    delete the device for good."""
    path = devices._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": "c3", "name": "edited", "token_sha256": devices._hash("t"), "created_at": 1.0, "kind": bad_kind}
    path.write_text(json.dumps(["not-an-object", entry]), "utf-8")  # a stray non-dict too
    assert [(d["id"], d["kind"]) for d in devices.list_devices()] == [("c3", "device")]


def test_a_new_code_gets_a_fresh_miss_budget(devices):
    """Four stale misses + one honest typo of a NEW code must not lock the operator out."""
    devices.start_pairing("agent")  # an old code — misses only count while one is pending
    for _ in range(devices._MAX_FAILED_CLAIMS - 1):
        assert devices.claim_pairing("stale-guess", "x") is None
    code, _ = devices.start_pairing("agent")
    assert devices.claim_pairing("ZZZZZ-ZZZZ0", "typo") is None  # the typo
    assert devices.claim_pairing(code, "hub") is not None


def test_legacy_registry_without_kind_loads_as_device(devices):
    path = devices._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = "legacy-token"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "name": "old phone",
                    "token_sha256": devices._hash(token),
                    "created_at": 1.0,
                    "last_seen_at": None,
                },
                {"id": "b2", "name": "weird", "token_sha256": "x", "created_at": 2.0, "kind": "root"},
            ]
        ),
        "utf-8",
    )
    listed = {d["id"]: d for d in devices.list_devices()}
    assert listed["a1"]["kind"] == "device"
    assert listed["b2"]["kind"] == "device"  # an unknown kind is not trusted either
    assert devices.verify_token(token).kind == "device"


def test_cancel_is_scoped_by_kind(devices):
    phone, _ = devices.start_pairing()
    agent, _ = devices.start_pairing("agent")
    devices.cancel_pairings("device")  # the phone dialog closed
    assert devices.claim_pairing(phone, "phone") is None
    assert devices.claim_pairing(agent, "hub") is not None

    phone, _ = devices.start_pairing()
    agent, _ = devices.start_pairing("agent")
    devices.cancel_pairings("agent")  # the agent dialog closed
    assert devices.claim_pairing(agent, "hub") is None
    assert devices.claim_pairing(phone, "phone") is not None


def test_cancel_with_no_kind_clears_everything(devices):
    phone, _ = devices.start_pairing()
    agent, _ = devices.start_pairing("agent")
    devices.cancel_pairings()
    assert devices._PENDING == {}
    assert devices.claim_pairing(phone, "phone") is None
    assert devices.claim_pairing(agent, "hub") is None


# ── Routes ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(devices, monkeypatch):
    """The pairing router on a bare app (no auth middleware — the allowlist is tested above)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["100.119.239.8", "192.168.5.31"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "remoteBox")
    return TestClient(app, base_url="http://testserver:7931")


def test_start_agent_route_shape(client, devices):
    r = client.post("/api/pairing/start", json={"kind": "agent"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["kind"] == "agent"
    assert body["ttl"] == 300 and body["name"] == "remoteBox"
    assert len(body["code"]) == 11 and body["code"][5] == "-"
    assert body["hosts"] == [
        {"host": "100.119.239.8", "kind": "tailnet", "url": "http://100.119.239.8:7931"},
        {"host": "192.168.5.31", "kind": "lan", "url": "http://192.168.5.31:7931"},
    ]  # base URLs a hub pairs against — no QR, no fragment


def test_start_with_no_body_is_still_a_device_code(client):
    r = client.post("/api/pairing/start")
    body = r.json()
    assert r.status_code == 200 and body["kind"] == "device"
    assert body["ttl"] == 120 and len(body["code"]) == 32
    assert body["hosts"][0]["url"].endswith(f"/app/#pair={body['code']}")


@pytest.mark.parametrize("payload", [{"kind": "agent"}, None])
def test_start_409s_when_loopback_bound_for_both_kinds(client, monkeypatch, payload):
    """An agent can't pair with a loopback-bound instance any more than a phone can."""
    import a2a_impl.auth as auth
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_BIND_HOST", ["127.0.0.1"])
    monkeypatch.setattr(auth, "_BEARER", [None])
    r = client.post("/api/pairing/start", json=payload) if payload else client.post("/api/pairing/start")
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["hosts"] == []
    assert body["available"] == [
        {"host": "100.119.239.8", "kind": "tailnet"},
        {"host": "192.168.5.31", "kind": "lan"},
    ]
    assert body["bind"] == "127.0.0.1" and body["auth_configured"] is False


def test_claim_kind_comes_from_the_code_not_the_body(client):
    phone = client.post("/api/pairing/start").json()["code"]
    r = client.post("/api/pairing/claim", json={"code": phone, "name": "sneaky", "kind": "agent"})
    assert r.status_code == 200 and r.json()["device"]["kind"] == "device"

    agent = client.post("/api/pairing/start", json={"kind": "agent"}).json()["code"]
    r = client.post(
        "/api/pairing/claim", json={"code": agent.lower().replace("-", ""), "name": "hub", "kind": "device"}
    )
    assert r.status_code == 200 and r.json()["device"]["kind"] == "agent"

    kinds = sorted(d["kind"] for d in client.get("/api/devices").json()["devices"])
    assert kinds == ["agent", "device"]


def test_second_claim_of_an_agent_code_is_403(client):
    code = client.post("/api/pairing/start", json={"kind": "agent"}).json()["code"]
    assert client.post("/api/pairing/claim", json={"code": code, "name": "hub"}).status_code == 200
    assert client.post("/api/pairing/claim", json={"code": code, "name": "again"}).status_code == 403


def test_cancel_route_is_kind_scoped(client):
    phone = client.post("/api/pairing/start").json()["code"]
    agent = client.post("/api/pairing/start", json={"kind": "agent"}).json()["code"]
    assert client.post("/api/pairing/cancel", json={"kind": "device"}).json() == {"ok": True}
    assert client.post("/api/pairing/claim", json={"code": phone, "name": "p"}).status_code == 403
    assert client.post("/api/pairing/claim", json={"code": agent, "name": "h"}).status_code == 200


def test_cancel_route_with_no_body_clears_everything(client):
    agent = client.post("/api/pairing/start", json={"kind": "agent"}).json()["code"]
    client.post("/api/pairing/cancel")
    assert client.post("/api/pairing/claim", json={"code": agent, "name": "h"}).status_code == 403
