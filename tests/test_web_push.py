"""The Web Push sender against a fake push service (ADR 0135 W2, "What leaves the machine").

The fake service is an ``httpx.MockTransport``; DNS goes through ``socket.getaddrinfo``
(stubbed). The test decrypts with its own RFC 8291 receiver, as a browser would.
"""
import asyncio
import base64
import json
import os
import socket

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from graph.push_transport import PushResult, WebPushProvider
from infra.paths import reset_instance_paths
from security import public_console, push_keys
from security.devices import PushBinding

pytestmark = pytest.mark.platform_sensitive  # reads the owner-only VAPID keyring

NOW = 1_000_000.0
ENDPOINT = "https://fcm.googleapis.com/fcm/send/subscription-capability"
ORIGIN = "https://console.example.com"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


class Browser:
    """A subscribed browser: holds the client keys and decrypts what the service relays."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.point = self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)

    def binding(self, endpoint=ENDPOINT, version=1):
        return PushBinding(endpoint, b64(self.point), b64(self.auth), version, 1, NOW, NOW)

    def decrypt(self, body: bytes) -> dict:
        salt, size, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        sender, record = body[21:21 + idlen], body[21 + idlen:]
        assert len(record) <= size
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender))
        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.point + sender, 32)
        key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(key).decrypt(nonce, record, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class Service:
    """The fake push service: records requests and answers with ``reply``."""

    def __init__(self, reply=lambda request: httpx.Response(201)):
        self.requests, self.reply = [], reply

    def __call__(self, request):
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    monkeypatch.setattr(public_console, "_ORIGIN", [ORIGIN])
    resolved = {"address": "142.250.0.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(0, 0, 0, "", (resolved["address"], 0))])
    public = push_keys.active_public_key()
    yield public, resolved
    reset_instance_paths()


def payload(**over):
    """What ``PushTransport`` hands a provider, plus operator text it must never forward."""
    base = {
        "version": 1, "notice_id": "notice-1", "kind": "question", "importance": "high",
        "created_at": NOW - 5, "expires_at": NOW + 600, "title": "Deploy prod-db now?",
        "body": "token sk-live-secret", "link": "/private/repo/path", "text": "Deploy prod-db now?",
    }
    return {**base, **over}


def send(service, binding, message=None):
    provider = WebPushProvider(clock=lambda: NOW, transport=httpx.MockTransport(service))
    return asyncio.run(provider.send(binding, message or payload()))


def test_send_is_encrypted_for_the_device_and_signed_with_the_instance_key(env):
    public, _ = env
    browser, service = Browser(), Service()

    assert send(service, browser.binding()) == PushResult("accepted")

    (request,) = service.requests
    assert str(request.url) == ENDPOINT and request.method == "POST"
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["TTL"] == "600" and request.headers["Urgency"] == "high"
    assert browser.decrypt(request.content) == {
        "v": 1, "id": "notice-1", "kind": "question", "importance": "high",
        "created_at": NOW - 5, "expires_at": NOW + 600, "title": "protoAgent needs your attention",
        "body": "Open the console to view a decision", "route": "/app/?notice=notice-1",
    }

    scheme, _, params = request.headers["Authorization"].partition(" ")
    fields = dict(part.strip().split("=", 1) for part in params.split(","))
    assert scheme == "vapid" and fields["k"] == public.public_key
    signed, _, signature = fields["t"].rpartition(".")
    raw = unb64(signature)
    instance_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(public.public_key))
    instance_key.verify(  # raises InvalidSignature unless the instance key signed it
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        signed.encode(), ec.ECDSA(hashes.SHA256()),
    )
    header, claims = (json.loads(unb64(part)) for part in signed.split("."))
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims == {"aud": "https://fcm.googleapis.com", "exp": int(NOW) + 12 * 3600, "sub": ORIGIN}


def test_payload_carries_no_operator_text_or_credentials(env):
    browser, service = Browser(), Service()
    binding = browser.binding()

    send(service, binding, payload(kind="notify", importance="low", expires_at=NOW + 10 * 86400))

    (request,) = service.requests
    sent = browser.decrypt(request.content)
    text = json.dumps(sent)
    for private in ("Deploy", "prod-db", "sk-live-secret", "/private", binding.auth, binding.p256dh, ENDPOINT):
        assert private not in text
    assert set(sent) == {"v", "id", "kind", "importance", "created_at", "expires_at", "title", "body", "route"}
    assert sent["body"] == "Open the console to view an update"
    assert request.headers["TTL"] == "86400" and request.headers["Urgency"] == "low"  # capped at 24h
    assert b"notice-1" not in request.content  # ciphertext only on the wire


@pytest.mark.parametrize("status, headers, expected", [
    (201, {}, PushResult("accepted")),
    (202, {}, PushResult("accepted")),
    (404, {}, PushResult("gone")),
    (410, {}, PushResult("gone")),
    (400, {}, PushResult("gone")),
    (429, {"Retry-After": "120"}, PushResult("retry", 120)),
    (503, {"Retry-After": "Mon, 12 Jan 1970 13:48:40 GMT"}, PushResult("retry", 120)),
    (503, {"Retry-After": "soon"}, PushResult("retry")),
    (403, {}, PushResult("failed")),
])
def test_service_responses_map_to_results(env, status, headers, expected):
    service = Service(lambda request: httpx.Response(status, headers=headers))
    assert send(service, Browser().binding()) == expected


def test_timeout_is_a_retry(env):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert send(Service(timeout), Browser().binding()) == PushResult("retry", error="timeout")


def test_redirect_is_not_followed(env):
    service = Service(lambda request: httpx.Response(307, headers={"Location": "https://fcm.googleapis.com/other"}))
    assert send(service, Browser().binding()) == PushResult("failed")
    assert len(service.requests) == 1


@pytest.mark.parametrize("endpoint, address", [
    ("https://push.example.com/subscription", "142.250.0.1"),  # unsupported host
    ("http://fcm.googleapis.com/fcm/send/x", "142.250.0.1"),  # not https
    (ENDPOINT, "10.0.0.7"),  # supported name now resolving inward
])
def test_endpoint_failing_validation_is_not_contacted(env, endpoint, address):
    _, resolved = env
    resolved["address"] = address
    service = Service()
    assert send(service, Browser().binding(endpoint)) == PushResult("retry", error="network")
    assert service.requests == []


def test_unsendable_notices_never_reach_the_service(env):
    service, browser = Service(), Browser()
    assert send(service, browser.binding(), payload(expires_at=NOW)) == PushResult("failed", error="retries_exhausted")  # expired
    assert send(service, browser.binding(), payload(notice_id="free text id")) == PushResult("failed")
    assert send(service, browser.binding(version=99)) == PushResult("failed")  # retired VAPID key
    assert service.requests == []
