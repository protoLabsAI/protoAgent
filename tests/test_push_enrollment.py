"""ADR 0135 W2 — self-only push enrollment on the paired-device record.

Pinned here:

* a paired phone fetches the instance's VAPID public key and stores, renews and deletes its
  OWN push binding through ``/api/devices/self/push``;
* the binding lives on the device's row in ``devices.json`` — no second store — so a token
  rotation keeps it (under the next generation) and revoking the device deletes it in the
  same write;
* another device's token, the shared operator token, an agent pairing and an
  unauthenticated caller are refused;
* an endpoint that fails ``validate_push_endpoint``, unusable client keys and a key version
  the keyring no longer signs with are refused, and nothing is stored;
* no response ever carries the endpoint or the client keys.

DNS goes through ``socket.getaddrinfo`` (stubbed); nothing here sends a push.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import socket

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Writes devices.json and the VAPID keyring (chmod + os.replace + a file lock).
pytestmark = pytest.mark.platform_sensitive

_SECRET = "op-secret"
PUBLIC_IP = "93.184.216.34"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/cT9x-AbC_123:APA91bH"
ENDPOINT_2 = "https://web.push.apple.com/QGuLtB3nTJs0fJ7xyz_-AbC"
URL = "/api/devices/self/push"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _client_keys() -> dict:
    """What a browser's ``PushSubscription.toJSON().keys`` holds: a fresh P-256 point + a
    16-byte auth secret."""
    point = (
        ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    return {"p256dh": _b64url(point), "auth": _b64url(os.urandom(16))}


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-push-enroll")
    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod
    import security.push_keys as keys_mod

    importlib.reload(mod)
    importlib.reload(keys_mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


@pytest.fixture
def keys(devices):
    import security.push_keys as mod

    return mod


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Every push host resolves to a public address unless a test says otherwise."""
    answer = {"ip": PUBLIC_IP}

    def fake(host, *_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer["ip"], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return answer


@pytest.fixture
def api(devices, monkeypatch):
    """The real pairing routes behind the real auth middleware."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth
    import operator_api.pairing_routes as pr

    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    auth.install(app, bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    return TestClient(app, base_url="http://testserver:7931")


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _phone(devices, name="Josh's iPhone"):
    code, _ = devices.start_pairing()
    return devices.claim_pairing(code, name)


def _agent(devices):
    code, _ = devices.start_pairing(devices.KIND_AGENT)
    return devices.claim_pairing(code, "hub")


def _rows(devices) -> list[dict]:
    return json.loads(devices._registry_path().read_text("utf-8"))


def _row(devices, device_id) -> dict:
    return next(r for r in _rows(devices) if r["id"] == device_id)


def _subscription(api, token, endpoint=ENDPOINT, keys=None) -> dict:
    """The PUT body a console builds: the key version it was given + the browser's subscription."""
    version = api.get(URL, headers=_hdr(token)).json()["version"]
    return {"endpoint": endpoint, "keys": keys or _client_keys(), "vapid_version": version}


def _assert_no_secrets(response, body: dict) -> None:
    text = response.text
    for secret in (body["endpoint"], body["keys"]["p256dh"], body["keys"]["auth"]):
        assert secret not in text
    assert '"endpoint"' not in text and '"p256dh"' not in text and '"auth"' not in text and '"keys"' not in text


# ── The public key ───────────────────────────────────────────────────────────────────────


def test_a_paired_device_fetches_the_active_public_key_and_version(devices, keys, api):
    _dev, token = _phone(devices)
    r = api.get(URL, headers=_hdr(token))
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    active = keys.active_public_key()
    assert r.json() == {
        "ok": True,
        "public_key": active.public_key,
        "version": active.version,
        "binding": None,
        "renew": None,
    }
    # Exactly what `PushManager.subscribe` takes: an uncompressed P-256 point.
    raw = base64.urlsafe_b64decode(active.public_key + "=" * (-len(active.public_key) % 4))
    assert len(raw) == 65 and raw[0] == 4


def test_the_key_follows_a_rotation(devices, keys, api):
    _dev, token = _phone(devices)
    first = api.get(URL, headers=_hdr(token)).json()
    keys.rotate()
    second = api.get(URL, headers=_hdr(token)).json()
    assert second["version"] == first["version"] + 1
    assert second["public_key"] != first["public_key"]


def test_a_lost_keyring_with_enrolled_devices_is_an_error_not_a_new_key(devices, keys, api):
    dev, token = _phone(devices)
    assert api.put(URL, headers=_hdr(token), json=_subscription(api, token)).status_code == 200
    path = keys._keyring_path()
    path.unlink()
    r = api.get(URL, headers=_hdr(token))
    assert r.status_code == 503
    assert r.json()["error"] == "push_keyring_unavailable"
    assert str(path) not in r.text  # the host path is for the log, not the phone
    assert not path.exists()


# ── Store, renew, delete ─────────────────────────────────────────────────────────────────


def test_a_device_stores_its_own_binding_on_its_own_row(devices, clock_free_now, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "ok": True,
        "binding": {
            "vapid_version": body["vapid_version"],
            "generation": 1,
            "created_at": clock_free_now.t,
            "renewed_at": clock_free_now.t,
        },
    }
    _assert_no_secrets(r, body)
    stored = _row(devices, dev["id"])["push"]
    assert stored == {
        "endpoint": ENDPOINT,
        "p256dh": body["keys"]["p256dh"],
        "auth": body["keys"]["auth"],
        "vapid_version": body["vapid_version"],
        "generation": 1,
        "created_at": clock_free_now.t,
        "renewed_at": clock_free_now.t,
    }
    # One store: the registry file, nothing beside it.
    assert devices.push_bindings_exist() is True
    got = api.get(URL, headers=_hdr(token))
    assert got.json()["binding"] == r.json()["binding"]
    _assert_no_secrets(got, body)


def test_storing_the_same_subscription_again_renews_it(devices, clock_free_now, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    api.put(URL, headers=_hdr(token), json=body)
    enrolled = clock_free_now.t
    clock_free_now.advance(3600)
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.json()["binding"] == {
        "vapid_version": body["vapid_version"],
        "generation": 1,
        "created_at": enrolled,
        "renewed_at": enrolled + 3600,
    }


def test_a_new_subscription_replaces_the_old_one_under_the_next_generation(devices, clock_free_now, api):
    dev, token = _phone(devices)
    api.put(URL, headers=_hdr(token), json=_subscription(api, token))
    enrolled = clock_free_now.t
    clock_free_now.advance(60)
    second = _subscription(api, token, endpoint=ENDPOINT_2)
    r = api.put(URL, headers=_hdr(token), json=second)
    assert r.status_code == 200
    assert r.json()["binding"]["generation"] == 2
    assert r.json()["binding"]["created_at"] == enrolled
    stored = _row(devices, dev["id"])["push"]
    assert stored["endpoint"] == ENDPOINT_2 and stored["p256dh"] == second["keys"]["p256dh"]
    assert ENDPOINT not in devices._registry_path().read_text("utf-8")


def test_a_device_deletes_its_own_binding_and_generations_are_never_reused(devices, api):
    dev, token = _phone(devices)
    api.put(URL, headers=_hdr(token), json=_subscription(api, token))
    r = api.delete(URL, headers=_hdr(token))
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    assert _row(devices, dev["id"])["push"] is None
    assert ENDPOINT not in devices._registry_path().read_text("utf-8")
    assert api.get(URL, headers=_hdr(token)).json()["binding"] is None
    assert api.delete(URL, headers=_hdr(token)).json() == {"ok": True, "removed": False}
    # A late result for the deleted subscription (generation 1) can't be taken for this one.
    again = api.put(URL, headers=_hdr(token), json=_subscription(api, token))
    assert again.json()["binding"]["generation"] == 2


def test_the_binding_survives_a_token_rotation_and_a_reload(devices, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    api.put(URL, headers=_hdr(token), json=body)
    _view, new_token = devices.rotate_device(dev["id"], token)
    importlib.reload(devices)
    assert devices.get_device(dev["id"]).push.endpoint == ENDPOINT
    # W2a3: the rotation moved it to the next generation, so sends queued under 1 are dropped.
    assert api.get(URL, headers=_hdr(new_token)).json()["binding"]["generation"] == 2


def test_a_replaced_token_in_its_grace_cannot_change_the_binding(devices, clock_free_now, api):
    """After a rotation (or a handoff) the NEW token has been used: the old one still
    authenticates for its grace, but it must not overwrite or delete the binding."""
    dev, old = _phone(devices)
    body = _subscription(api, old)
    api.put(URL, headers=_hdr(old), json=body)
    _view, new = devices.rotate_device(dev["id"], old)
    assert api.get("/api/devices/self", headers=_hdr(new)).status_code == 200  # confirms the new token
    clock_free_now.advance(5)
    r = api.put(URL, headers=_hdr(old), json=_subscription(api, old, endpoint=ENDPOINT_2))
    assert r.status_code == 409 and r.json()["error"] == "not_enrollable"
    assert api.delete(URL, headers=_hdr(old)).status_code == 409
    assert _row(devices, dev["id"])["push"]["endpoint"] == ENDPOINT


# ── Revoke ───────────────────────────────────────────────────────────────────────────────


def test_revoking_a_device_removes_its_binding_in_the_same_write(devices, api, monkeypatch):
    dev, token = _phone(devices)
    other, other_token = _phone(devices, "Pixel")
    body = _subscription(api, token)
    api.put(URL, headers=_hdr(token), json=body)
    api.put(URL, headers=_hdr(other_token), json=_subscription(api, other_token, endpoint=ENDPOINT_2))

    writes = []
    real_save = devices._save
    monkeypatch.setattr(devices, "_save", lambda rows: (writes.append(1), real_save(rows))[1])
    assert api.delete(f"/api/devices/{dev['id']}", headers=_hdr(_SECRET)).status_code == 200
    assert len(writes) == 1  # the row and its binding went in ONE atomic write

    text = devices._registry_path().read_text("utf-8")
    assert ENDPOINT not in text and body["keys"]["auth"] not in text and body["keys"]["p256dh"] not in text
    assert [r["id"] for r in _rows(devices)] == [other["id"]]
    assert _row(devices, other["id"])["push"]["endpoint"] == ENDPOINT_2  # the other device keeps its own
    assert devices.push_status(dev["id"]) is None
    assert api.get(URL, headers=_hdr(token)).status_code == 401  # and its token is dead


def test_a_reuse_revocation_takes_the_binding_with_the_row(devices, clock_free_now, api):
    dev, old = _phone(devices)
    api.put(URL, headers=_hdr(old), json=_subscription(api, old))
    _view, new = devices.rotate_device(dev["id"], old)
    assert devices.verify_token(new) is not None
    clock_free_now.advance(devices.ROTATION_GRACE_SECONDS + 1)
    assert devices.verify_token(old) is None  # reuse detection revokes the device
    assert _rows(devices) == []
    assert devices.push_bindings_exist() is False


# ── Who may call ─────────────────────────────────────────────────────────────────────────


def test_a_device_can_only_ever_reach_its_own_binding(devices, api):
    a, a_token = _phone(devices, "A")
    b, b_token = _phone(devices, "B")
    a_body = _subscription(api, a_token)
    api.put(URL, headers=_hdr(a_token), json=a_body)

    # B's token reads B's (empty) binding, never A's.
    got = api.get(URL, headers=_hdr(b_token))
    assert got.json()["binding"] is None
    _assert_no_secrets(got, a_body)
    # B can't take A's endpoint for itself…
    stolen = api.put(URL, headers=_hdr(b_token), json=dict(a_body, keys=_client_keys()))
    assert stolen.status_code == 409 and stolen.json()["error"] == "endpoint_in_use"
    _assert_no_secrets(stolen, a_body)
    assert _row(devices, b["id"])["push"] is None
    # …and B's delete and B's own PUT leave A's row alone.
    assert api.delete(URL, headers=_hdr(b_token)).json() == {"ok": True, "removed": False}
    assert api.put(URL, headers=_hdr(b_token), json=_subscription(api, b_token, endpoint=ENDPOINT_2)).status_code == 200
    assert _row(devices, a["id"])["push"]["endpoint"] == ENDPOINT
    assert _row(devices, a["id"])["push"]["auth"] == a_body["keys"]["auth"]
    # There is no route that names another device's binding, with any credential.
    for token in (b_token, _SECRET):
        for method in ("get", "put", "delete"):
            r = getattr(api, method)(f"/api/devices/{a['id']}/push", headers=_hdr(token))
            assert r.status_code in (404, 405)
    assert _row(devices, a["id"])["push"]["endpoint"] == ENDPOINT


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_the_shared_operator_token_is_refused(devices, keys, api, method):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    kwargs = {"json": body} if method == "put" else {}
    r = getattr(api, method)(URL, headers=_hdr(_SECRET), **kwargs)
    assert r.status_code == 403 and r.json()["error"] == "not_a_device"
    assert _row(devices, dev["id"])["push"] is None


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_an_agent_pairing_is_refused(devices, api, method):
    phone, phone_token = _phone(devices)
    body = _subscription(api, phone_token)
    agent, agent_token = _agent(devices)
    assert agent["kind"] == "agent"
    kwargs = {"json": body} if method == "put" else {}
    r = getattr(api, method)(URL, headers=_hdr(agent_token), **kwargs)
    assert r.status_code == 403 and r.json()["error"] == "not_a_device"
    assert "public_key" not in r.text
    assert _row(devices, agent["id"])["push"] is None


@pytest.mark.parametrize("method", ["get", "put", "delete"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-token"}])
def test_an_unauthenticated_caller_is_refused(devices, api, method, headers):
    r = getattr(api, method)(URL, headers=headers)
    assert r.status_code == 401
    assert _rows_or_empty(devices) == []


def _rows_or_empty(devices) -> list:
    return _rows(devices) if devices._registry_path().exists() else []


def test_the_store_refuses_agents_expired_devices_and_foreign_tokens(devices, clock_free_now):
    kwargs = {"endpoint": ENDPOINT, "p256dh": "p", "auth": "a", "vapid_version": 1}
    phone, phone_token = _phone(devices)
    other, other_token = _phone(devices, "other")
    agent, agent_token = _agent(devices)
    assert devices.set_push_binding(agent["id"], agent_token, **kwargs) is None
    assert devices.set_push_binding(phone["id"], other_token, **kwargs) is None  # another device's token
    assert devices.set_push_binding(phone["id"], "", **kwargs) is None
    assert devices.set_push_binding("nope", phone_token, **kwargs) is None
    assert devices.set_push_binding(None, phone_token, **kwargs) is None
    assert devices.clear_push_binding(phone["id"], other_token) is None
    assert devices.push_bindings_exist() is False
    assert devices.set_push_binding(phone["id"], phone_token, **kwargs) is not None
    clock_free_now.advance((devices.DEFAULT_IDLE_TTL_DAYS + 1) * 86400)
    assert devices.set_push_binding(phone["id"], phone_token, **dict(kwargs, endpoint=ENDPOINT_2)) is None
    assert devices.clear_push_binding(phone["id"], phone_token) is None


# ── What may be stored ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://evil.example.com/fcm/send/abc",
        "https://user:pw@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://127.0.0.1/fcm/send/abc",
        "https://fcm.googleapis.com/",
        "",
        None,
        ["https://fcm.googleapis.com/fcm/send/abc"],
    ],
)
def test_an_endpoint_that_fails_validation_is_refused_and_nothing_is_stored(devices, api, endpoint):
    dev, token = _phone(devices)
    body = _subscription(api, token, endpoint=endpoint)
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_endpoint"
    if isinstance(endpoint, str) and endpoint:
        assert endpoint not in r.text
    assert _row(devices, dev["id"])["push"] is None
    assert devices.push_bindings_exist() is False


def test_an_endpoint_resolving_inward_is_refused_and_keeps_the_existing_binding(devices, api, dns):
    dev, token = _phone(devices)
    api.put(URL, headers=_hdr(token), json=_subscription(api, token))
    dns["ip"] = "10.0.0.7"
    r = api.put(URL, headers=_hdr(token), json=_subscription(api, token, endpoint=ENDPOINT_2))
    assert r.status_code == 400 and r.json()["error"] == "invalid_endpoint"
    stored = _row(devices, dev["id"])["push"]
    assert stored["endpoint"] == ENDPOINT and stored["generation"] == 1


def test_the_stored_endpoint_is_the_canonical_form(devices, api):
    dev, token = _phone(devices)
    body = _subscription(api, token, endpoint="HTTPS://FCM.GoogleAPIs.com:443/fcm/send/AbC?x=1")
    assert api.put(URL, headers=_hdr(token), json=body).status_code == 200
    assert _row(devices, dev["id"])["push"]["endpoint"] == "https://fcm.googleapis.com/fcm/send/AbC?x=1"


@pytest.mark.parametrize(
    "keys_value",
    [
        None,
        "p256dh",
        {},
        {"p256dh": _b64url(b"\x04" + b"\x00" * 64), "auth": _b64url(b"a" * 16)},  # not on the curve
        {"p256dh": _b64url(b"\x04" * 33), "auth": _b64url(b"a" * 16)},  # wrong length
        {"p256dh": "!!!not base64!!!", "auth": _b64url(b"a" * 16)},
        {"p256dh": 7, "auth": _b64url(b"a" * 16)},
    ],
)
def test_unusable_client_keys_are_refused_and_nothing_is_stored(devices, api, keys_value):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    body["keys"] = keys_value
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_keys"
    assert _row(devices, dev["id"])["push"] is None


@pytest.mark.parametrize("auth", ["", "short", _b64url(b"a" * 15), _b64url(b"a" * 17), None])
def test_a_bad_auth_secret_is_refused(devices, api, auth):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    body["keys"]["auth"] = auth
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_keys"
    assert _row(devices, dev["id"])["push"] is None


def test_padded_client_keys_are_stored_unpadded(devices, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    sent = {k: v + "=" * (-len(v) % 4) for k, v in body["keys"].items()}
    assert api.put(URL, headers=_hdr(token), json=dict(body, keys=sent)).status_code == 200
    stored = _row(devices, dev["id"])["push"]
    assert (stored["p256dh"], stored["auth"]) == (body["keys"]["p256dh"], body["keys"]["auth"])


@pytest.mark.parametrize("body", [None, [], "x"])
def test_a_body_that_is_not_an_object_is_refused(devices, api, body):
    dev, token = _phone(devices)
    r = api.put(URL, headers=_hdr(token), json=body) if body is not None else api.put(URL, headers=_hdr(token))
    assert r.status_code == 400
    assert _row(devices, dev["id"])["push"] is None


@pytest.mark.parametrize("version", [None, 0, 99, "1", True, 1.0])
def test_an_unknown_key_version_is_refused(devices, api, version):
    dev, token = _phone(devices)
    body = dict(_subscription(api, token), vapid_version=version)
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 409 and r.json()["error"] == "stale_vapid_version"
    assert _row(devices, dev["id"])["push"] is None


def test_the_key_version_must_be_active_or_inside_its_migration_window(devices, keys, api, monkeypatch):
    dev, token = _phone(devices)
    body = _subscription(api, token)  # subscribed under version 1
    t = {"now": keys._now()}
    monkeypatch.setattr(keys, "_now", lambda: t["now"])
    keys.rotate()  # version 2 is active; version 1 has 30 days left
    assert api.put(URL, headers=_hdr(token), json=body).status_code == 200
    assert _row(devices, dev["id"])["push"]["vapid_version"] == 1

    current = _subscription(api, token, endpoint=ENDPOINT_2)
    assert current["vapid_version"] == 2
    assert api.put(URL, headers=_hdr(token), json=current).status_code == 200

    t["now"] += keys.ROTATION_WINDOW_SECONDS + 1  # the window closed (the key isn't pruned yet)
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 409 and r.json()["error"] == "stale_vapid_version"
    stored = _row(devices, dev["id"])["push"]
    assert (stored["endpoint"], stored["vapid_version"]) == (ENDPOINT_2, 2)


def test_a_retired_key_version_is_refused_at_once(devices, keys, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    keys.rotate()
    assert keys.retire(1) is True
    r = api.put(URL, headers=_hdr(token), json=body)
    assert r.status_code == 409 and r.json()["error"] == "stale_vapid_version"
    assert _row(devices, dev["id"])["push"] is None


# ── The record ───────────────────────────────────────────────────────────────────────────


def test_no_device_listing_or_repr_carries_the_binding_secrets(devices, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    api.put(URL, headers=_hdr(token), json=body)
    secrets = (ENDPOINT, body["keys"]["p256dh"], body["keys"]["auth"])
    listing = api.get("/api/devices", headers=_hdr(_SECRET)).text
    me = api.get("/api/devices/self", headers=_hdr(token)).text
    device = devices.get_device(dev["id"])
    for secret in secrets:
        assert secret not in listing and secret not in me
        assert secret not in repr(device) and secret not in repr(device.push)
    assert set(device.push.public()) == {"vapid_version", "generation", "created_at", "renewed_at"}
    assert set(devices.push_status(dev["id"])) == set(device.push.public())


def test_rows_written_before_push_load_without_a_binding(devices):
    dev, token = _phone(devices)
    rows = _rows(devices)
    for row in rows:
        row.pop("push")
        row.pop("push_generation")
    devices._registry_path().write_text(json.dumps(rows), "utf-8")
    loaded = devices.get_device(dev["id"])
    assert loaded.push is None and loaded.push_generation == 0
    assert devices.verify_token(token) is not None


@pytest.mark.parametrize(
    "junk",
    ["x", [], {}, {"endpoint": ENDPOINT}, {"endpoint": 1, "p256dh": "p", "auth": "a", "vapid_version": 1}],
)
def test_a_hand_edited_binding_reads_as_not_enrolled_and_keeps_the_device(devices, junk):
    dev, token = _phone(devices)
    rows = _rows(devices)
    rows[0]["push"] = junk
    devices._registry_path().write_text(json.dumps(rows), "utf-8")
    assert devices.get_device(dev["id"]).push is None
    assert devices.verify_token(token) is not None


def test_the_push_routes_are_off_the_credential_plane_ceiling():
    """A phone enabling its own notifications changes nothing about who can get in."""
    from a2a_impl.credential_scope import ceiling_rule

    for method in ("GET", "PUT", "DELETE"):
        assert ceiling_rule(method, URL) is None


# ── Fixtures that need the module above ──────────────────────────────────────────────────


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock_free_now(devices, monkeypatch):
    """The registry's clock, pinned and movable."""
    c = Clock()
    monkeypatch.setattr(devices, "_now", c)
    return c


# ── Key migration is reported on the device's next visit (W2a3) ──────────────────────────


def test_a_binding_stranded_by_a_retired_key_is_cleared_and_reported_on_the_next_get(devices, keys, api):
    dev, token = _phone(devices)
    body = _subscription(api, token)
    api.put(URL, headers=_hdr(token), json=body)
    assert api.get(URL, headers=_hdr(token)).json()["renew"] is None
    assert keys.retire(body["vapid_version"]) is True
    r = api.get(URL, headers=_hdr(token))
    assert r.json()["binding"] is None and r.json()["renew"] == "vapid_key"
    assert r.json()["version"] == body["vapid_version"] + 1  # the key to subscribe again with
    _assert_no_secrets(r, body)
    assert "push" in _row(devices, dev["id"]) and _row(devices, dev["id"])["push"] is None
    again = api.put(URL, headers=_hdr(token), json=_subscription(api, token))
    assert again.json()["binding"]["generation"] == 2
    assert api.get(URL, headers=_hdr(token)).json()["renew"] is None


@pytest.mark.parametrize("credential", ["shared", "device"])
def test_key_status_does_not_enable_push(devices, keys, api, credential):
    token = _SECRET if credential == "shared" else _phone(devices)[1]
    response = api.get("/api/push/keys", headers=_hdr(token))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "keys": [], "rotation_window_seconds": keys.ROTATION_WINDOW_SECONDS, "bindings_by_version": {},
    }
    assert not keys._keyring_path().exists()


@pytest.mark.parametrize("credential", ["shared", "device"])
def test_operator_rotates_keys_and_status_counts_migrating_bindings(devices, keys, api, monkeypatch, caplog, credential):
    import logging
    import threading

    dev, token = _phone(devices)
    body = _subscription(api, token)
    assert api.put(URL, headers=_hdr(token), json=body).status_code == 200
    if credential == "device":
        devices.set_device_scope(dev["id"], "full")
    caller = token if credential == "device" else _SECRET
    now = keys._now()
    monkeypatch.setattr(keys, "_now", lambda: now)
    rotate = keys.rotate

    def checked_rotate(**kwargs):
        # TestClient's portal runs the event loop; key I/O belongs in a worker.
        import asyncio

        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert threading.current_thread() is not threading.main_thread()
        return rotate(**kwargs)

    monkeypatch.setattr(keys, "rotate", checked_rotate)
    with caplog.at_level(logging.INFO, logger="protoagent.server.pairing"):
        response = api.post("/api/push/keys/rotate", headers=_hdr(caller))
    assert response.status_code == 200
    fresh = response.json()
    assert fresh == keys.public_keys()[-1].public()
    assert set(fresh) == {"version", "public_key", "created_at", "active", "retire_after"}
    assert fresh["version"] == body["vapid_version"] + 1
    assert fresh["active"] is True and fresh["retire_after"] is None
    assert fresh["created_at"] == now
    assert (dev["id"] if credential == "device" else "None") in caplog.text
    status = api.get("/api/push/keys", headers=_hdr(token))
    assert status.json() == {
        "keys": [key.public() for key in keys.public_keys()],
        "rotation_window_seconds": keys.ROTATION_WINDOW_SECONDS,
        "bindings_by_version": {str(body["vapid_version"]): 1, str(fresh["version"]): 0},
    }
    old = status.json()["keys"][0]
    assert old["active"] is False
    assert old["retire_after"] == now + keys.ROTATION_WINDOW_SECONDS
    for key in status.json()["keys"]:
        assert set(key) == set(fresh)
    for secret in (body["endpoint"], *body["keys"].values()):
        assert secret not in status.text and secret not in response.text
    # A renewed binding moves counts, without a stale status cache.
    body["vapid_version"] = fresh["version"]
    assert api.put(URL, headers=_hdr(token), json=body).status_code == 200
    assert api.get("/api/push/keys", headers=_hdr(token)).json()["bindings_by_version"] == {
        str(old["version"]): 0, str(fresh["version"]): 1,
    }


@pytest.mark.parametrize("method, path", [("GET", "/api/push/keys"), ("POST", "/api/push/keys/rotate")])
def test_key_management_requires_authentication(api, method, path):
    assert api.request(method, path).status_code == 401


@pytest.mark.parametrize("method, path", [("GET", "/api/push/keys"), ("POST", "/api/push/keys/rotate")])
def test_key_management_reports_corruption_without_exposing_it(devices, keys, api, method, path):
    keys.active_public_key()
    keyring = keys._keyring_path()
    keyring.write_text("broken-secret-keyring", "utf-8")
    response = api.request(method, path, headers=_hdr(_SECRET))
    assert response.status_code == 503
    assert response.json()["error"] == "push_keyring_unavailable"
    assert str(keyring) not in response.text and "broken-secret-keyring" not in response.text
    assert keyring.read_text("utf-8") == "broken-secret-keyring"


def test_rotation_does_not_replace_a_lost_keyring_with_bindings(devices, keys, api):
    _, token = _phone(devices)
    assert api.put(URL, headers=_hdr(token), json=_subscription(api, token)).status_code == 200
    keyring = keys._keyring_path()
    keyring.unlink()
    response = api.post("/api/push/keys/rotate", headers=_hdr(_SECRET))
    assert response.status_code == 503
    assert response.json()["error"] == "push_keyring_unavailable"
    assert not keyring.exists()
