"""Per-device tokens + QR pairing (ADR 0087).

Auth before this module was a single shared secret (``a2a_impl.auth._BEARER``): every
device presented the same string, so revoking one device meant rotating the secret and
logging out *everything* — which in practice meant nobody revoked anything and a lost phone
stayed authorised.

This adds a registry of named devices, each with its own token, individually revocable. A
device token resolves to the **operator** tier: a paired phone runs the full console and
needs the same surface the desktop does. Since ADR 0121 D4 a phone's token also carries a
narrower credential **scope** (``device``): it can use the whole product but not change who can
get in — pairing, plugin lifecycle, secrets and credential-plane config. Agent devices (hubs)
and rows written before ADR 0121 are ``full``.

Two halves, deliberately different in durability:

* **The registry** is persisted (``instance_root/devices.json``) and stores only
  ``sha256(token)`` — never the token. A leaked registry cannot be replayed, and there is no
  way to recover a token after issue, so no "show token" affordance can ever be built.
* **Pending pairings** are memory-only. A restart invalidates them, which is the desired
  behavior: a code nobody claimed within its window should not survive anything.

Two KINDS of pending code share that one store and one claim throttle (ADR 0113 D2, as
amended by ADR 0121 D3 — see ``security.claim_throttle``): a **device** code (a phone scans
it off a QR, or types it into the console's "Have a pairing code?") and an **agent** code
(typed into another machine's "Pair…" dialog). Both are the same shape — 10 Crockford
base32 chars shown as ``XXXXX-XXXXX``, 300s (ADR 0087 D3 as amended 2026-10-18). The kind is fixed when the code is MINTED and copied onto the device it
yields, so a claimer can never relabel what it is.

A third use of the same store: a **handoff** code (ADR 0121 D1), minted by a paired phone for
itself, moves that phone to another of this instance's origins. Claiming it rotates the
existing device's token rather than adding a row (`start_handoff` / `_rebind`).

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
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from infra.paths import instance_paths
from security import claim_throttle

logger = logging.getLogger(__name__)

# A pairing code is displayed on screen (as a QR AND as text) and is claimable by anyone who
# can read it, so its safety comes from being short-lived, single-use, high-entropy and
# throttle-guarded (ADR 0121 D3) rather than secret. 300s, not the original 120s: since
# ADR 0087's 2026-10-18 amendment the one device code is also TYPED on the phone, and typing
# takes longer than scanning — the same reasoning that gave agent codes 300s.
PAIRING_TTL_SECONDS = 300
_TOKEN_BYTES = 32

KIND_DEVICE = "device"
KIND_AGENT = "agent"
_KINDS = frozenset({KIND_DEVICE, KIND_AGENT})

# Lifetime (ADR 0121 D5). Rotation cadence and grace are constants, not settings; only the
# idle expiry is configurable (``devices.idle_ttl_days``, `idle_ttl_days`).
ROTATION_INTERVAL_SECONDS = 7 * 86400
# How long a rotated-out token keeps working AFTER the new one is first presented — requests
# already in flight with the old token when the browser switched.
ROTATION_GRACE_SECONDS = 120
# A second rotate that presents the previous token within this long of an unused rotation
# is refused (409) rather than replacing it: two tabs racing the same rotation.
ROTATION_SETTLE_SECONDS = 120
DEFAULT_IDLE_TTL_DAYS = 30


def _now() -> float:
    """The registry's clock — one seam for tests to move time."""
    return time.time()


def idle_ttl_days() -> int:
    """``devices.idle_ttl_days`` from the live config (``0`` = never expire), read per call
    so a Settings save applies without a restart. No config loaded ⇒ the default."""
    try:
        from runtime.state import STATE

        value = getattr(STATE.graph_config, "device_idle_ttl_days", DEFAULT_IDLE_TTL_DAYS)
        days = int(value)
    except Exception:  # noqa: BLE001 — a missing/odd config reads as the default
        return DEFAULT_IDLE_TTL_DAYS
    return days if days >= 0 else DEFAULT_IDLE_TTL_DAYS


# Credential scope (ADR 0121 D4) — orthogonal to the trust TIER, which stays "operator" for
# every device. ``device`` scope can use the whole product but can't change who can get in
# (the credential-plane ceiling, ``a2a_impl.credential_scope``). Mirrors that module's
# constants; ``security`` doesn't import ``a2a_impl``.
SCOPE_FULL = "full"
SCOPE_DEVICE = "device"
_SCOPES = frozenset({SCOPE_FULL, SCOPE_DEVICE})

# Every code is TYPEABLE (agent codes per ADR 0113 D2; device codes since ADR 0087's
# 2026-10-18 amendment): short, unambiguous, with a window long enough to read it off one
# screen and type it into another. 10 chars of Crockford base32 = 50 bits; under the claim
# throttle (ADR 0121 D3) a code's 300 s admit at most ~150 evaluated guesses, ~1.3e-13
# odds. Crockford drops I, L, O and U so what's shown can't be misread — and claim folds the
# look-alikes back (O→0, I/L→1) in case it is anyway. One alphabet, one length, one fold for both kinds: what differs is only the
# kind stamped on the device the code yields.
AGENT_PAIRING_TTL_SECONDS = 300
_TTL_BY_KIND = {KIND_DEVICE: PAIRING_TTL_SECONDS, KIND_AGENT: AGENT_PAIRING_TTL_SECONDS}
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
    # ``full`` or ``device`` (ADR 0121 D4). The dataclass default is ``full`` because that is
    # what a row written before ADR 0121 means; NEW phone pairings are minted ``device`` in
    # `_register`. Read through `effective_scope`, never directly: an agent device is always
    # full, whatever its row says.
    scope: str = SCOPE_FULL
    # Rotation (ADR 0121 D5). ``prev_token_sha256`` is the token this device held before its
    # last rotation: it stays valid until the NEW token is first presented
    # (``prev_retired_at`` records when), then for ``ROTATION_GRACE_SECONDS`` more, and after
    # that presenting it REVOKES the device (reuse detection). ``rotated_at`` is when the
    # current token was minted by a rotation (``None`` = the pairing's own token).
    prev_token_sha256: str | None = None
    rotated_at: float | None = None
    prev_retired_at: float | None = None
    # The previous token was retired by a HANDOFF (ADR 0121 D1, #4336 S7.5), not a rotation:
    # the phone moved to another origin, and the old origin's storage still holds that token.
    # Presented after its grace it is refused WITHOUT revoking the device — the old origin is
    # the expected late presenter, not a second party. Cleared by the next rotation.
    prev_handed_off: bool = False

    def effective_scope(self) -> str:
        """The scope this device's token earns. An agent (another protoAgent's hub, ADR 0113)
        must proxy the whole console, so it is ``full`` regardless of the stored value."""
        if self.kind == KIND_AGENT:
            return SCOPE_FULL
        return self.scope if self.scope in _SCOPES else SCOPE_DEVICE

    def idle_since(self) -> float:
        """What idle expiry is measured from: the last use, or the pairing if never used."""
        return self.last_seen_at or self.created_at

    def expires_at(self) -> float | None:
        """When this device's token dies of idleness (ADR 0121 D5), or None if it never does:
        ``full`` scope (agents, legacy rows, promoted phones) and ``idle_ttl_days: 0``."""
        if self.effective_scope() != SCOPE_DEVICE:
            return None
        days = idle_ttl_days()
        if days <= 0:
            return None
        return self.idle_since() + days * 86400

    def is_expired(self, now: float | None = None) -> bool:
        exp = self.expires_at()
        return exp is not None and (_now() if now is None else now) >= exp

    def rotate_due(self, now: float | None = None) -> bool:
        """At most once per ``ROTATION_INTERVAL_SECONDS`` (a constant, D5): measured from the
        last rotation, or the pairing if never rotated. Only a ``device``-scope credential
        rotates; a full-access token (an agent hub, a legacy row) keeps its token."""
        if self.effective_scope() != SCOPE_DEVICE:
            return False
        since = self.rotated_at or self.created_at
        return ((_now() if now is None else now) - since) >= ROTATION_INTERVAL_SECONDS

    def public(self) -> dict:
        """The shape safe to hand the console — everything except the hash.

        ``active`` is computed HERE, against the same throttle that writes ``last_seen_at``,
        so the console's badge can't drift from the write cadence again (it used to test
        "seen in the last 60 s" against a stamp only refreshed every 300 s, so a phone in
        constant use showed "active" for one minute in five).
        """
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "scope": self.effective_scope(),
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "active": is_active(self.last_seen_at),
            # ADR 0121 D5: an expired device stays listed ("Expired") until it is removed.
            "expired": self.is_expired(),
            "expires_at": self.expires_at(),
            "rotated_at": self.rotated_at,
        }


# ── Device names ──────────────────────────────────────────────────────────────────────
# A name is operator-facing text in a short list, so the rule is "printable, one line,
# short". Rejected categories: control (Cc), format (Cf — includes the bidi overrides that
# would let a claimer's name render as something else), unassigned/private-use/surrogate
# (Cn/Co/Cs) and the line/paragraph separators (Zl/Zp). Everything else — accents, CJK,
# apostrophes, emoji — is a legitimate thing to call a phone.
MAX_NAME_LEN = 64
_BAD_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"})


def _clean_name(raw: str) -> str:
    """Drop disallowed characters, collapse whitespace runs, trim. May return ``""``."""
    # Whitespace first (a tab or newline is Cc, but it separated two words), then the rest.
    spaced = "".join(" " if ch.isspace() else ch for ch in unicodedata.normalize("NFC", raw or ""))
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) not in _BAD_NAME_CATEGORIES)
    return " ".join(kept.split())


def validate_device_name(raw: object) -> str:
    """The operator-supplied name for a rename, cleaned — or ``ValueError`` saying why not.

    Strict where `_register` is lenient: a claim name comes from an unauthenticated
    user-agent sniff and is silently cleaned, but a rename is something the operator TYPED,
    so an unusable one is refused with a reason rather than quietly altered.
    """
    if not isinstance(raw, str):
        raise ValueError("name must be a string")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("name can't be empty")
    if any(unicodedata.category(ch) in _BAD_NAME_CATEGORIES for ch in stripped):
        raise ValueError("name can't contain control or formatting characters")
    name = _clean_name(stripped)
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be {MAX_NAME_LEN} characters or fewer")
    return name


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
            raw_scope = item.get("scope", SCOPE_FULL)
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
                    # ADR 0121 D4: a row with NO scope predates it and keeps ``full`` (the
                    # Devices panel labels it "Full access" and offers Limit). A PRESENT but
                    # unknown value reads as ``device`` — the narrower scope is the safe
                    # default for anything we can't vouch for. Nothing is rewritten here.
                    scope=(raw_scope if isinstance(raw_scope, str) and raw_scope in _SCOPES else SCOPE_DEVICE),
                    # ADR 0121 D5 rotation state. Absent on every row written before it: no
                    # previous token, never rotated.
                    prev_token_sha256=_opt_str(item.get("prev_token_sha256")),
                    rotated_at=_opt_float(item.get("rotated_at")),
                    prev_retired_at=_opt_float(item.get("prev_retired_at")),
                    prev_handed_off=item.get("prev_handed_off") is True,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):  # AttributeError: a non-dict entry
            continue  # skip a hand-edited/partial entry rather than failing the whole load
    return out


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
        return None
    return float(value)


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


def rename_device(device_id: str, name: object) -> dict | None:
    """Rename a device; returns its public shape, or None if there's no such device.

    Raises ``ValueError`` (from `validate_device_name`) for an unusable name. Identity only:
    the token, kind and timestamps are untouched, so the renamed device stays signed in.
    """
    clean = validate_device_name(name)
    with _LOCK:
        devices = _load()
        for device in devices:
            if device.id == device_id:
                device.name = clean
                _save(devices)
                logger.info("[devices] renamed %s", device_id)
                return device.public()
    return None


def set_device_scope(device_id: str, scope: object) -> dict | None:
    """Promote (``full``) or limit (``device``) a device; returns its public shape, or None if
    there's no such device. ``ValueError`` for an unknown scope or for limiting an agent (a hub
    must proxy the whole console, ADR 0113). WHO may call this is the route's check: only a
    ``full``-scope caller (ADR 0121 D4)."""
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise ValueError("scope must be 'full' or 'device'")
    with _LOCK:
        devices = _load()
        for device in devices:
            if device.id != device_id:
                continue
            if device.kind == KIND_AGENT and scope != SCOPE_FULL:
                raise ValueError("an agent pairing always has full access")
            device.scope = str(scope)
            _save(devices)
            logger.info("[devices] scope of %s set to %s", device_id, scope)
            return device.public()
    return None


def revoke_device(device_id: str) -> bool:
    """Drop a device. Its token stops authenticating on the next request, and any stream it
    holds open closes within one keepalive (``is_live``, ADR 0121 D5)."""
    with _LOCK:
        devices = _load()
        remaining = [d for d in devices if d.id != device_id]
        if len(remaining) == len(devices):
            return False
        _save(remaining)
        _drop_handoffs(device_id)
    logger.info("[devices] revoked %s", device_id)
    return True


def verify_token(token: str) -> Device | None:
    """Return the device this token belongs to, else None.

    Compared by hash, so the registry never holds anything replayable. ``compare_digest`` on
    each candidate keeps the comparison constant-time; a plain dict lookup would leak
    nothing useful here (the input is already hashed) but the explicit compare keeps the
    intent obvious next to the rest of the auth code.

    Lifetime (ADR 0121 D5), decided here so every caller (the HTTP middleware, the fleet WS
    proxy's ``bearer_tier``/``bearer_scope``) gets the same answer:

    * an **expired** device (idle past ``devices.idle_ttl_days``) is refused, but stays listed;
    * the **current** token's first presentation after a rotation starts the previous
      token's ``ROTATION_GRACE_SECONDS`` grace;
    * the **previous** token is accepted until then and through the grace — and presented
      after it, it **revokes the device** and writes an audit entry (two parties hold one
      token: the theft signal rotation exists to surface).
    """
    if not token:
        return None
    digest = _hash(token)
    now = _now()
    current: Device | None = None
    previous: Device | None = None
    for device in _load():
        # Both compares on every row, no early exit: timing stays uniform across the registry.
        if hmac.compare_digest(device.token_sha256, digest) and current is None:
            current = device
        if device.prev_token_sha256 and hmac.compare_digest(device.prev_token_sha256, digest) and previous is None:
            previous = device
    if current is not None:
        if current.is_expired(now):
            return None
        if current.prev_token_sha256 and current.prev_retired_at is None:
            _confirm_rotation(current.id, digest, now)
        _touch(current.id)
        return current
    if previous is not None:
        if previous.prev_retired_at is not None and now >= previous.prev_retired_at + ROTATION_GRACE_SECONDS:
            if previous.prev_handed_off:
                # The origin this phone moved AWAY from (ADR 0121 D1): its storage still holds
                # the handed-off token. Refused, never a reuse revocation — see `_rebind`.
                logger.info("[devices] %s: the token handed off to another origin was presented; refused", previous.id)
                return None
            _revoke_on_reuse(previous, digest, now)
            return None
        if previous.is_expired(now):
            return None
        _touch(previous.id)
        return previous
    return None


def is_live(device_id: str | None) -> bool:
    """Does ``device_id`` still authenticate — paired, not revoked, not idle-expired?

    What long-lived streams re-check at their keepalive (ADR 0121 D5): a stream holds no
    token to re-present (an SSE stream was opened with a 30 s query token, and re-presenting
    a rotated-out token would trip reuse detection), so it re-verifies the DEVICE the
    middleware stamped on it. Read-only: never touches last-seen."""
    if not device_id:
        return False
    for device in _load():
        if device.id == device_id:
            return not device.is_expired()
    return False


def get_device(device_id: str | None) -> Device | None:
    if not device_id:
        return None
    for device in _load():
        if device.id == device_id:
            return device
    return None


def self_view(device_id: str | None, presented_token: str = "") -> dict | None:
    """``GET /api/devices/self`` for the caller's device id, or None if there is none.

    ``handed_off`` is true when ``presented_token`` is the token this device handed off to
    another origin (ADR 0121 D1) AND the new origin has used its token — the move completed,
    and the caller is the origin the phone moved AWAY from, still inside the grace. Its
    console forgets the token rather than keep presenting it. Before the new token is used
    (its storage write failed, say) this stays false: the old origin is still the phone's."""
    device = get_device(device_id)
    if device is None:
        return None
    out = device.public()
    out["rotate_due"] = device.rotate_due()
    out["handed_off"] = bool(
        presented_token
        and device.prev_handed_off
        and device.prev_retired_at is not None
        and device.prev_token_sha256
        and hmac.compare_digest(device.prev_token_sha256, _hash(presented_token))
    )
    return out


class RotationInProgress(Exception):
    """A rotation minted moments ago hasn't been presented yet (see `rotate_device`)."""


def rotate_device(device_id: str, presented_token: str) -> tuple[dict, str] | None:
    """Mint this device a new token (ADR 0121 D5); returns ``(self_view, token)`` once, or
    None when there is no such (live, ``device``-scope) device or the presented token isn't
    one of its own.

    The registry ends up with the new hash as current and the PRESENTED token as previous —
    the one the caller provably holds — so a rotate never leaves the browser without a
    working token, whatever happens to its storage write:

    * **Presented the current token** (the normal case): previous ← current, current ← new.
      A previous token still inside its grace is dropped (it 401s from now on).
    * **Presented the previous token while the current one was never used** (a rotate whose
      response the browser failed to store): the never-presented current is discarded and
      replaced — nobody can be relying on a token nobody has used — and the previous stays.
      Inside ``ROTATION_SETTLE_SECONDS`` of that unused rotation this raises
      :class:`RotationInProgress` instead, so two tabs racing one rotation can't each mint
      a token and leave storage holding the loser's.
    * **Presented the previous token after the current one was used**: not a rotation the
      caller may make — the previous token is retired. (Past its grace, `verify_token` has
      already revoked the device before any route runs.)
    """
    if not presented_token:
        return None
    digest = _hash(presented_token)
    now = _now()
    with _LOCK:
        devices = _load()
        device = next((d for d in devices if d.id == device_id), None)
        # Only a ``device``-scope token rotates (D5): a hub stores its token in fleet config and
        # never rotates, and a full-access row doesn't expire either.
        if device is None or device.is_expired(now) or device.effective_scope() != SCOPE_DEVICE:
            return None
        if hmac.compare_digest(device.token_sha256, digest):
            prev = device.token_sha256
        elif device.prev_token_sha256 and hmac.compare_digest(device.prev_token_sha256, digest):
            if device.prev_retired_at is not None:
                return None
            if device.rotated_at and now - device.rotated_at < ROTATION_SETTLE_SECONDS:
                raise RotationInProgress()
            prev = device.prev_token_sha256
        else:
            return None
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        device.prev_token_sha256 = prev
        device.token_sha256 = _hash(token)
        device.rotated_at = now
        device.prev_retired_at = None
        # A plain rotation: the previous token is held by THIS origin's own storage, so a late
        # presentation of it is the theft signal again.
        device.prev_handed_off = False
        _save(devices)
        view = device.public()
        view["rotate_due"] = device.rotate_due(now)
    logger.info("[devices] rotated the token of %s", device_id)
    return view, token


def _confirm_rotation(device_id: str, digest: str, now: float) -> None:
    """The new token was presented: start the previous token's grace."""
    with _LOCK:
        devices = _load()
        for device in devices:
            if device.id == device_id and device.token_sha256 == digest and device.prev_retired_at is None:
                device.prev_retired_at = now
                try:
                    _save(devices)
                except OSError:
                    logger.warning("[devices] could not record first use of %s's new token", device_id)
                return


def _revoke_on_reuse(device: Device, digest: str, now: float) -> None:
    """A retired token came back after its grace: revoke the device and audit it."""
    with _LOCK:
        devices = _load()
        remaining = [d for d in devices if not (d.id == device.id and d.prev_token_sha256 == digest)]
        if len(remaining) == len(devices):
            return  # already revoked (or rotated again) by a racing request
        _save(remaining)
    logger.warning(
        "[devices] REVOKED %s (%s): a rotated-out token was presented %ds after its grace — two parties held one token",
        device.id,
        device.name,
        int(now - (device.prev_retired_at or now) - ROTATION_GRACE_SECONDS),
    )
    _audit(
        "device.revoked_token_reuse",
        {"device_id": device.id, "name": device.name, "retired_at": device.prev_retired_at},
        "a rotated-out device token was presented after its grace; the device was revoked",
        success=False,
    )


def _audit(action: str, args: dict, summary: str, *, success: bool = True) -> None:
    """The shared audit log (``observability.audit``, ``instance_root/audit/audit.jsonl``)."""
    try:
        from observability.audit import audit_logger

        audit_logger.log(
            session_id="devices",
            tool=action,
            args=args,
            result_summary=summary,
            duration_ms=0,
            success=success,
        )
    except Exception:  # noqa: BLE001 — auditing must never break auth
        logger.debug("[devices] audit log failed for %s", action, exc_info=True)


# Writing on every request would be a disk write per API call, so last-seen is coarse.
# Kept at 300 s rather than lowered to match the old 60 s badge: ADR 0121 D5 measures idle
# expiry against this stamp at DAY granularity, so finer resolution buys nothing there, and
# every refresh is a full read-modify-rename of devices.json on the request path. The badge
# follows the throttle instead (`is_active`).
_LAST_SEEN_THROTTLE_SECONDS = 300
# A device in continuous use has a stamp at most one throttle window old; the margin covers
# a request that lands just after the window plus clock skew between list and render.
ACTIVE_WINDOW_SECONDS = _LAST_SEEN_THROTTLE_SECONDS + 60


def is_active(last_seen_at: float | None, now: float | None = None) -> bool:
    """Was this device used recently enough that its last-seen stamp can't tell us more?"""
    if not last_seen_at:
        return False
    return ((_now() if now is None else now) - last_seen_at) < ACTIVE_WINDOW_SECONDS


def _touch(device_id: str) -> None:
    now = _now()
    with _LOCK:  # the same read-modify-rename as a rotation: one at a time, or one is lost
        devices = _load()
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
        # Cleaned, not validated: the claimer is unauthenticated and the name is a UA sniff,
        # so an odd one is repaired rather than failing the pairing.
        name=_clean_name(name)[:MAX_NAME_LEN].strip() or ("Unnamed agent" if kind == KIND_AGENT else "Unnamed device"),
        token_sha256=_hash(token),
        created_at=_now(),
        kind=kind,
        # ADR 0121 D4: every new phone/tablet pairing — LAN, tailnet or public — is
        # ``device`` scope; an agent (a hub) is ``full``.
        scope=SCOPE_FULL if kind == KIND_AGENT else SCOPE_DEVICE,
    )
    devices = _load()
    devices.append(device)
    _save(devices)
    logger.info("[devices] paired %s %s (%s)", device.kind, device.name, device.id)
    return device, token


# ── Pending pairings (memory-only, see the module docstring) ────────────────────────────
# normalized code -> (expiry timestamp, kind). Every code is stored normalized (no dash,
# uppercase) so claim compares like with like.
_PENDING: dict[str, tuple[float, str]] = {}
# Pending HANDOFF codes (ADR 0121 D1, #4336 S7.5): the subset of `_PENDING` keys that rebind
# an EXISTING device instead of minting one → (device id, sha256 of the token that asked).
# Same store, same `_match`, same throttle; only what a claim does differs.
_HANDOFF: dict[str, tuple[str, str]] = {}
# Claims arrive on the event loop today, but nothing stops a sync caller on a worker thread
# (the CLI, a test). The lock makes "find + consume" one step, so single-use holds under any
# concurrency, not just the event loop's.
_LOCK = threading.RLock()


def _prune(now: float) -> None:
    for code, (expires, _kind) in list(_PENDING.items()):
        if expires <= now:
            del _PENDING[code]
            _HANDOFF.pop(code, None)


def _drop_handoffs(device_id: str) -> None:
    """Forget every pending handoff code for ``device_id`` (a revoke, or a newer handoff)."""
    with _LOCK:
        for code, (bound_id, _digest) in list(_HANDOFF.items()):
            if bound_id == device_id:
                _HANDOFF.pop(code, None)
                _PENDING.pop(code, None)


def _new_code() -> str:
    return "".join(secrets.choice(_AGENT_CODE_ALPHABET) for _ in range(_AGENT_CODE_LEN))


def format_code(code: str) -> str:
    """``ABCDEFGHJK`` → ``ABCDE-FGHJK`` — the form shown to (and typed by) the operator."""
    half = _AGENT_CODE_LEN // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_code(code: str) -> str:
    """Fold what a human might type into the stored form: case, dashes/spaces/underscores,
    and the Crockford look-alikes (O→0, I/L→1). Applied to every claim — device and agent
    codes share the alphabet, so folding can't merge two distinct issued codes."""
    return (code or "").upper().translate(_AGENT_CODE_STRIP)


# The ADR 0113 names, kept so existing callers (and the hub-side fleet code) read unchanged.
_new_agent_code = _new_code
format_agent_code = format_code
normalize_agent_code = normalize_code


def start_pairing(kind: str = KIND_DEVICE) -> tuple[str, float]:
    """Mint a pairing code of ``kind``. Operator-authed callers only (enforced at the route).

    Returns the code in its DISPLAY form (``XXXXX-XXXXX``, either kind) and its expiry.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown pairing kind: {kind!r}")
    now = time.time()
    with _LOCK:
        _prune(now)
        # Looked up through the module so a test can pin the generator by monkeypatching
        # `_new_agent_code` (the alias) — the name tests have always patched.
        stored = _new_agent_code()
        while stored in _PENDING:  # astronomically unlikely; cheap to rule out
            stored = _new_agent_code()
        shown = format_code(stored)
        expires_at = now + _TTL_BY_KIND[kind]
        _PENDING[stored] = (expires_at, kind)
    return shown, expires_at


def cancel_pairings(kind: str | None = None) -> None:
    """Drop pending codes — e.g. the operator closed the Add-device dialog.

    With ``kind``, only that kind goes: closing the phone dialog must not kill an agent code
    the operator is halfway through typing on another machine (and vice versa). With no
    argument, every pending code goes — the pre-ADR-0113 behaviour. The console always sends
    its dialog's kind (``agentPairing.cancelBody``).
    """
    # Handoff codes are never cancelled here: they belong to a phone moving itself, not to
    # the operator's dialog, and closing "Add a device" must not strand a move in flight.
    with _LOCK:
        for code, (_expires, code_kind) in list(_PENDING.items()):
            if code in _HANDOFF:
                continue
            if kind is None or code_kind == kind:
                del _PENDING[code]


def _match(code: str) -> str | None:
    """The pending key ``code`` redeems, or None. Caller holds ``_LOCK``.

    Every code (either kind) is stored normalized, so the claim is folded once and compared
    constant-time against every candidate — no early exit, to keep timing uniform. A
    pre-amendment ``#pair=`` link carried a long url-safe code; none can still be pending
    (pending codes are memory-only and a restart clears them), and such a string folds to
    something no 10-char code equals, so it is simply a miss.
    """
    matched: str | None = None
    folded = normalize_code(code).encode("utf-8")
    for pending in _PENDING:
        if hmac.compare_digest(pending.encode("utf-8"), folded) and matched is None:
            matched = pending
    return matched


def claim_pairing(
    code: str, device_name: str, *, client: str = "", ingress: str = claim_throttle.INGRESS_PRIVATE
) -> tuple[dict, str] | None:
    """Redeem a code for a fresh device token, or None if it isn't valid.

    Single-use: the code is removed before the device is created, so two racing claims
    cannot both succeed. The minted device's kind is the CODE's kind. A HANDOFF code
    (`start_handoff`, ADR 0121 D1) mints no device: it rebinds the one it was minted for
    (`_rebind`) and the returned view carries ``moved: True``.

    Throttled, not locked out (ADR 0121 D3, amending ADR 0087 D4 and ADR 0113 D2): a claim
    from a ``client`` or ``ingress`` class whose bucket is empty raises
    :class:`~security.claim_throttle.ClaimThrottled` WITHOUT being compared to any code —
    even the right one. Only misses draw; nothing a claimer sends can cancel a pending code.
    ``client`` defaults to ``""``: an in-process caller shares one bucket rather than going
    unthrottled.
    """
    now = time.time()
    with _LOCK:
        gate = claim_throttle.throttle()
        gate.check(client, ingress)  # before ANY look at the codes: a throttled claim isn't a guess
        _prune(now)
        if not code or not _PENDING:
            # Rejected before any comparison, as ever. It still spends the client's budget so
            # probers stay throttled, but not the class budget: nothing was compared.
            gate.miss(client, ingress, anything_pending=False)
            return None

        matched = _match(code)
        if matched is None:
            gate.miss(client, ingress, anything_pending=True)
            return None

        _expires, kind = _PENDING.pop(matched)  # consume BEFORE minting, so a race can't double-issue
        handoff = _HANDOFF.pop(matched, None)
        if handoff is not None:
            # A handoff code (ADR 0121 D1): rebind the EXISTING device — no new row, same id,
            # name and scope, a rotated token. A device revoked (or expired) since the code was
            # minted yields nothing, with the one failure message every bad code gets.
            return _rebind(*handoff)
        # Minted under the lock too, so two CLAIMS can't interleave their load→append→save of
        # devices.json. This does not make the registry lock-protected in general: `_touch`
        # and `revoke_device` don't take `_LOCK` — in practice the event loop serializes them
        # (every caller is a sync call on the loop).
        device, token = _register(device_name, kind)
    return device.public(), token


# ── Handoff: move a device to another of this instance's origins (ADR 0121 D1) ───────────
# A phone's token, transcripts and home-screen icon live on ONE origin. Moving it (LAN →
# tailnet, tailnet → the public URL) is a fresh claim on the new origin of a short-lived code
# bound to the existing device, so the registry keeps one row per phone and the token never
# travels in a URL.
HANDOFF_TTL_SECONDS = 60


def start_handoff(device_id: str | None, presented_token: str) -> tuple[str, float] | None:
    """Mint a 60 s, single-use handoff code for ``device_id`` — the caller's own device, proved
    by ``presented_token`` — or None when it can't move: no such device, an agent (a hub has no
    console origin), expired, or the token isn't one the device currently honours as its own.

    Same alphabet, length and display form as a pairing code (``XXXXX-XXXXX``), in the same
    pending store, so a claim goes through the same throttle. At most one per device: a newer
    handoff replaces an older one. WHICH origin it's for is the route's check — the code itself
    works on any origin of this instance, which is fine: every origin is the same registry."""
    if not device_id or not presented_token:
        return None
    digest = _hash(presented_token)
    with _LOCK:
        device = get_device(device_id)
        if device is None or device.kind != KIND_DEVICE or device.is_expired(_now()):
            return None
        holds_current = hmac.compare_digest(device.token_sha256, digest)
        # The previous token counts only while it is still the one in use (the new token was
        # never presented — a rotation whose storage write failed). In its grace it's on the
        # way out, and moving with it would hand off a token this browser has already replaced.
        holds_unretired_prev = bool(
            device.prev_token_sha256
            and device.prev_retired_at is None
            and hmac.compare_digest(device.prev_token_sha256, digest)
        )
        if not (holds_current or holds_unretired_prev):
            return None
        _drop_handoffs(device_id)
        now = time.time()
        _prune(now)
        stored = _new_agent_code()
        while stored in _PENDING:
            stored = _new_agent_code()
        expires_at = now + HANDOFF_TTL_SECONDS
        _PENDING[stored] = (expires_at, KIND_DEVICE)
        _HANDOFF[stored] = (device_id, digest)
    logger.info("[devices] %s asked to move to another origin", device_id)
    return format_code(stored), expires_at


def _rebind(device_id: str, digest: str) -> tuple[dict, str] | None:
    """Claim side of a handoff: rotate ``device_id``'s token (ADR 0121 D5 semantics) and mark
    the token that asked as HANDED OFF. Caller holds ``_LOCK``.

    The token that asked for the handoff (``digest``) becomes the previous token. As with any
    rotation it stays valid until the NEW token is first presented, then for the 120 s grace —
    so a new origin that fails to store its token leaves the old origin working. Unlike a
    rotation, presenting it after the grace is refused WITHOUT revoking the device
    (``prev_handed_off``): the old origin's storage still holds it by design, and the phone
    going back there must not cost it its pairing. The old origin's console drops the token
    as soon as the server says it was handed off (``self_view``'s ``handed_off``)."""
    devices = _load()
    device = next((d for d in devices if d.id == device_id), None)
    now = _now()
    if device is None or device.kind != KIND_DEVICE or device.is_expired(now):
        return None
    if hmac.compare_digest(device.token_sha256, digest):
        prev = device.token_sha256
    elif device.prev_token_sha256 and device.prev_retired_at is None and hmac.compare_digest(device.prev_token_sha256, digest):
        # The asker held the previous token of a rotation nobody ever used; that rotation's
        # token is discarded (nobody can be relying on it) — the same rule as `rotate_device`.
        prev = device.prev_token_sha256
    else:
        return None  # the asker's token was rotated out meanwhile: it can't hand off any more
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    device.prev_token_sha256 = prev
    device.token_sha256 = _hash(token)
    device.rotated_at = now
    device.prev_retired_at = None
    device.prev_handed_off = True
    _save(devices)
    logger.info("[devices] %s (%s) moved to another origin; its token was rotated", device.id, device.name)
    _audit(
        "device.handed_off",
        {"device_id": device.id, "name": device.name},
        "a paired device moved to another of this agent's addresses; its token was rotated",
    )
    view = device.public()
    view["moved"] = True  # the claim route reports it as `moved`, outside the device shape
    return view, token
