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
  behavior: a code nobody claimed within its window should not survive anything.

Two KINDS of pending code share that one store and its one failed-claim counter (ADR 0113
D2): a **device** code (a phone scans it off a QR — long, url-safe, 120s) and an **agent**
code (read off one screen and typed into another machine's "Pair…" dialog — 10 Crockford
base32 chars, 300s). The kind is fixed when the code is MINTED and copied onto the device it
yields, so a claimer can never relabel what it is.

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
import threading
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
# the code right first time; repeated misses mean someone is probing. SHARED across code
# kinds: a separate agent-code counter would hand a prober 5 more guesses per kind.
_MAX_FAILED_CLAIMS = 5

KIND_DEVICE = "device"
KIND_AGENT = "agent"
_KINDS = frozenset({KIND_DEVICE, KIND_AGENT})

# Agent codes (ADR 0113 D2) are TYPED, not scanned: short, unambiguous, and a longer window
# because reading a code off one machine and typing it into another takes longer than a scan.
# 10 chars of Crockford base32 = 50 bits; with the shared 5-miss lockout a prober's odds per
# issued code are ~5/2^50. Crockford drops I, L, O and U so what's shown can't be misread —
# and claim folds the look-alikes back (O→0, I/L→1) in case it is anyway.
AGENT_PAIRING_TTL_SECONDS = 300
_AGENT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_AGENT_CODE_LEN = 10
_AGENT_CODE_STRIP = str.maketrans({"-": None, " ": None, "_": None, "O": "0", "I": "1", "L": "1"})


@dataclass
class Device:
    """A paired device. ``token_sha256`` is the only trace of the credential."""

    id: str
    name: str
    token_sha256: str
    created_at: float
    last_seen_at: float | None = None
    # ``device`` (a phone/browser) or ``agent`` (another protoAgent's hub, ADR 0113 D3). Taken
    # from the pending code's kind at claim time — never from anything the claimer sent.
    kind: str = KIND_DEVICE

    def public(self) -> dict:
        """The shape safe to hand the console — everything except the hash."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
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
            raw_kind = item.get("kind")
            out.append(
                Device(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    token_sha256=str(item["token_sha256"]),
                    created_at=float(item["created_at"]),
                    last_seen_at=(float(item["last_seen_at"]) if item.get("last_seen_at") else None),
                    # A registry written before ADR 0113 has no kind: every entry was a phone.
                    # An unknown value also reads as "device" — the less-trusted-sounding label
                    # is the safe default for anything we can't vouch for.
                    # isinstance first: a hand-edited list/dict is unhashable, and a TypeError
                    # here would skip the whole entry — which the next `_save` then deletes.
                    kind=(raw_kind if isinstance(raw_kind, str) and raw_kind in _KINDS else KIND_DEVICE),
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):  # AttributeError: a non-dict entry
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


def _register(name: str, kind: str = KIND_DEVICE) -> tuple[Device, str]:
    """Mint a device + its token. The token is returned ONCE and never stored."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    device = Device(
        id=secrets.token_hex(8),
        name=(name or "").strip()[:64] or ("Unnamed agent" if kind == KIND_AGENT else "Unnamed device"),
        token_sha256=_hash(token),
        created_at=time.time(),
        kind=kind,
    )
    devices = _load()
    devices.append(device)
    _save(devices)
    logger.info("[devices] paired %s %s (%s)", device.kind, device.name, device.id)
    return device, token


# ── Pending pairings (memory-only, see the module docstring) ────────────────────────────
# normalized code -> (expiry timestamp, kind). Device codes are stored as issued; agent codes
# are stored normalized (no dash, uppercase) so claim compares like with like.
_PENDING: dict[str, tuple[float, str]] = {}
_failed_claims = [0]
# Claims arrive on the event loop today, but nothing stops a sync caller on a worker thread
# (the CLI, a test). The lock makes "find + consume" one step, so single-use holds under any
# concurrency, not just the event loop's.
_LOCK = threading.RLock()


def _prune(now: float) -> None:
    for code, (expires, _kind) in list(_PENDING.items()):
        if expires <= now:
            del _PENDING[code]


def _new_agent_code() -> str:
    return "".join(secrets.choice(_AGENT_CODE_ALPHABET) for _ in range(_AGENT_CODE_LEN))


def format_agent_code(code: str) -> str:
    """``ABCDEFGHJK`` → ``ABCDE-FGHJK`` — the form shown to (and typed by) the operator."""
    half = _AGENT_CODE_LEN // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_agent_code(code: str) -> str:
    """Fold what a human might type into the stored form: case, dashes/spaces/underscores,
    and the Crockford look-alikes (O→0, I/L→1). Never applied to device codes — those are
    case-sensitive url-safe strings where folding would merge distinct codes."""
    return (code or "").upper().translate(_AGENT_CODE_STRIP)


def start_pairing(kind: str = KIND_DEVICE) -> tuple[str, float]:
    """Mint a pairing code of ``kind``. Operator-authed callers only (enforced at the route).

    Returns the code in its DISPLAY form (an agent code as ``XXXXX-XXXXX``) and its expiry.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown pairing kind: {kind!r}")
    now = time.time()
    with _LOCK:
        _prune(now)
        if kind == KIND_AGENT:
            stored = _new_agent_code()
            while stored in _PENDING:  # astronomically unlikely; cheap to rule out
                stored = _new_agent_code()
            shown = format_agent_code(stored)
            expires_at = now + AGENT_PAIRING_TTL_SECONDS
        else:
            stored = shown = secrets.token_urlsafe(_CODE_BYTES)
            expires_at = now + PAIRING_TTL_SECONDS
        _PENDING[stored] = (expires_at, kind)
        # A fresh code gets a fresh 5-miss budget (ADR 0113 D2: "5 guesses per code the
        # operator issues"). Without this the counter never decays: four stale misses, then
        # one honest typo of a NEW typed code, would lock the legitimate claimer out.
        _failed_claims[0] = 0
    return shown, expires_at


def cancel_pairings(kind: str | None = None) -> None:
    """Drop pending codes — e.g. the operator closed the Add-device dialog.

    With ``kind``, only that kind goes: closing the phone dialog must not kill an agent code
    the operator is halfway through typing on another machine (and vice versa). With no
    argument, every pending code goes — the pre-ADR-0113 behaviour. NOTE: until the console
    slice (ADR 0113 S6) the console's cancel still sends no kind, so closing either dialog
    still clears both.
    """
    with _LOCK:
        if kind is None:
            _PENDING.clear()
            return
        for code, (_expires, code_kind) in list(_PENDING.items()):
            if code_kind == kind:
                del _PENDING[code]


def _match(code: str) -> str | None:
    """The pending key ``code`` redeems, or None. Caller holds ``_LOCK``.

    Device codes: exact, constant-time — as before. Agent codes: normalized first, then
    constant-time against AGENT codes only, so folding can never make a device code match.
    Every candidate is compared (no early exit on kind) to keep timing uniform.
    """
    matched: str | None = None
    folded = normalize_agent_code(code)
    for pending, (_expires, kind) in _PENDING.items():
        candidate = code if kind == KIND_DEVICE else folded
        if hmac.compare_digest(pending.encode("utf-8"), candidate.encode("utf-8")) and matched is None:
            matched = pending
    return matched


def claim_pairing(code: str, device_name: str) -> tuple[dict, str] | None:
    """Redeem a code for a fresh device token, or None if it isn't valid.

    Single-use: the code is removed before the device is created, so two racing claims
    cannot both succeed. Repeated failures drop every pending code — of BOTH kinds — rather
    than allowing indefinite probing of an open endpoint (this is reachable unauthenticated
    by necessity — ADR 0087 D4, ADR 0113 D2). The minted device's kind is the CODE's kind.
    """
    now = time.time()
    with _LOCK:
        _prune(now)
        if not code or not _PENDING:
            return None

        matched = _match(code)
        if matched is None:
            _failed_claims[0] += 1
            if _failed_claims[0] >= _MAX_FAILED_CLAIMS:
                logger.warning("[devices] %d failed pairing claims — dropping pending codes", _failed_claims[0])
                _PENDING.clear()
                _failed_claims[0] = 0
            return None

        _expires, kind = _PENDING.pop(matched)  # consume BEFORE minting, so a race can't double-issue
        _failed_claims[0] = 0
        # Minted under the lock too, so two CLAIMS can't interleave their load→append→save of
        # devices.json. This does not make the registry lock-protected in general: `_touch`
        # and `revoke_device` don't take `_LOCK` — in practice the event loop serializes them
        # (every caller is a sync call on the loop).
        device, token = _register(device_name, kind)
    return device.public(), token
