"""ADR 0121 D1, #4336 S7.5 — a phone's address set and the one-tap handoff between origins.

Pinned here:

* ``addresses`` on the claim and on ``GET /api/devices/self``, in D1 order (tailnet HTTPS →
  the declared public URL → tailnet name/IP over http → LAN), with each one's requirement;
* ``POST /api/devices/self/handoff`` only for a paired device, only to an origin this
  instance offers (exact, after normalisation — no scheme swaps, lookalikes or junk);
* the handoff code: 60 s, single use, bound to the EXISTING device; claiming it rotates that
  device's token and keeps its id, name, scope and its single row;
* the old token's fate: valid until the new one is first used, then the 120 s grace, then
  refused WITHOUT the reuse revocation a plain rotation would trigger;
* a device revoked meanwhile can't be claimed back; the claim throttle and the public-path
  https check apply to handoff claims like any claim.

The tailscale CLI is never run (``FakeCLI`` from the S4 suite).
"""

from __future__ import annotations

import importlib
import json

import pytest

from infra import tailscale
from security import public_console
from tests.test_tailscale_reach import SERVED_443, FakeCLI

# Writes devices.json (chmod + os.replace) and patches infra.tailscale (sys.platform branches).
pytestmark = pytest.mark.platform_sensitive

_SECRET = "op-secret"
PUBLIC = "https://agent.example.com"
TS_HTTPS = "https://box.tail1234.ts.net"
TS_NAME = "http://box.tail1234.ts.net:7931"
TS_IP = "http://100.119.239.8:7931"
LAN = "http://192.168.5.31:7931"


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-s7-5")
    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(devices, monkeypatch):
    c = Clock()
    monkeypatch.setattr(devices, "_now", c)
    return c


@pytest.fixture
def audit(tmp_path, monkeypatch):
    import observability.audit as audit_mod

    logger = audit_mod.AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod, "audit_logger", logger)
    return tmp_path / "audit.jsonl"


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCLI()
    monkeypatch.setattr(tailscale, "_run", fake)
    tailscale.clear_cache()
    yield fake
    tailscale.clear_cache()


@pytest.fixture
def reach(monkeypatch, cli):
    """A wildcard-bound box on a tailnet (100.119.239.8) and a LAN (192.168.5.31), port 7931."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["100.119.239.8", "192.168.5.31"])
    monkeypatch.setattr(pr, "_BIND_HOST", ["0.0.0.0"])
    monkeypatch.setattr(pr, "_EXTRA_LISTEN", [])
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    return cli


@pytest.fixture
def api(devices, reach):
    """The real pairing routes behind the real auth middleware."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth
    import operator_api.pairing_routes as pr

    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    auth.install(app, bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    return TestClient(app, base_url="http://testserver:7931")


def _claimer(peer: str = "192.168.5.40", base_url: str = "http://testserver:7931"):
    """The claim route alone (it's unauthenticated), as a client at ``peer`` addressing ``base_url``."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _claim(http, code, headers=None):
    return http.post("/api/pairing/claim", json={"code": code, "name": "new-origin-browser"}, headers=headers or {})


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _phone(devices, name="Josh's iPhone"):
    code, _ = devices.start_pairing()
    return devices.claim_pairing(code, name)


def _rows(devices):
    return json.loads(devices._registry_path().read_text("utf-8"))


def _handoff(api, token, origin):
    return api.post("/api/devices/self/handoff", headers=_hdr(token), json={"origin": origin})


# ── The address set ─────────────────────────────────────────────────────────────────────


def test_addresses_in_d1_order_with_tailnet_https_and_the_public_url(api, reach, devices):
    reach.serve = SERVED_443
    public_console.set_public_console_url(PUBLIC)
    _device, token = _phone(devices)
    body = api.get("/api/devices/self", headers=_hdr(token)).json()
    assert body["addresses"] == [
        {"kind": "tailnet_https", "origin": TS_HTTPS, "requirement": "tailscale"},
        {"kind": "public", "origin": PUBLIC},
        {"kind": "tailnet", "origin": TS_NAME, "requirement": "tailscale"},
        {"kind": "tailnet", "origin": TS_IP, "requirement": "tailscale"},
        {"kind": "lan", "origin": LAN, "requirement": "same_network"},
    ]


def test_addresses_without_the_public_url_offer_nothing_public(api, reach, devices):
    reach.serve = SERVED_443
    _device, token = _phone(devices)
    kinds = [a["kind"] for a in api.get("/api/devices/self", headers=_hdr(token)).json()["addresses"]]
    assert kinds == ["tailnet_https", "tailnet", "tailnet", "lan"]


def test_addresses_without_tailscale(api, monkeypatch, devices):
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout: (-1, "", "not found"))
    tailscale.clear_cache()
    _device, token = _phone(devices)
    assert [a["origin"] for a in api.get("/api/devices/self", headers=_hdr(token)).json()["addresses"]] == [TS_IP, LAN]
    public_console.set_public_console_url(PUBLIC)
    assert [a["origin"] for a in api.get("/api/devices/self", headers=_hdr(token)).json()["addresses"]] == [
        PUBLIC,
        TS_IP,
        LAN,
    ]


def test_a_funnel_served_https_origin_carries_no_tailscale_requirement(api, reach, devices):
    reach.serve = dict(SERVED_443, AllowFunnel={"box.tail1234.ts.net:443": True})
    _device, token = _phone(devices)
    first = api.get("/api/devices/self", headers=_hdr(token)).json()["addresses"][0]
    assert first == {"kind": "tailnet_https", "origin": TS_HTTPS}


def test_the_claim_returns_the_address_set_for_a_phone_but_not_for_an_agent(reach, devices):
    reach.serve = SERVED_443
    code, _ = devices.start_pairing()
    body = _claim(_claimer(), code).json()
    assert body["ok"] is True and body["moved"] is False
    assert [a["origin"] for a in body["addresses"]] == [TS_HTTPS, TS_NAME, TS_IP, LAN]
    agent_code, _ = devices.start_pairing(devices.KIND_AGENT)
    assert "addresses" not in _claim(_claimer(), agent_code).json()


def test_self_with_the_shared_bearer_has_no_addresses(api):
    assert api.get("/api/devices/self", headers=_hdr(_SECRET)).json() == {"device": None}


def test_discovered_public_addresses_are_never_in_the_set(api, monkeypatch, devices):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_local_addresses", lambda: ["8.8.4.4", "192.168.5.31"])
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout: (-1, "", "not found"))
    tailscale.clear_cache()
    _device, token = _phone(devices)
    assert [a["origin"] for a in api.get("/api/devices/self", headers=_hdr(token)).json()["addresses"]] == [LAN]


# ── Origin validation ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://box.tail1234.ts.net", TS_HTTPS),
        ("https://box.tail1234.ts.net/", TS_HTTPS),
        ("HTTPS://Box.Tail1234.TS.net", TS_HTTPS),
        ("https://box.tail1234.ts.net:443", TS_HTTPS),
        ("http://192.168.5.31:80", "http://192.168.5.31"),
        ("http://box.tail1234.ts.net:7931", TS_NAME),
        ("http://[fd7a:115c:a1e0::1]:7931", "http://[fd7a:115c:a1e0::1]:7931"),
    ],
)
def test_normalize_origin_accepts_origins(raw, expected):
    from operator_api.pairing_routes import normalize_origin

    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        42,
        "box.tail1234.ts.net",
        "//box.tail1234.ts.net",
        "javascript:alert(1)",
        "ftp://box.tail1234.ts.net",
        "https://box.tail1234.ts.net/app/",
        "https://box.tail1234.ts.net//",
        "https://box.tail1234.ts.net?x=1",
        "https://box.tail1234.ts.net#x",
        "https://box.tail1234.ts.net?",
        "https://user@box.tail1234.ts.net",
        "https://evil.example@box.tail1234.ts.net",
        "https://box.tail1234.ts.net\\@evil.example",
        " https://box.tail1234.ts.net",
        "https://box.tail1234.ts.net\n",
        "https://box.tail1234.ts.net:99999",
        "https://box.tail1234.ts.net:abc",
        "https://" + "a" * 600 + ".net",
    ],
)
def test_normalize_origin_refuses_everything_else(raw):
    from operator_api.pairing_routes import normalize_origin

    assert normalize_origin(raw) == ""


# ── POST /api/devices/self/handoff ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",  # not offered at all
        "http://box.tail1234.ts.net",  # the https name over http
        "https://box.tail1234.ts.net:7931",  # the http name's port over https
        "https://100.119.239.8:7931",  # an http-only IP over https
        "http://agent.example.com",  # the public URL over http
        "https://box.tail1234.ts.net.evil.com",  # lookalike: an extra label
        "https://box.tail1234.ts.net.",  # lookalike: a trailing dot
        "https://xbox.tail1234.ts.net",  # lookalike: a prefix
        "https://agent.example.com@evil.com",  # userinfo
        "https://box.tail1234.ts.net/app/",  # a path
        "http://127.0.0.1:7931",  # loopback is never an offered address
        "",
    ],
)
def test_handoff_refuses_an_origin_this_instance_does_not_offer(api, reach, devices, origin):
    reach.serve = SERVED_443
    public_console.set_public_console_url(PUBLIC)
    _device, token = _phone(devices)
    r = _handoff(api, token, origin)
    assert r.status_code == 400 and r.json()["error"] == "origin_not_offered"
    assert "code" not in r.json() and "url" not in r.json()
    assert devices._HANDOFF == {}


def test_handoff_refuses_a_missing_or_non_string_origin(api, reach, devices):
    _device, token = _phone(devices)
    assert api.post("/api/devices/self/handoff", headers=_hdr(token)).status_code == 400
    assert api.post("/api/devices/self/handoff", headers=_hdr(token), json={"origin": ["x"]}).status_code == 400


def test_handoff_refuses_the_shared_bearer(api, reach, devices):
    r = _handoff(api, _SECRET, LAN)
    assert r.status_code == 404 and r.json()["error"] == "not_a_device"
    assert "no device to move" in r.json()["detail"]
    assert devices._PENDING == {}


def test_handoff_refuses_an_agent_pairing(api, reach, devices):
    code, _ = devices.start_pairing(devices.KIND_AGENT)
    _hub, token = devices.claim_pairing(code, "hub")
    r = _handoff(api, token, LAN)
    assert r.status_code == 409 and r.json()["error"] == "not_movable"


def test_handoff_mints_a_code_and_a_url_built_from_the_offered_origin(api, reach, devices):
    reach.serve = SERVED_443
    _device, token = _phone(devices)
    r = _handoff(api, token, "HTTPS://Box.Tail1234.ts.net/")  # normalised to the offered origin
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin"] == TS_HTTPS and body["ttl"] == 60
    assert body["url"] == f"{TS_HTTPS}/app/#pair={body['code']}"
    assert len(body["code"]) == 11 and body["code"][5] == "-"  # the S2 Crockford form
    assert set(body["code"].replace("-", "")) <= set(devices._AGENT_CODE_ALPHABET)
    assert r.headers["cache-control"] == "no-store"


def test_a_limited_phone_passes_the_device_scope_ceiling_for_its_own_handoff():
    from a2a_impl.credential_scope import ceiling_rule

    assert ceiling_rule("POST", "/api/devices/self/handoff") is None


# ── Claiming a handoff code ─────────────────────────────────────────────────────────────


def test_claim_rebinds_the_same_device_keeping_name_and_scope(api, reach, devices, clock, audit):
    device, old = _phone(devices)
    code = _handoff(api, old, TS_IP).json()["code"]
    body = _claim(_claimer("100.64.0.9"), code).json()
    assert body["ok"] is True and body["moved"] is True
    new = body["token"]
    assert new != old
    assert body["device"]["id"] == device["id"] and body["device"]["name"] == "Josh's iPhone"
    assert body["device"]["scope"] == "device" and "moved" not in body["device"]
    assert [a["origin"] for a in body["addresses"]] == [TS_NAME, TS_IP, LAN]
    rows = _rows(devices)
    assert len(rows) == 1 and rows[0]["id"] == device["id"] and rows[0]["name"] == "Josh's iPhone"
    assert rows[0]["prev_handed_off"] is True
    assert devices.verify_token(new).id == device["id"]
    assert "device.handed_off" in audit.read_text("utf-8")


def test_a_promoted_phone_keeps_full_access_through_a_handoff(api, reach, devices):
    device, old = _phone(devices)
    devices.set_device_scope(device["id"], "full")
    code = _handoff(api, old, LAN).json()["code"]
    body = _claim(_claimer(), code).json()
    assert body["device"]["scope"] == "full" and len(_rows(devices)) == 1


def test_the_handoff_code_is_single_use(api, reach, devices):
    _device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    assert _claim(_claimer(), code).status_code == 200
    assert _claim(_claimer(), code).status_code == 403
    assert len(_rows(devices)) == 1


def test_the_handoff_code_expires_at_60_seconds(api, reach, devices, monkeypatch):
    import time

    _device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    real_time = time.time
    monkeypatch.setattr(devices.time, "time", lambda: real_time() + 59)
    assert devices._match(code) is not None  # still pending at 59 s …
    monkeypatch.setattr(devices.time, "time", lambda: real_time() + devices.HANDOFF_TTL_SECONDS + 1)
    assert _claim(_claimer(), code).status_code == 403  # … gone after 60
    monkeypatch.setattr(devices.time, "time", real_time)
    assert devices.verify_token(old) is not None  # nothing changed for the phone


def test_old_token_rides_until_the_new_one_is_used_then_is_refused_without_revoking(api, reach, devices, clock, audit):
    """The design: the asking token becomes the previous token. Valid until the new token is
    first presented (so a failed store at the new origin strands nothing), then for the 120 s
    grace, then refused — and, because it was HANDED OFF, never a reuse revocation."""
    device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    new = _claim(_claimer(), code).json()["token"]

    # Not yet used at the new origin: the old origin is still this phone's, in full.
    clock.advance(3600)
    assert api.get("/api/devices/self", headers=_hdr(old)).json()["device"]["handed_off"] is False

    # The new origin boots: first use of the new token starts the old one's grace.
    assert api.get("/api/devices/self", headers=_hdr(new)).json()["device"]["handed_off"] is False
    old_view = api.get("/api/devices/self", headers=_hdr(old)).json()["device"]
    assert old_view["id"] == device["id"] and old_view["handed_off"] is True  # → the old tab drops it

    clock.advance(devices.ROTATION_GRACE_SECONDS)
    assert api.get("/api/devices", headers=_hdr(old)).status_code == 401
    assert api.get("/api/devices", headers=_hdr(old)).status_code == 401
    # No spurious reuse revocation: the device is still paired and the new token still works.
    assert api.get("/api/devices/self", headers=_hdr(new)).json()["device"]["id"] == device["id"]
    assert len(_rows(devices)) == 1
    assert "revoked_token_reuse" not in (audit.read_text("utf-8") if audit.exists() else "")


def test_a_later_rotation_restores_reuse_detection(api, reach, devices, clock, audit):
    """Handed-off is a property of THAT previous token: after the next plain rotation, a
    rotated-out token presented past its grace revokes the device again."""
    device, old = _phone(devices)
    new = _claim(_claimer(), _handoff(api, old, LAN).json()["code"]).json()["token"]
    devices.verify_token(new)
    clock.advance(devices.ROTATION_GRACE_SECONDS + 1)
    rotated = api.post("/api/devices/self/rotate", headers=_hdr(new)).json()["token"]
    devices.verify_token(rotated)
    clock.advance(devices.ROTATION_GRACE_SECONDS)
    assert devices.verify_token(new) is None
    assert devices.get_device(device["id"]) is None  # revoked: the theft signal is back
    assert "revoked_token_reuse" in audit.read_text("utf-8")


def test_a_failed_store_at_the_new_origin_leaves_the_old_origin_working(api, reach, devices, clock):
    """The new origin claimed but never stored (or never used) its token: the old origin keeps
    its token, and can move again — that newer handoff replaces the unused one."""
    device, old = _phone(devices)
    _claim(_claimer(), _handoff(api, old, LAN).json()["code"])  # its token is lost
    clock.advance(600)
    assert devices.verify_token(old).id == device["id"]
    retry = _handoff(api, old, LAN)
    assert retry.status_code == 200
    again = _claim(_claimer(), retry.json()["code"]).json()["token"]
    assert devices.verify_token(again).id == device["id"]
    assert len(_rows(devices)) == 1


def test_a_revoked_devices_pending_handoff_code_fails(api, reach, devices):
    device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    assert devices.revoke_device(device["id"])
    assert _claim(_claimer(), code).status_code == 403
    assert _rows(devices) == []


def test_a_revoked_device_whose_code_survived_still_cannot_come_back(api, reach, devices):
    """Belt and braces: even a code still pending when the row vanished (a hand-edited
    registry) rebinds nothing and mints nothing."""
    device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    devices._save([])  # the row is gone, the pending code is not
    assert _claim(_claimer(), code).status_code == 403
    assert _rows(devices) == []


def test_a_newer_handoff_replaces_the_older_code(api, reach, devices):
    _device, old = _phone(devices)
    first = _handoff(api, old, LAN).json()["code"]
    second = _handoff(api, old, TS_IP).json()["code"]
    assert _claim(_claimer(), first).status_code == 403
    assert _claim(_claimer(), second).status_code == 200


def test_closing_the_operators_pairing_dialog_does_not_cancel_a_move(api, reach, devices):
    _device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    devices.cancel_pairings()
    devices.cancel_pairings(devices.KIND_DEVICE)
    assert _claim(_claimer(), code).status_code == 200


def test_a_token_already_rotated_out_cannot_hand_off(api, reach, devices, clock):
    _device, old = _phone(devices)
    rotated = api.post("/api/devices/self/rotate", headers=_hdr(old)).json()["token"]
    devices.verify_token(rotated)  # first use: the old token is now in its grace
    r = _handoff(api, old, LAN)
    assert r.status_code == 409 and r.json()["error"] == "not_movable"


def test_handoff_claims_are_throttled_like_any_claim(api, reach, devices):
    _device, old = _phone(devices)
    code = _handoff(api, old, LAN).json()["code"]
    prober = _claimer("192.168.5.66")
    for _ in range(5):
        assert _claim(prober, "ZZZZZ-ZZZZZ").status_code == 403
    r = _claim(prober, code)
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert _claim(_claimer("192.168.5.67"), code).status_code == 200  # never evaluated: still good


def test_a_handoff_to_the_public_url_must_be_claimed_over_https(api, reach, devices):
    public_console.set_public_console_url(PUBLIC)
    _device, old = _phone(devices)
    body = _handoff(api, old, PUBLIC).json()
    assert body["url"] == f"{PUBLIC}/app/#pair={body['code']}"
    plain = _claim(_claimer("203.0.113.7", "http://agent.example.com"), body["code"])
    assert plain.status_code == 400 and plain.json()["reason"] == "public_not_https"
    via_edge = _claim(_claimer("127.0.0.1", "http://agent.example.com"), body["code"], {"X-Forwarded-Proto": "https"})
    assert via_edge.status_code == 200 and via_edge.json()["moved"] is True


def test_legacy_rows_load_without_the_handoff_field(devices):
    path = devices._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": "a", "name": "old", "token_sha256": "x" * 64, "created_at": 1.0}]), "utf-8")
    assert devices.get_device("a").prev_handed_off is False
