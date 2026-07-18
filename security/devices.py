"""Per-device tokens + QR pairing (ADR 0087).

Auth before this module was a single shared secret (``a2a_impl.auth._BEARER``): every
device presented the same string, so revoking one device meant rotating the secret and
logging out *everything* — which in practice meant nobody revoked anything and a lost phone
stayed authorised.

This adds a registry of named devices, each with its own token, individually revocable. A
device token resolves to the **operator** tier: a paired phone runs the full console and
needs the same surface the desktop does. The split buys identity and revocation, not reduced
capability.

Two halves, deliberately different in durability:

* **The registry** is persisted (``instance_root/devices.json``) and stores only
  ``sha256(token)`` — never the token. A leaked registry cannot be replayed, and there is no
  way to recover a token after issue, so no "show token" affordance can ever be built.
* **Pending pairings** are memory-only. A restart invalidates them, which is the desired
  behavior: a code nobody claimed within its 120s window should not survive anything.

The registry lives at the INSTANCE tier, not ``config_dir`` — config is the tier that gets
seeded/shared between instances, and a device paired to the dev sandbox must never
authenticate against prod.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from infra.paths import instance_paths

logger = logging.getLogger(__name__)

# A pairing code is displayed on screen (usually as a QR) and is claimable by anyone who can
# read it, so its safety comes from being short-lived and single-use rather than secret.
PAIRING_TTL_SECONDS = 120
# 32 url-safe chars ≈ 190 bits. Guessing is not the threat model; screen-visibility is.
_CODE_BYTES = 24
_TOKEN_BYTES = 32
# Consecutive failed claims before every pending code is dropped. A legitimate scanner gets
# the code right first time; repeated misses mean someone is probing.
_MAX_FAILED_CLAIMS = 5


@dataclass
class Device:
    """A paired device. ``token_sha256`` is the only trace of the credential."""

    id: str
    name: str
    token_sha256: str
    created_at: float
    last_seen_at: float | None = None

    def public(self) -> dict:
        """The shape safe to hand the console — everything except the hash."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _registry_path() -> Path:
    return instance_paths().instance_root / "devices.json"


def _load() -> list[Device]:
    path = _registry_path()
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # A corrupt registry must not white-screen auth. Treat it as empty: paired devices
        # stop working (they re-pair) but the shared bearer still gets the operator in.
        logger.warning("[devices] registry unreadable at %s — treating as empty", path)
        return []
    out: list[Device] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(
                Device(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    token_sha256=str(item["token_sha256"]),
                    created_at=float(item["created_at"]),
                    last_seen_at=(float(item["last_seen_at"]) if item.get("last_seen_at") else None),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # skip a hand-edited/partial entry rather than failing the whole load
    return out


def _save(devices: list[Device]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([asdict(d) for d in devices], indent=2), "utf-8")
    # 0600 before the rename: hashes aren't replayable, but the device list is still a map of
    # who can reach this instance.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic — a crash mid-write can't truncate the registry


def list_devices() -> list[dict]:
    return [d.public() for d in _load()]


def revoke_device(device_id: str) -> bool:
    """Drop a device. Its token stops authenticating on the next request."""
    devices = _load()
    remaining = [d for d in devices if d.id != device_id]
    if len(remaining) == len(devices):
        return False
    _save(remaining)
    logger.info("[devices] revoked %s", device_id)
    return True


def verify_token(token: str) -> Device | None:
    """Return the device this token belongs to, else None.

    Compared by hash, so the registry never holds anything replayable. ``compare_digest`` on
    each candidate keeps the comparison constant-time; a plain dict lookup would leak
    nothing useful here (the input is already hashed) but the explicit compare keeps the
    intent obvious next to the rest of the auth code.
    """
    if not token:
        return None
    digest = _hash(token)
    for device in _load():
        if hmac.compare_digest(device.token_sha256, digest):
            _touch(device.id)
            return device
    return None


# Writing on every request would be a disk write per API call, so last-seen is coarse.
_LAST_SEEN_THROTTLE_SECONDS = 300


def _touch(device_id: str) -> None:
    devices = _load()
    now = time.time()
    for device in devices:
        if device.id != device_id:
            continue
        if device.last_seen_at and now - device.last_seen_at < _LAST_SEEN_THROTTLE_SECONDS:
            return  # recently recorded — skip the write
        device.last_seen_at = now
        try:
            _save(devices)
        except OSError:
            logger.debug("[devices] could not record last-seen for %s", device_id)
        return


def _register(name: str) -> tuple[Device, str]:
    """Mint a device + its token. The token is returned ONCE and never stored."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    device = Device(
        id=secrets.token_hex(8),
        name=(name or "").strip()[:64] or "Unnamed device",
        token_sha256=_hash(token),
        created_at=time.time(),
    )
    devices = _load()
    devices.append(device)
    _save(devices)
    logger.info("[devices] paired %s (%s)", device.name, device.id)
    return device, token


# ── Pending pairings (memory-only, see the module docstring) ────────────────────────────
# code -> expiry timestamp.
_PENDING: dict[str, float] = {}
_failed_claims = [0]


def _prune(now: float) -> None:
    for code, expires in list(_PENDING.items()):
        if expires <= now:
            del _PENDING[code]


def start_pairing() -> tuple[str, float]:
    """Mint a pairing code. Operator-authed callers only (enforced at the route)."""
    now = time.time()
    _prune(now)
    code = secrets.token_urlsafe(_CODE_BYTES)
    expires_at = now + PAIRING_TTL_SECONDS
    _PENDING[code] = expires_at
    return code, expires_at


def cancel_pairings() -> None:
    """Drop every pending code — e.g. the operator closed the Add-device dialog."""
    _PENDING.clear()


def claim_pairing(code: str, device_name: str) -> tuple[dict, str] | None:
    """Redeem a code for a fresh device token, or None if it isn't valid.

    Single-use: the code is removed before the device is created, so two racing claims
    cannot both succeed. Repeated failures drop every pending code rather than allowing
    indefinite probing of an open endpoint (this is reachable unauthenticated by necessity —
    ADR 0087 D4).
    """
    now = time.time()
    _prune(now)
    if not code or not _PENDING:
        return None

    matched: str | None = None
    for pending in _PENDING:
        if hmac.compare_digest(pending, code):
            matched = pending
            break
    if matched is None:
        _failed_claims[0] += 1
        if _failed_claims[0] >= _MAX_FAILED_CLAIMS:
            logger.warning("[devices] %d failed pairing claims — dropping pending codes", _failed_claims[0])
            _PENDING.clear()
            _failed_claims[0] = 0
        return None

    del _PENDING[matched]  # consume BEFORE minting, so a race can't double-issue
    _failed_claims[0] = 0
    device, token = _register(device_name)
    return device.public(), token
