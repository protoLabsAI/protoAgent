"""Push security primitives (ADR 0135 W2, #5539): the per-instance VAPID keyring and
push endpoint validation in ``security.push_keys``.

Time moves through ``security.push_keys._now``; DNS through ``socket.getaddrinfo``.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import socket
import threading

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from tests.privacy_asserts import assert_owner_only

# Writes the keyring (chmod + os.replace + a file lock) through security.push_keys.
pytestmark = pytest.mark.platform_sensitive

DAY = 86400.0
PUBLIC_IP = "93.184.216.34"


def _scope(monkeypatch, tmp_path, instance: str):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", instance)

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.push_keys as mod

    return importlib.reload(mod)


@pytest.fixture
def keys(tmp_path, monkeypatch):
    yield _scope(monkeypatch, tmp_path, "test-push-keys")

    import infra.paths

    infra.paths.reset_instance_paths()


def _restart(mod):
    """A process restart: nothing in memory survives, only the file."""
    return importlib.reload(mod)


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _stored(mod) -> dict:
    return json.loads(mod._keyring_path().read_text("utf-8"))


def _private_material(mod) -> list[str]:
    return [k["private_key"] for k in _stored(mod)["keys"]]


def _public_point(private_key) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


# ── Keyring: first use, persistence ──────────────────────────────────────────────────────


def test_first_use_creates_an_owner_only_keyring_under_the_push_store(keys):
    from infra.paths import instance_paths

    path = keys._keyring_path()
    assert not path.exists()

    key = keys.active_public_key()

    assert path.parent == instance_paths().store("push")
    assert path.is_file()
    assert_owner_only(path)
    assert key.version == 1 and key.active is True and key.retire_after is None
    point = _decode(key.public_key)
    # The browser's applicationServerKey: an uncompressed P-256 point.
    assert len(point) == 65 and point[0] == 0x04
    assert "=" not in key.public_key


def test_restart_reuses_the_same_active_key_and_version(keys):
    first = keys.active_public_key()
    before = keys._keyring_path().read_bytes()

    again = _restart(keys).active_public_key()

    assert (again.version, again.public_key) == (first.version, first.public_key)
    assert keys._keyring_path().read_bytes() == before


def test_named_instances_get_independent_keys(tmp_path, monkeypatch):
    import infra.paths

    one = _scope(monkeypatch, tmp_path, "push-one").active_public_key()
    two = _scope(monkeypatch, tmp_path, "push-two").active_public_key()
    infra.paths.reset_instance_paths()

    assert one.public_key != two.public_key


def test_concurrent_first_use_mints_exactly_one_key(keys):
    seen: list[str] = []
    barrier = threading.Barrier(8)

    def enable():
        barrier.wait()
        seen.append(keys.active_public_key().public_key)

    threads = [threading.Thread(target=enable) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 8 and len(set(seen)) == 1
    assert len(_stored(keys)["keys"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows relies on the ACL set at write")
def test_a_keyring_left_readable_by_others_is_tightened_on_load(keys):
    keys.active_public_key()
    path = keys._keyring_path()
    os.chmod(path, 0o644)

    keys.active_public_key()

    assert_owner_only(path)


def test_the_signing_key_signs_for_the_served_public_key(keys):
    public = keys.active_public_key()

    private = keys.signing_key(public.version)

    assert _public_point(private) == _decode(public.public_key)
    signature = private.sign(b"vapid-claims", ec.ECDSA(hashes.SHA256()))
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _decode(public.public_key)).verify(
        signature, b"vapid-claims", ec.ECDSA(hashes.SHA256())
    )


def test_signing_key_is_none_for_a_version_that_never_existed(keys):
    keys.active_public_key()

    assert keys.signing_key(7) is None
    assert keys.signing_key("1") is None


# ── Keyring: rotation and retirement ─────────────────────────────────────────────────────


def test_rotate_activates_a_new_version_and_keeps_the_old_signing_key(keys, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(keys, "_now", lambda: now)
    v1 = keys.active_public_key()

    v2 = keys.rotate()

    assert v2.version == 2 and v2.active and v2.public_key != v1.public_key
    assert keys.active_public_key().version == 2
    assert _restart(keys).active_public_key().public_key == v2.public_key
    monkeypatch.setattr(keys, "_now", lambda: now)
    # The old key still signs, and is still the key v1 subscriptions were created against.
    assert _public_point(keys.signing_key(1)) == _decode(v1.public_key)
    assert _public_point(keys.signing_key(2)) == _decode(v2.public_key)
    listed = keys.public_keys()
    assert [(k.version, k.active) for k in listed] == [(1, False), (2, True)]
    assert listed[0].retire_after == now + 30 * DAY
    assert keys.ROTATION_WINDOW_SECONDS == 30 * DAY


def test_the_old_key_stops_signing_when_the_migration_window_closes(keys, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(keys, "_now", lambda: clock[0])
    keys.active_public_key()
    keys.rotate()

    clock[0] += 30 * DAY - 1
    assert keys.signing_key(1) is not None
    assert keys.prune_expired() == []

    clock[0] += 1
    assert keys.signing_key(1) is None  # refused before it is even deleted
    assert keys.prune_expired() == [1]
    assert [k["version"] for k in _stored(keys)["keys"]] == [2]
    assert keys.prune_expired() == []
    assert keys.signing_key(2) is not None


def test_rotate_drops_versions_whose_window_already_closed(keys, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(keys, "_now", lambda: clock[0])
    keys.active_public_key()
    keys.rotate()
    clock[0] += 31 * DAY

    v3 = keys.rotate()

    assert v3.version == 3
    assert [k.version for k in keys.public_keys()] == [2, 3]


def test_retire_removes_a_rotated_out_key_immediately(keys):
    keys.active_public_key()
    keys.rotate()

    assert keys.retire(1) is True

    assert keys.signing_key(1) is None
    assert [k["version"] for k in _stored(keys)["keys"]] == [2]
    assert keys.active_public_key().version == 2
    assert keys.retire(1) is False  # already gone
    assert_owner_only(keys._keyring_path())


def test_retiring_the_active_key_activates_a_fresh_version_never_a_reused_one(keys):
    v1 = keys.active_public_key()

    assert keys.retire(1) is True

    replacement = keys.active_public_key()
    assert replacement.version == 2 and replacement.public_key != v1.public_key
    assert keys.signing_key(1) is None
    # Retire the newest too: the next version is 3, not a second "2".
    assert keys.retire(2) is True
    assert keys.active_public_key().version == 3
    assert keys.rotate().version == 4


# ── Keyring: corruption and loss fail visibly ────────────────────────────────────────────


def _tamper_public(doc: dict) -> dict:
    doc["keys"][0]["public_key"] = doc["keys"][0]["public_key"][:-4] + "AAAA"
    return doc


def _drop_active(doc: dict) -> dict:
    doc["active"] = 9
    return doc


def _wrong_schema(doc: dict) -> dict:
    doc["schema"] = 99
    return doc


def _bad_private(doc: dict) -> dict:
    doc["keys"][0]["private_key"] = "AAAA"
    return doc


def _duplicate_version(doc: dict) -> dict:
    doc["keys"].append(dict(doc["keys"][0]))
    return doc


@pytest.mark.parametrize(
    "damage",
    [
        lambda doc: "{ not json",
        lambda doc: json.dumps(doc)[: len(json.dumps(doc)) // 2],
        lambda doc: "[]",
        lambda doc: json.dumps({**doc, "keys": []}),
        lambda doc: json.dumps(_tamper_public(doc)),
        lambda doc: json.dumps(_drop_active(doc)),
        lambda doc: json.dumps(_wrong_schema(doc)),
        lambda doc: json.dumps(_bad_private(doc)),
        lambda doc: json.dumps(_duplicate_version(doc)),
    ],
    ids=[
        "garbage",
        "truncated",
        "wrong-shape",
        "no-keys",
        "halves-mismatch",
        "no-active",
        "schema",
        "bad-private",
        "dup",
    ],
)
def test_a_corrupt_keyring_raises_instead_of_minting_new_keys(keys, damage):
    keys.active_public_key()
    path = keys._keyring_path()
    secret = _private_material(keys)[0]
    path.write_text(damage(_stored(keys)), "utf-8")
    corrupt = path.read_bytes()

    for call in (
        keys.active_public_key,
        keys.public_keys,
        keys.rotate,
        keys.prune_expired,
        lambda: keys.signing_key(1),
        lambda: keys.retire(1),
    ):
        with pytest.raises(keys.PushKeyringError) as err:
            call()
        message = str(err.value)
        assert str(path) in message and "NOT regenerated" in message
        assert secret not in message  # the error names the file, never quotes it
    assert path.read_bytes() == corrupt  # nothing was written over the evidence


def test_a_lost_keyring_with_existing_bindings_raises_and_creates_nothing(keys):
    path = keys._keyring_path()

    for call in (lambda: keys.active_public_key(bindings_exist=True), lambda: keys.rotate(bindings_exist=True)):
        with pytest.raises(keys.PushKeyringError, match="missing"):
            call()
    # A binding names a version, so asking to sign or retire without a keyring is a loss too.
    with pytest.raises(keys.PushKeyringError, match="missing"):
        keys.signing_key(1)
    with pytest.raises(keys.PushKeyringError, match="missing"):
        keys.retire(1)

    assert not path.exists()
    assert keys.public_keys() == [] and keys.prune_expired() == []
    assert not path.exists()  # the read-only accessors never create one either


def test_bindings_exist_is_harmless_once_the_keyring_is_there(keys):
    first = keys.active_public_key()

    assert keys.active_public_key(bindings_exist=True).public_key == first.public_key
    assert keys.rotate(bindings_exist=True).version == 2


# ── Keyring: private material never leaves ───────────────────────────────────────────────


def test_no_private_key_material_in_logs_reprs_or_public_accessors(keys, caplog):
    caplog.set_level(logging.DEBUG)
    exposed: list[str] = []

    exposed.append(repr(keys.active_public_key()))
    exposed.append(repr(keys.rotate()))
    keys.rotate()
    keys.retire(1)
    keys.retire(3)  # the active one — mints a replacement
    listing = keys.public_keys()
    exposed.append(repr(listing))
    exposed.append(json.dumps([k.public() for k in listing]))
    private = keys.signing_key(keys.active_public_key().version)
    exposed += [repr(private), str(private)]
    # The internal structures too: a debug log or traceback of them must stay clean.
    keyring = keys._read(keys._keyring_path())
    exposed += [repr(keyring), str(keyring.keys[0])]

    secrets_on_disk = _private_material(keys)
    assert len(secrets_on_disk) == 2
    haystack = caplog.text + "\n".join(exposed)
    for secret in secrets_on_disk:
        assert secret not in haystack
        assert _decode(secret).hex() not in haystack
        assert str(int.from_bytes(_decode(secret), "big")) not in haystack
    assert "private" not in json.dumps([k.public() for k in listing])
    assert set(listing[0].public()) == {"version", "public_key", "created_at", "active", "retire_after"}
    assert caplog.text  # the lifecycle WAS logged — just without the material


# ── Endpoint validation ──────────────────────────────────────────────────────────────────


@pytest.fixture
def dns(monkeypatch):
    """Route every lookup to a table: host -> addresses (unknown hosts fail to resolve)."""
    table: dict[str, list[str]] = {}

    def fake_getaddrinfo(host, *args, **kwargs):
        addresses = table.get(host, [PUBLIC_IP] if table.get("*") else None)
        if addresses is None:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, 0, 0, "", (a, 0)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    table["*"] = [PUBLIC_IP]
    return table


@pytest.mark.parametrize(
    ("endpoint", "provider"),
    [
        ("https://web.push.apple.com/QGuLtB3nTJs0fJ7xyz_-AbC", "apple"),
        ("https://api.push.apple.com/3/device/abc123", "apple"),
        ("https://fcm.googleapis.com/fcm/send/dA1b2:APA91bF-x_y", "google"),
        ("https://fcm.googleapis.com/wp/dA1b2:APA91bF-x_y", "google"),
        ("https://jmt17.google.com/fcm/send/dA1b2:APA91bF", "google"),
        ("https://updates.push.services.mozilla.com/wpush/v2/gAAAAABl-_x", "mozilla"),
    ],
)
def test_accepts_a_well_formed_endpoint_on_each_supported_service(keys, dns, endpoint, provider):
    from urllib.parse import urlsplit

    assert keys.validate_push_endpoint(endpoint) == endpoint
    assert keys.push_provider(urlsplit(endpoint).hostname) == provider


def test_returns_a_canonical_endpoint(keys, dns):
    dns["fcm.googleapis.com"] = [PUBLIC_IP]

    assert (
        keys.validate_push_endpoint("HTTPS://FCM.GoogleAPIs.com:443/fcm/send/AbC?x=1")
        == "https://fcm.googleapis.com/fcm/send/AbC?x=1"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",  # not https
        "ftp://fcm.googleapis.com/fcm/send/abc",
        "//fcm.googleapis.com/fcm/send/abc",
        "fcm.googleapis.com/fcm/send/abc",
        "https://user@fcm.googleapis.com/fcm/send/abc",  # userinfo
        "https://user:pw@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com@evil.example/fcm/send/abc",
        "https://@fcm.googleapis.com/fcm/send/abc",
        "https://evil.example/fcm/send/abc",  # unsupported hosts
        "https://fcm.googleapis.com.evil.example/fcm/send/abc",
        "https://evilfcm.googleapis.com/fcm/send/abc",
        "https://notfcm.googleapis.com/fcm/send/abc",
        "https://googleapis.com/fcm/send/abc",
        "https://www.google.com/fcm/send/abc",
        "https://evilpush.apple.com/abc",
        "https://apple.com/abc",
        "https://mozilla.com/wpush/v2/abc",
        "https://wns2-by3p.notify.windows.com/w/?token=abc",  # a real service, not yet supported
        "https://127.0.0.1/fcm/send/abc",
        "https://[::1]/fcm/send/abc",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",  # non-default port
        "https://fcm.googleapis.com:notaport/fcm/send/abc",
        "https://fcm.googleapis.com./fcm/send/abc",
        "https://fcm.googleapis.com",  # no subscription path
        "https://fcm.googleapis.com/",
        "https://fcm.googleapis.com/fcm/send/abc#frag",
        "https://fcm.googleapis.com/fcm/send/abc#",
        "https://fcm.googleapis.com/fcm/ send/abc",  # ambiguous bytes
        "https://fcm.googleapis.com/fcm/send/abc\n",
        "https://fcm.googleapis.com\t/fcm/send/abc",
        "https://fcm.googleapis.com\\@evil.example/abc",
        "https://fcm.googleapis.com/fcm/send/é",
        "https://[fcm.googleapis.com/fcm/send/abc",
        "",
        None,
        b"https://fcm.googleapis.com/fcm/send/abc",
        ["https://fcm.googleapis.com/fcm/send/abc"],
    ],
)
def test_refuses_unsafe_or_unsupported_endpoints(keys, dns, endpoint):
    with pytest.raises(keys.PushEndpointError):
        keys.validate_push_endpoint(endpoint)


def test_refuses_an_over_long_endpoint(keys, dns):
    base = "https://fcm.googleapis.com/fcm/send/"
    at_limit = base + "a" * (keys.MAX_ENDPOINT_LENGTH - len(base))

    assert keys.validate_push_endpoint(at_limit) == at_limit
    with pytest.raises(keys.PushEndpointError, match="longer"):
        keys.validate_push_endpoint(at_limit + "a")


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",  # private
        "192.168.1.10",
        "172.16.0.1",
        "127.0.0.1",  # loopback
        "::1",
        "169.254.169.254",  # link-local / cloud metadata
        "fe80::1",
        "fd00::1",  # unique-local
        "100.64.0.1",  # shared address space (tailnet)
        "0.0.0.0",
    ],
)
def test_refuses_a_supported_host_that_resolves_inward(keys, dns, address):
    dns["fcm.googleapis.com"] = [address]

    with pytest.raises(keys.PushEndpointError, match="private, loopback or link-local"):
        keys.validate_push_endpoint("https://fcm.googleapis.com/fcm/send/abc")


def test_refuses_when_any_resolved_address_is_internal(keys, dns):
    dns["web.push.apple.com"] = [PUBLIC_IP, "10.1.2.3"]

    with pytest.raises(keys.PushEndpointError):
        keys.validate_push_endpoint("https://web.push.apple.com/abc")


def test_refuses_a_supported_host_that_does_not_resolve(keys, dns):
    dns.pop("*")

    with pytest.raises(keys.PushEndpointError, match="did not resolve"):
        keys.validate_push_endpoint("https://updates.push.services.mozilla.com/wpush/v2/abc")


def test_an_egress_allowlist_does_not_bypass_the_address_check(keys, dns):
    from security import egress

    dns["fcm.googleapis.com"] = ["10.0.0.5"]
    egress.set_allowed_hosts(["fcm.googleapis.com"])
    try:
        with pytest.raises(keys.PushEndpointError):
            keys.validate_push_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    finally:
        egress.set_allowed_hosts([])


def test_a_refusal_never_echoes_the_endpoint(keys, dns):
    secret_path = "fcm/send/SECRET-SUBSCRIPTION-TOKEN"

    for endpoint in (f"http://fcm.googleapis.com/{secret_path}", f"https://evil.example/{secret_path}"):
        with pytest.raises(keys.PushEndpointError) as err:
            keys.validate_push_endpoint(endpoint)
        assert "SECRET" not in str(err.value) and "evil.example" not in str(err.value)
