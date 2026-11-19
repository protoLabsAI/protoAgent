"""Bounded per-device delivery attempts, not a notice store (ADR 0135 W2).

Register ``deliver`` with the notice seam and periodically call ``drain`` (W2b4).
``WebPushProvider`` is the one sender behind ``PushProvider`` (D8): it encrypts a
generic payload for the device (RFC 8291) and signs it with VAPID (RFC 8292).
Only transport metadata is retained: no notice text, target, or binding secrets.
Accepted/terminal rows remain until expiry to suppress replay. A crash during a
send leaves an inflight row quarantined until expiry: service acceptance cannot
be inferred after restart. This is not an exactly-once presentation guarantee.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from graph.plugins.chat_surface import ChatNotice
from infra.paths import instance_paths
from security import devices, public_console, push_keys
from security.devices import PushBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushResult:
    """``failed`` ends this attempt without a retry and WITHOUT retiring the binding."""

    status: Literal["accepted", "retry", "gone", "failed"]
    retry_after: float = 0


class PushProvider(Protocol):
    async def send(self, binding: PushBinding, payload: dict) -> PushResult:
        """Return service acceptance, proven retryable failure, or 404/410 gone.

        Payload expires_at bounds provider TTL (also capped at 24 hours).
        Exceptions/timeouts use bounded retries with the same notice identity.
        Acceptance can be ambiguous; receivers must deduplicate presentation.
        """
        ...


class PushTransport:
    """Durable fanout with atomic claims, usable by concurrent drains/instances.

    ``eligible`` can consult authoritative decision state before each attempt;
    false discards obsolete prompts without persisting a second notice record.
    Queue overflow raises visibly before any part of that fanout is inserted.
    """

    def __init__(
        self, provider: PushProvider, *, clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random, max_attempts: int = 5,
        capacity: int = 10000, concurrency: int = 8, timeout: float = 15,
        eligible: Callable[[str], bool] = lambda notice_id: True,
    ):
        if not 1 <= max_attempts <= 5 or capacity < 1 or concurrency < 1:
            raise ValueError("invalid push delivery bounds")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.provider = provider
        self.clock, self.jitter, self.eligible = clock, jitter, eligible
        self.max_attempts, self.capacity, self.timeout = max_attempts, capacity, timeout
        self._slots = asyncio.Semaphore(concurrency)
        self.path = instance_paths().store("push") / "delivery.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                notice_id TEXT NOT NULL, device_id TEXT NOT NULL, generation INTEGER NOT NULL,
                kind TEXT NOT NULL, importance TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (notice_id, device_id, generation))""")
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (self.clock(),))
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, notice: ChatNotice) -> int:
        """Snapshot enrolled recipients once; repeated enqueue is idempotent per key."""
        if not notice.notice_id or not math.isfinite(notice.expires_at):
            raise ValueError("push requires a notice identity and finite expiry")
        now = self.clock()
        if notice.expires_at <= now:
            return 0
        recipients = []
        for view in devices.list_devices():
            device = devices.get_device(view["id"])
            if device and device.kind == devices.KIND_DEVICE and not device.is_expired(now) and device.push:
                recipients.append(device)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (now,))
            added = 0
            for device in recipients:
                added += db.execute(
                    """INSERT OR IGNORE INTO attempts
                    (notice_id, device_id, generation, kind, importance, created_at, expires_at, next_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (notice.notice_id, device.id, device.push.generation, notice.kind,
                     notice.importance, notice.created_at, notice.expires_at, now),
                ).rowcount
            if db.execute("SELECT count(*) FROM attempts").fetchone()[0] > self.capacity:
                raise OverflowError("push delivery ledger capacity reached")
        return added

    async def deliver(self, notice: ChatNotice) -> None:
        """Notice-adapter entrypoint; enqueue durably before attempting delivery."""
        self.enqueue(notice)
        await self.drain()

    async def drain(self) -> None:
        """Send due rows independently; pending retries survive reconstruction."""
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (self.clock(),))
            rows = db.execute(
                "SELECT * FROM attempts WHERE state = 'pending' AND next_at <= ?", (self.clock(),)
            ).fetchall()
        await asyncio.gather(*(self._send(row) for row in rows))

    async def _send(self, row) -> None:
        async with self._slots:
            key = (row["notice_id"], row["device_id"], row["generation"])
            where = "notice_id = ? AND device_id = ? AND generation = ?"
            now = self.clock()
            device = devices.get_device(row["device_id"])
            if (row["expires_at"] <= now or device is None or device.is_expired(now)
                    or device.kind != devices.KIND_DEVICE or device.push is None
                    or device.push.generation != row["generation"]):
                with self._db() as db:
                    db.execute(f"DELETE FROM attempts WHERE {where}", key)
                return
            try:
                eligible = self.eligible(row["notice_id"])
            except Exception:
                return  # failed authoritative read: defer, never send stale state
            with self._db() as db:
                if not eligible:
                    db.execute(f"UPDATE attempts SET state = 'obsolete' WHERE {where}", key)
                    return
                claimed = db.execute(
                    f"""UPDATE attempts SET state = 'inflight', attempts = attempts + 1
                    WHERE {where} AND state = 'pending' AND next_at <= ? AND attempts < ?
                    AND attempts = ?""",
                    (*key, now, self.max_attempts, row["attempts"]),
                ).rowcount
            if not claimed:
                return
            payload = {
                "version": 1, "notice_id": row["notice_id"], "kind": row["kind"],
                "importance": row["importance"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "title": "protoAgent needs your attention",
                "body": "Open the console to view an update",
                "link": "/app/?notice_id=" + quote(row["notice_id"], safe=""),
            }
            try:
                result = await asyncio.wait_for(self.provider.send(device.push, payload), self.timeout)
            except Exception:
                result = PushResult("retry")
            state, next_at = "failed", now
            if result.status == "gone":
                devices.invalidate_push_binding(device.id, row["generation"])
                state = "gone"
            elif result.status == "accepted":
                state = "accepted"
            elif result.status == "retry" and row["attempts"] + 1 < self.max_attempts:
                delay = min(3600, 30 * 2 ** row["attempts"] * (1 + self.jitter()))
                next_at = self.clock() + max(delay, result.retry_after)
                if math.isfinite(next_at) and next_at < row["expires_at"]:
                    state = "pending"
            with self._db() as db:
                db.execute(f"UPDATE attempts SET state = ?, next_at = ? WHERE {where}", (state, next_at, *key))


# ── The Web Push sender (ADR 0135 W2, "What leaves the machine") ─────────────────────────

PAYLOAD_VERSION = 1
MAX_PAYLOAD_BYTES = 2048  # before encryption
MAX_TTL_SECONDS = 86400
_VAPID_LIFETIME_SECONDS = 12 * 3600  # RFC 8292 allows at most 24 hours
_RECORD_SIZE = 4096  # one aes128gcm record always holds the capped payload
_TITLE = "protoAgent needs your attention"
_BODY_DECISION = "Open the console to view a decision"
_BODY_UPDATE = "Open the console to view an update"
_KINDS = ("notify", "question", "review")
_IMPORTANCES = ("low", "normal", "high")  # also the RFC 8030 Urgency values we use
# Same pattern the console's push worker accepts (apps/web/public/sw.js NOTICE_ID).
_NOTICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def build_push_payload(payload: dict) -> bytes:
    """The versioned message the console's push worker reads (``apps/web/public/sw.js``).

    Rebuilt from the notice's identity and code-assigned fields ONLY: title, body and
    route are fixed here, so whatever else a caller put in ``payload`` (notice text, a
    link, a token) cannot leave the machine. Raises ``ValueError`` on anything else.
    """
    notice_id, kind, importance = payload.get("notice_id"), payload.get("kind"), payload.get("importance")
    if not isinstance(notice_id, str) or not _NOTICE_ID.fullmatch(notice_id):
        raise ValueError("notice id is not an opaque identifier")
    if kind not in _KINDS or importance not in _IMPORTANCES:
        raise ValueError("unknown notice kind or importance")
    created_at, expires_at = float(payload["created_at"]), float(payload["expires_at"])
    if not (math.isfinite(created_at) and math.isfinite(expires_at)):
        raise ValueError("notice times are not finite")
    body = json.dumps({
        "v": PAYLOAD_VERSION, "id": notice_id, "kind": kind, "importance": importance,
        "created_at": created_at, "expires_at": expires_at, "title": _TITLE,
        "body": _BODY_UPDATE if kind == "notify" else _BODY_DECISION,
        "route": "/app/?notice=" + quote(notice_id, safe=""),
    }, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("push payload is over the size cap")
    return body


def encrypt_push_payload(plaintext: bytes, p256dh: str, auth: str) -> bytes:
    """One ``aes128gcm`` record for the subscription's client keys (RFC 8291 §3.4, RFC 8188)."""
    client_point, auth_secret = _unb64url(p256dh), _unb64url(auth)
    client_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_point)
    sender_key = ec.generate_private_key(ec.SECP256R1())  # fresh per message
    sender_point = _point(sender_key.public_key())
    shared = sender_key.exchange(ec.ECDH(), client_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + client_point + sender_point, 32)
    salt = os.urandom(16)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    header = salt + _RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_point)]) + sender_point
    return header + AESGCM(key).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: last record


def vapid_authorization(key: ec.EllipticCurvePrivateKey, audience: str, subject: str, now: float) -> str:
    """The RFC 8292 ``Authorization`` value: a short-lived ES256 token and its public key."""
    claims = {"aud": audience, "exp": int(now) + _VAPID_LIFETIME_SECONDS}
    if subject:
        claims["sub"] = subject
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in ({"typ": "JWT", "alg": "ES256"}, claims)
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={signing_input}.{signature}, k={_b64url(_point(key.public_key()))}"


def _retry_after(value: str | None, now: float) -> float:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or an HTTP date); 0 if unusable."""
    if not value:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError):
            return 0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0


class WebPushProvider:
    """Hands one notice to one device's push service: the only sender (ADR 0135 D8).

    The service sees the endpoint, TTL/urgency, ciphertext size and the VAPID claims, whose
    subject is this instance's public console origin (omitted when none is declared). It
    never sees the payload. Logs name the provider and status only: never keys, payloads
    or endpoints.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.time, timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.clock, self.timeout, self._transport = clock, timeout, transport

    def _prepare(self, binding: PushBinding, payload: dict) -> tuple[str, dict[str, str], bytes] | PushResult:
        """Validate, encrypt and sign. Blocking (DNS, keyring read): runs off the loop."""
        now = self.clock()
        try:
            plaintext = build_push_payload(payload)
            ttl = int(min(float(payload["expires_at"]) - now, MAX_TTL_SECONDS))
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("[push] notice not sent: %s", exc)
            return PushResult("failed")
        if ttl <= 0:
            return PushResult("failed")  # expired: dropped locally
        try:
            endpoint = push_keys.validate_push_endpoint(binding.endpoint)
        except push_keys.PushEndpointError as exc:
            # Could be a DNS blip, so the binding is kept; the service is never contacted.
            logger.warning("[push] endpoint refused before send: %s", exc)
            return PushResult("retry")
        try:
            key = push_keys.signing_key(binding.vapid_version)
        except push_keys.PushKeyringError as exc:
            logger.error("[push] VAPID keyring unavailable: %s", exc)
            return PushResult("retry")
        if key is None:
            # Retired or out of its migration window: the registry's reconcile clears the
            # binding with its renew reason; this attempt just ends.
            logger.warning("[push] VAPID key version %s is no longer usable", binding.vapid_version)
            return PushResult("failed")
        try:
            body = encrypt_push_payload(plaintext, binding.p256dh, binding.auth)
        except ValueError:
            logger.error("[push] binding client keys cannot be encrypted to")
            return PushResult("failed")
        audience = "https://" + urlsplit(endpoint).netloc
        headers = {
            "Authorization": vapid_authorization(key, audience, public_console.public_origin(), now),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Urgency": payload["importance"],
        }
        return endpoint, headers, body

    async def send(self, binding: PushBinding, payload: dict) -> PushResult:
        prepared = await asyncio.to_thread(self._prepare, binding, payload)
        if isinstance(prepared, PushResult):
            return prepared
        endpoint, headers, body = prepared
        provider = push_keys.push_provider(urlsplit(endpoint).hostname or "")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(endpoint, headers=headers, content=body)
        except httpx.HTTPError as exc:
            logger.warning("[push] %s request failed: %s", provider, type(exc).__name__)
            return PushResult("retry")
        status = response.status_code
        if 200 <= status < 300:
            return PushResult("accepted")
        if status == 429 or status >= 500:
            logger.warning("[push] %s asked us to retry (HTTP %s)", provider, status)
            return PushResult("retry", _retry_after(response.headers.get("Retry-After"), self.clock()))
        if status in (404, 410):
            return PushResult("gone")
        if status in (401, 403) or status < 400:
            # 401/403 is sender-key or provider-policy trouble, not proof the device
            # unsubscribed (ADR 0135 W2); a redirect is never followed. Keep the binding.
            logger.error("[push] %s refused the send (HTTP %s); binding kept", provider, status)
            return PushResult("failed")
        logger.error("[push] %s rejected the subscription (HTTP %s); binding retired", provider, status)
        return PushResult("gone")
