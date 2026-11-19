"""Web Push security primitives (ADR 0135 W2): the per-instance VAPID keyring and push
endpoint validation.

Two halves, both consumed by later W2 cards (enrollment serves the active public key and
validates endpoints; the sender signs VAPID tokens with the key a binding was enrolled
under; the device registry asks `usable_versions` which bindings a rotation or retirement
has stranded). Nothing here sends a push, reads config or touches the device registry.

**The keyring.** Web Push authenticates the SENDER with VAPID (RFC 8292): an ES256 token
signed by a P-256 key whose public half the browser was given when it subscribed. That key
is this instance's push identity, so it is generated here, from OS randomness, on first use
— never shipped, shared between instances or read from config. It lives in one versioned
file under ``instance_paths().store("push")``, written atomically with owner-only
permissions (0600 / the Windows ACL equivalent, ``infra.paths.atomic_write``).

A subscription is bound to the public key it was created with: it cannot simply be sent to
with a newer key. So the keyring keeps VERSIONS. `rotate` activates a new one and keeps the
old signing key for `ROTATION_WINDOW_SECONDS` while devices resubscribe; `retire` is the
compromise path and removes a key immediately. Version numbers are never reused, so a
binding's stored version can only ever mean the key it was enrolled under.

Because existing subscriptions die with the key, a keyring that is corrupt — or missing
while bindings exist — raises `PushKeyringError`. It is never silently regenerated: that
would look healthy while every enrolled device had quietly stopped receiving.

Private keys never leave this module as data. `signing_key` hands back the library's key
OBJECT (whose repr carries no material); every dict, dataclass, repr and log line here is
public halves and version numbers only.

**Endpoint validation.** A push endpoint is a URL the browser hands us and we later POST
to: untrusted outbound. `validate_push_endpoint` admits only HTTPS URLs on the explicitly
supported Apple, Google and Mozilla push hosts, so enrollment cannot become SSRF or an open
relay. A new provider needs a host entry here and a test.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from filelock import FileLock

from infra.paths import atomic_write, harden_private_file, instance_paths
from security import egress

logger = logging.getLogger(__name__)

_SCHEMA = 1
_KEYRING_NAME = "vapid-keyring.json"
_LOCK_NAME = "vapid-keyring.lock"
_LOCK_TIMEOUT_SECONDS = 30

# How long a rotated-out signing key stays usable so already-enrolled devices can
# resubscribe under the new public key on their next visit (ADR 0135 W2, "Rotation").
ROTATION_WINDOW_SECONDS = 30 * 86400

_P256_SCALAR_BYTES = 32
_P256_POINT_BYTES = 65  # uncompressed: 0x04 || X || Y

# Guards read-modify-write inside this process; the file lock beside the keyring does the
# same across processes (two servers on one instance must not each mint "the" first key).
_LOCK = threading.RLock()


class PushKeyringError(RuntimeError):
    """The keyring is corrupt, unreadable, or missing when it must exist.

    Deliberately not recovered from automatically: a fresh key would orphan every existing
    subscription. The message names the file and the way out; it never quotes the file.
    """


def _now() -> float:
    """The keyring's clock — one seam for tests to move time."""
    return time.time()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class PushPublicKey:
    """One keyring version's public half — everything that is safe to hand out.

    ``public_key`` is the unpadded base64url uncompressed P-256 point: exactly the
    ``applicationServerKey`` a browser passes to ``PushManager.subscribe``.
    ``retire_after`` is set once the version has been rotated out: the moment its signing
    key stops being served (``None`` for the active version).
    """

    version: int
    public_key: str
    created_at: float
    active: bool
    retire_after: float | None = None

    def public(self) -> dict:
        return {
            "version": self.version,
            "public_key": self.public_key,
            "created_at": self.created_at,
            "active": self.active,
            "retire_after": self.retire_after,
        }


@dataclass
class _Key:
    """A keyring entry. ``private_key`` is excluded from the repr so an accidental
    ``logger.debug("%r", key)`` or a traceback's locals cannot print it."""

    version: int
    public_key: str
    created_at: float
    private_key: str = field(repr=False)
    retire_after: float | None = None

    def expired(self, now: float) -> bool:
        return self.retire_after is not None and now >= self.retire_after


@dataclass
class _Keyring:
    active: int
    last_version: int
    keys: list[_Key]

    def get(self, version: int) -> _Key | None:
        for key in self.keys:
            if key.version == version:
                return key
        return None


def _keyring_path() -> Path:
    return instance_paths().store("push") / _KEYRING_NAME


def _file_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return FileLock(str(path.parent / _LOCK_NAME), timeout=_LOCK_TIMEOUT_SECONDS)


def _generate(version: int, now: float) -> _Key:
    # OpenSSL's CSPRNG, seeded from the OS entropy source — the same source `secrets` uses.
    private = ec.generate_private_key(ec.SECP256R1())
    scalar = private.private_numbers().private_value.to_bytes(_P256_SCALAR_BYTES, "big")
    point = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _Key(version=version, public_key=_b64url(point), created_at=now, private_key=_b64url(scalar))


def _private_key(key: _Key) -> ec.EllipticCurvePrivateKey:
    scalar = _unb64url(key.private_key)
    if len(scalar) != _P256_SCALAR_BYTES:
        raise ValueError("bad private key length")
    return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())


def _corrupt(path: Path, why: str) -> PushKeyringError:
    return PushKeyringError(
        f"The push keyring at {path} is {why}. It was NOT regenerated: a new key would "
        "silently break every device's existing push subscription. Restore the file from a "
        "backup, or delete it to start over — every device must then enable notifications again."
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse(path: Path, text: str) -> _Keyring:
    """Parse and VERIFY the keyring: structure, version bookkeeping, and that every stored
    public key is the one its private key derives. Any doubt is corruption — a keyring we
    half-trust could sign with a key no subscription was created against."""
    try:
        raw = json.loads(text)
    except ValueError:
        raise _corrupt(path, "not valid JSON") from None
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        raise _corrupt(path, "not a keyring this version understands")
    active, last, items = raw.get("active"), raw.get("last_version"), raw.get("keys")
    if not _is_int(active) or not _is_int(last) or not isinstance(items, list) or not items:
        raise _corrupt(path, "missing its version bookkeeping or its keys")
    keys: list[_Key] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            raise _corrupt(path, "holding a malformed key entry")
        version, public, private = item.get("version"), item.get("public_key"), item.get("private_key")
        created, retire_after = item.get("created_at"), item.get("retire_after")
        if (
            not _is_int(version)
            or version < 1
            or version > last
            or version in seen
            or not isinstance(public, str)
            or not isinstance(private, str)
            or not _is_number(created)
            or not (retire_after is None or _is_number(retire_after))
        ):
            raise _corrupt(path, "holding a malformed key entry")
        seen.add(version)
        key = _Key(
            version=version,
            public_key=public,
            created_at=float(created),
            private_key=private,
            retire_after=None if retire_after is None else float(retire_after),
        )
        try:
            point = _private_key(key).public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        except Exception:  # noqa: BLE001 — any decode/curve failure is the same verdict
            raise _corrupt(path, f"holding an unusable key (version {version})") from None
        if len(point) != _P256_POINT_BYTES or _b64url(point) != public:
            raise _corrupt(path, f"holding a key whose halves do not match (version {version})")
        keys.append(key)
    active_key = next((k for k in keys if k.version == active), None)
    if active_key is None or active_key.retire_after is not None:
        raise _corrupt(path, "without a usable active key")
    return _Keyring(active=active, last_version=last, keys=keys)


def _read(path: Path) -> _Keyring | None:
    """The keyring on disk, ``None`` if there is no file. Raises on anything else."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise _corrupt(path, f"unreadable ({type(exc).__name__})") from None
    keyring = _parse(path, text)
    _reharden(path)
    return keyring


def _reharden(path: Path) -> None:
    """A keyring restored from a backup or copied by hand can land group/world-readable.
    Tighten it rather than keep serving a signing key anyone on the box can read."""
    if os.name == "nt":
        return  # mode bits are decorative there; the ACL is set when the file is written
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            harden_private_file(path)
            logger.warning("[push] keyring at %s was readable by others — reset to owner-only", path)
    except OSError:
        logger.warning("[push] could not check permissions on the keyring at %s", path)


def _write(path: Path, keyring: _Keyring) -> None:
    payload = {
        "schema": _SCHEMA,
        "active": keyring.active,
        "last_version": keyring.last_version,
        "keys": [
            {
                "version": k.version,
                "public_key": k.public_key,
                "private_key": k.private_key,
                "created_at": k.created_at,
                "retire_after": k.retire_after,
            }
            for k in sorted(keyring.keys, key=lambda k: k.version)
        ],
    }
    # 0600 on the temp file before the rename (and the Windows ACL after it): the final
    # path never exists with looser permissions, and a crash mid-write can't truncate it.
    atomic_write(path, json.dumps(payload, indent=2), mode=0o600)


def _lost(path: Path) -> PushKeyringError:
    return PushKeyringError(
        f"The push keyring at {path} is missing, but push subscriptions signed by it exist. "
        "It was NOT regenerated: a new key would silently break them. Restore the file from "
        "a backup, or disable push on the affected devices and have them enable it again."
    )


def _load_or_create(path: Path, *, bindings_exist: bool) -> _Keyring:
    """Caller holds both locks. First use mints version 1 — unless bindings already exist,
    in which case an absent keyring is a LOST one."""
    keyring = _read(path)
    if keyring is not None:
        return keyring
    if bindings_exist:
        raise _lost(path)
    keyring = _Keyring(active=1, last_version=1, keys=[_generate(1, _now())])
    _write(path, keyring)
    logger.info("[push] generated this instance's VAPID keyring (version 1)")
    return keyring


def _require(path: Path) -> _Keyring:
    keyring = _read(path)
    if keyring is None:
        raise _lost(path)
    return keyring


def _public(keyring: _Keyring, key: _Key) -> PushPublicKey:
    return PushPublicKey(
        version=key.version,
        public_key=key.public_key,
        created_at=key.created_at,
        active=key.version == keyring.active,
        retire_after=key.retire_after,
    )


def active_public_key(*, bindings_exist: bool = False) -> PushPublicKey:
    """The active VAPID public key and its version, generating the keyring on first use.

    ``bindings_exist`` is the caller's knowledge that some device already holds a push
    binding. With it set, a missing keyring is a lost one and raises `PushKeyringError`
    instead of minting a replacement. A corrupt keyring raises either way.
    """
    path = _keyring_path()
    with _LOCK, _file_lock(path):
        keyring = _load_or_create(path, bindings_exist=bindings_exist)
        key = keyring.get(keyring.active)
        assert key is not None  # `_parse` / `_load_or_create` guarantee an active key
        return _public(keyring, key)


def public_keys() -> list[PushPublicKey]:
    """Every version still in the keyring, oldest first — public halves only. Empty when
    push has never been enabled on this instance. Never creates a keyring."""
    path = _keyring_path()
    with _LOCK:
        keyring = _read(path)
    if keyring is None:
        return []
    return [_public(keyring, k) for k in sorted(keyring.keys, key=lambda k: k.version)]


def signing_key(version: int) -> ec.EllipticCurvePrivateKey | None:
    """The signing key a binding enrolled under ``version`` must be sent with.

    ``None`` when that version has been retired or its migration window has closed — the
    binding needs renewing on the device. Raises `PushKeyringError` when there is no keyring
    at all: a caller asking for a version only does so because a binding names it.

    The return value is the crypto library's key object, for signing only. Do not serialize
    or log anything derived from it.
    """
    path = _keyring_path()
    with _LOCK:
        keyring = _require(path)
    key = keyring.get(version) if _is_int(version) else None
    if key is None or key.expired(_now()):
        return None
    return _private_key(key)


def rotate(*, bindings_exist: bool = False) -> PushPublicKey:
    """Activate a new key version; keep the previous one signing for the migration window.

    The outgoing version stays retrievable through `signing_key` for
    `ROTATION_WINDOW_SECONDS`, so devices that have not resubscribed yet keep receiving.
    Versions whose window has already closed are deleted here. Returns the new active key.
    """
    path = _keyring_path()
    with _LOCK, _file_lock(path):
        keyring = _load_or_create(path, bindings_exist=bindings_exist)
        now = _now()
        previous = keyring.active
        for key in keyring.keys:
            if key.version == previous:
                key.retire_after = now + ROTATION_WINDOW_SECONDS
        fresh = _generate(keyring.last_version + 1, now)
        keyring.keys = [k for k in keyring.keys if not k.expired(now)] + [fresh]
        keyring.active = keyring.last_version = fresh.version
        _write(path, keyring)
        logger.info("[push] rotated the VAPID key: version %d is active, %d retires later", fresh.version, previous)
        return _public(keyring, fresh)


def retire(version: int) -> bool:
    """Remove a key version NOW — the compromise path. Returns whether it was present.

    No migration window: its signing key is gone as soon as this returns, so every binding
    enrolled under it needs renewing. Retiring the ACTIVE version activates a freshly
    generated one in the same write, so the keyring never lacks an active key.
    """
    path = _keyring_path()
    with _LOCK, _file_lock(path):
        keyring = _require(path)
        if keyring.get(version) is None:
            return False
        keyring.keys = [k for k in keyring.keys if k.version != version]
        if version == keyring.active:
            fresh = _generate(keyring.last_version + 1, _now())
            keyring.keys.append(fresh)
            keyring.active = keyring.last_version = fresh.version
        _write(path, keyring)
        logger.warning("[push] retired VAPID key version %d; version %d is active", version, keyring.active)
        return True


def prune_expired() -> list[int]:
    """Delete versions whose migration window has closed; return the versions removed.

    `signing_key` already refuses them — this drops the material from disk. Bindings still
    naming a returned version are the ones to mark "Renew on this device".
    """
    path = _keyring_path()
    with _LOCK, _file_lock(path):
        keyring = _read(path)
        if keyring is None:
            return []
        now = _now()
        gone = sorted(k.version for k in keyring.keys if k.expired(now))
        if gone:
            keyring.keys = [k for k in keyring.keys if not k.expired(now)]
            _write(path, keyring)
            logger.info("[push] deleted expired VAPID key versions %s", gone)
        return gone


# ── Endpoint validation ──────────────────────────────────────────────────────────────────

# Real endpoints run 150–600 characters. The bound keeps a hostile enrollment from parking
# kilobytes in the device registry.
MAX_ENDPOINT_LENGTH = 2048

# The push services a browser may hand us, by provider. ``exact`` hosts match only
# themselves; a ``suffix`` matches the apex and any subdomain of a zone the provider
# controls end to end (never a shared or registrable-by-others zone).
#   Apple   — Safari and iOS/iPadOS Home Screen web apps; Apple documents the whole
#             ``*.push.apple.com`` zone as the one to allow.
#   Google  — Chrome and other FCM-backed browsers.
#   Mozilla — Firefox's autopush.
# Anything else (including other Chromium vendors' own services) is refused until it is
# added here with a test.
_PUSH_HOSTS: dict[str, dict[str, tuple[str, ...]]] = {
    "apple": {"exact": (), "suffix": ("push.apple.com",)},
    "google": {"exact": ("fcm.googleapis.com", "jmt17.google.com"), "suffix": ()},
    "mozilla": {"exact": (), "suffix": ("push.services.mozilla.com",)},
}

_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


class PushEndpointError(ValueError):
    """A push endpoint was refused. The message says why and never echoes the endpoint —
    an endpoint is a capability URL and must not reach logs through an error string."""


def push_provider(host: str) -> str | None:
    """Which supported push service ``host`` belongs to, or ``None``."""
    host = (host or "").lower()
    for provider, rules in _PUSH_HOSTS.items():
        if host in rules["exact"]:
            return provider
        if any(host == suffix or host.endswith("." + suffix) for suffix in rules["suffix"]):
            return provider
    return None


def validate_push_endpoint(url: object) -> str:
    """Return the canonical form of a push endpoint, or raise `PushEndpointError`.

    Accepted: an ``https`` URL, at most `MAX_ENDPOINT_LENGTH` characters, on a supported
    Apple, Google or Mozilla push host, on the default port, with a path, without userinfo
    or a fragment, whose host resolves only to public addresses.

    The resolution check is one-shot, and resolves through the system resolver (it blocks —
    call it off the event loop). The sender must validate again right before each request
    and must not follow redirects; this check is what stops a bad endpoint being STORED.
    """
    if not isinstance(url, str) or not url:
        raise PushEndpointError("push endpoint must be a non-empty string")
    if len(url) > MAX_ENDPOINT_LENGTH:
        raise PushEndpointError(f"push endpoint is longer than {MAX_ENDPOINT_LENGTH} characters")
    # Before parsing: urlsplit silently strips tabs/newlines and tolerates other bytes that a
    # different HTTP client may read differently — one unambiguous reading or none.
    if not url.isascii() or any(ch <= " " or ch == "\x7f" or ch == "\\" for ch in url):
        raise PushEndpointError("push endpoint contains whitespace, control or non-ASCII characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
    except ValueError:
        raise PushEndpointError("push endpoint is not a well-formed URL") from None
    if parts.scheme.lower() != "https":
        raise PushEndpointError("push endpoint must use https")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise PushEndpointError("push endpoint must not carry credentials")
    if port not in (None, 443):
        raise PushEndpointError("push endpoint must use the default https port")
    host = host.lower()
    if not host or not set(host) <= _HOST_CHARS or host.startswith(".") or host.endswith(".") or ".." in host:
        raise PushEndpointError("push endpoint has no valid host")
    if push_provider(host) is None:
        raise PushEndpointError("push endpoint is not on a supported push service")
    if parts.fragment or "#" in url:
        raise PushEndpointError("push endpoint must not carry a fragment")
    if not parts.path or parts.path == "/":
        raise PushEndpointError("push endpoint has no subscription path")
    # Deliberately NOT `egress.check_url`: an operator's egress allowlist bypasses the
    # address check there, and an allowlisted name must still never resolve inward here.
    blocked = egress._blocked_ip(host)
    if blocked == "unresolvable":
        raise PushEndpointError("push endpoint host did not resolve")
    if blocked:
        raise PushEndpointError("push endpoint host resolves to a private, loopback or link-local address")
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{parts.path}{query}"


# ── Enrollment checks ────────────────────────────────────────────────────────────────────

_AUTH_SECRET_BYTES = 16
_B64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class PushSubscriptionError(ValueError):
    """A subscription's client keys or key version were refused. The message says why and
    never echoes what was sent — the ``auth`` secret must not reach logs through an error."""


def _decode_client_key(value: object, name: str, size: int) -> tuple[str, bytes]:
    if not isinstance(value, str) or not value:
        raise PushSubscriptionError(f"keys.{name} must be a non-empty string")
    text = value.rstrip("=")  # browsers send it unpadded; tolerate a padded copy
    # Bounded before decoding, and one alphabet only: `urlsafe_b64decode` silently skips
    # characters outside it, which would let two different strings name one key.
    if len(text) > 2 * size or not set(text) <= _B64URL_CHARS:
        raise PushSubscriptionError(f"keys.{name} is not base64url")
    try:
        raw = _unb64url(text)
    except ValueError:
        raise PushSubscriptionError(f"keys.{name} is not base64url") from None
    if len(raw) != size:
        raise PushSubscriptionError(f"keys.{name} has the wrong length")
    return _b64url(raw), raw


def validate_subscription_keys(p256dh: object, auth: object) -> tuple[str, str]:
    """Return a browser subscription's client keys in canonical (unpadded base64url) form,
    or raise `PushSubscriptionError`.

    ``p256dh`` must be an uncompressed P-256 point that is actually on the curve — the key
    the payload is encrypted to (RFC 8291) — and ``auth`` the 16-byte authentication secret.
    Checked at enrollment so a binding that could never be encrypted to is not stored.
    """
    point_text, point = _decode_client_key(p256dh, "p256dh", _P256_POINT_BYTES)
    auth_text, _secret = _decode_client_key(auth, "auth", _AUTH_SECRET_BYTES)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise PushSubscriptionError("keys.p256dh is not a valid P-256 public key") from None
    return point_text, auth_text


def usable_versions() -> frozenset[int] | None:
    """The key versions a binding can still be sent with: the active one and any rotated-out
    one still inside its migration window. A binding naming any OTHER version was enrolled
    under a key that was retired or has left its window — it can never be sent to again and
    the device must enroll anew (``security.devices.reconcile_push_bindings``).

    ``None`` when there is no keyring: with bindings present that is a LOST keyring, which
    must not read as "every version is gone". Never creates a keyring; a corrupt one raises
    `PushKeyringError`.
    """
    path = _keyring_path()
    with _LOCK:
        keyring = _read(path)
    if keyring is None:
        return None
    now = _now()
    return frozenset(k.version for k in keyring.keys if not k.expired(now))


def enrollable_version(version: object) -> bool:
    """Whether a binding may be stored against keyring ``version``: it is the active key, or
    a rotated-out one still inside its migration window. Never creates a keyring; a corrupt
    one raises `PushKeyringError`."""
    if not _is_int(version):
        return False
    now = _now()
    return any(k.version == version and (k.retire_after is None or now < k.retire_after) for k in public_keys())
