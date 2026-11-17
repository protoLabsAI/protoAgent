"""Short-lived signed delegations for human-operator authority across a trust hop.

Conversations-in-Work Phase 1 (P1a.2, ADR 0131 amendment / proposal §3). Decision 1 says
"the operator" for private steering is the human's own console session only; **any** agent
— fleet service token, paired ``agent`` devices, federation — is a peer. The P1a.1 primitive
``a2a_impl.auth.is_human_operator`` classifies a *credential*; this module carries that human
identity across the two places a credential cannot: the fleet proxy hop (hub → member) and the
loopback self-POST (``server/chat.py`` intake).

**Why a signed artifact and not the bearer.** A member sees only the fleet service token (the
hub swaps the caller's credential, ADR 0089 D3), which is a **peer** kind — ``fleet-service``.
A self-invocation presents the shared operator bearer, which is human *as a credential* but is
an automated call, not a human. So neither the transport credential nor "it came over loopback"
may confer human-operator status. Instead the hub, having authenticated a human caller
(``is_human_operator(request.state.principal_kind)``), mints a delegation bound to ONE member,
method, context and operation; the owner verifies issuer, audience, expiry, nonce and
revocation generation before honoring it. A swapped fleet bearer alone carries no delegation,
so it stays a peer whatever operator tier it earns.

**Dormant.** Nothing in the live request path grants private access off a delegation yet; the
later private-steering slices (P1b.2 binds the loopback delegation to the persisted
private-input id) consume it. This slice lands the mint/verify/redeem mechanism and its proof.

**Signing key — asymmetric, so the capability to MINT is genuinely hub-held.** A symmetric key
would have to be shared with the owner to verify, and every member already holds the fleet
service token (injected by env, ``graph.fleet.service_token``); deriving the key from *that*
would let any member — or anyone who swapped in the fleet bearer — mint a delegation for any
audience. So the signature is **Ed25519**: a hub-held PRIVATE key signs, and the owner verifies
with the matching PUBLIC key.

* The private key is a hub server secret — resolved from ``PROTOAGENT_FLEET_DELEGATION_SEED``
  (an explicit/standalone seam) else read-or-created at ``workspaces_root()/.fleet-delegation-key``
  (``0600``, hardened), **never** delivered to a member. A member's own ``workspaces_root`` is
  empty by construction, so it has no hub private key and cannot mint a proxy delegation any
  owner trusts; possessing the fleet bearer reaches none of this.
* The owner verifies a **proxy**-issued delegation against the hub's public key
  (``PROTOAGENT_FLEET_DELEGATION_PUBKEY`` on a member — a public key is safe to deliver; P1b
  wires that spawn injection when a member first consumes a delegation) and a **loopback**-issued
  one against its own local key (minted and redeemed inside one process). Until the member
  spawn injection lands, a member only ever verifies its own loopback mints — which is all the
  dormant slice exercises.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

logger = logging.getLogger(__name__)

# --- Untrusted inbound headers the proxy strips before minting its own ----------------------
#
# A caller must never be able to assert its own principal or hand the member a delegation: the
# hub authenticates the caller and mints the one delegation the member will honor. These are the
# header names (lower-cased) that carry a principal claim or a delegation; the proxy drops any
# the caller sent and re-adds only what it mints itself.
PRINCIPAL_KIND_HEADER = "x-principal-kind"
HUMAN_OPERATOR_HEADER = "x-human-operator"
DELEGATION_HEADER = "x-fleet-delegation"
PRINCIPAL_HEADERS = frozenset({PRINCIPAL_KIND_HEADER, HUMAN_OPERATOR_HEADER, DELEGATION_HEADER})

# Default issuers an owner trusts: the fleet proxy hop and the loopback self-POST. Both are
# this-instance mints; a token whose ``iss`` is anything else is forged/foreign and refused.
ISSUER_PROXY = "fleet-proxy"
ISSUER_LOOPBACK = "loopback"
_KNOWN_ISSUERS = frozenset({ISSUER_PROXY, ISSUER_LOOPBACK})

# The human-operator principal kind a verified delegation conveys to the owner. It is always a
# human kind (``is_human_operator`` true); a peer caller never obtains a delegation to begin with.
DELEGATED_PRINCIPAL_KIND = "shared-operator-bearer"

_DELEGATION_TTL = 30.0  # seconds — short-lived; a stalled op re-mints rather than replays

# Hub-held private signing key. The env seed is an explicit/standalone seam (and the test seam);
# otherwise the hub reads-or-creates a 32-byte seed file beside the fleet token registry. The
# public counterpart a member verifies PROXY delegations against rides its own env var.
_SEED_ENV = "PROTOAGENT_FLEET_DELEGATION_SEED"
_PUBKEY_ENV = "PROTOAGENT_FLEET_DELEGATION_PUBKEY"
_KEY_FILENAME = ".fleet-delegation-key"
_SEED_BYTES = 32  # Ed25519 private seed

# Bounded redemption ledger (nonce → expiry): replay rejection for the proxy hop, idempotent
# re-redeem for loopback. In-memory and process-local — a restart drops it, which fails CLOSED
# (an unknown nonce after a restart is treated as fresh for idempotent loopback, and the proxy
# hop's tokens are 30s-lived so a restart window cannot be replayed into). Capped like the
# proxy's own ``_ticket_bindings`` so a flood can't grow it without bound.
_LEDGER_LIMIT = 8192
_redeemed: dict[str, float] = {}

# Process cache for the private key — constant for a process; rotation is restart-gated.
_private_key_cache: list[Ed25519PrivateKey | None] = [None]

# Revocation generation. A delegation carries the generation current at mint; the owner compares
# it against the live generation and refuses an older one. The accessor is a seam so a real
# revocation service can replace it and so a test can make it unavailable (fail closed).
_revocation_generation = [0]


class DelegationError(Exception):
    """A delegation failed verification: forged, wrong target, expired, replayed or revoked."""


class RevocationUnavailable(DelegationError):
    """The revocation generation could not be read — private access fails CLOSED."""


@dataclass(frozen=True)
class Delegation:
    """A verified single-operation grant of human-operator authority."""

    issuer: str
    audience: str
    method: str
    context: str
    operation: str
    task_id: str
    nonce: str
    expires_at: float
    revocation_gen: int
    principal_kind: str = DELEGATED_PRINCIPAL_KIND


def current_revocation_generation() -> int:
    """The live revocation generation. Raising or returning None here is "service unavailable",
    and the owner treats that as fail-closed (``RevocationUnavailable``)."""
    return _revocation_generation[0]


def bump_revocation_generation() -> int:
    """Advance the generation so every delegation minted before now is refused. Test/ops seam."""
    _revocation_generation[0] += 1
    return _revocation_generation[0]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _key_path():
    from graph.workspaces import manager

    return manager.workspaces_root() / _KEY_FILENAME


def _read_or_create_seed() -> bytes:
    """The hub's persisted private seed, minting + writing one ``0600`` if absent.

    Mirrors ``service_token._read_or_create`` (pid-unique temp + atomic replace + re-read), so a
    concurrent creator can't truncate it and every hub caller converges on one key. Delivered to
    members? Never — only the PUBLIC key is (and only when P1b wires it); a member's empty
    ``workspaces_root`` means it reads-or-creates its OWN unrelated key, useful only for its own
    loopback, never trusted as a proxy mint by any sister."""
    from infra.paths import harden_private_file

    path = _key_path()
    try:
        existing = path.read_bytes().strip()
        if len(existing) == _SEED_BYTES:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("[fleet] delegation key unreadable at %s — regenerating", path)

    seed = secrets.token_bytes(_SEED_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{_KEY_FILENAME}.{os.getpid()}.tmp")
        tmp.write_bytes(seed)
        os.chmod(tmp, 0o600)  # 0600 before the rename — a server secret, even on loopback.
        os.replace(tmp, path)  # atomic
        harden_private_file(path)
        winner = path.read_bytes().strip()
        return winner if len(winner) == _SEED_BYTES else seed
    except OSError:
        logger.warning("[fleet] could not persist delegation key at %s — using an ephemeral one", path)
        return seed


def _load_private_key() -> Ed25519PrivateKey:
    seed = os.environ.get(_SEED_ENV, "").strip()
    raw = _unb64(seed) if seed else _read_or_create_seed()
    return Ed25519PrivateKey.from_private_bytes(raw[:_SEED_BYTES])


def _private_key() -> Ed25519PrivateKey:
    """The hub's Ed25519 private key for THIS process (never log it, never deliver it). Cached."""
    if _private_key_cache[0] is None:
        _private_key_cache[0] = _load_private_key()
    return _private_key_cache[0]


def _public_key(issuer: str | None) -> Ed25519PublicKey:
    """The public key that verifies a delegation from ``issuer``.

    A **proxy** delegation is minted by the hub, so on a member the owner verifies it against the
    hub's injected public key (``PROTOAGENT_FLEET_DELEGATION_PUBKEY``); on the hub / standalone it
    is the local private key's public half. A **loopback** delegation is minted and redeemed in
    one process, so it is always the local key. (No pubkey env, or any other issuer, falls back to
    the local key — on the hub that is correct; on a member that only verifies its own loopback,
    which is all the dormant slice reaches.)"""
    if issuer == ISSUER_PROXY:
        pub = os.environ.get(_PUBKEY_ENV, "").strip()
        if pub:
            return Ed25519PublicKey.from_public_bytes(_unb64(pub)[:32])
    return _private_key().public_key()


def reset_signing_keys() -> None:
    """Drop the cached private key (test / restart-rotation seam)."""
    _private_key_cache[0] = None


def public_key_b64() -> str:
    """The hub's delegation public key, url-safe base64 — what a member is given to verify a
    PROXY delegation (``PROTOAGENT_FLEET_DELEGATION_PUBKEY``). Dormant delivery seam for P1b."""
    from cryptography.hazmat.primitives import serialization

    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return _b64(raw)


def mint_delegation(
    *,
    audience: str,
    method: str,
    context: str,
    operation: str,
    task_id: str = "",
    issuer: str = ISSUER_PROXY,
    ttl: float = _DELEGATION_TTL,
    now: float | None = None,
    revocation_gen: int | None = None,
    signing_key: Ed25519PrivateKey | None = None,
) -> str:
    """Mint a signed delegation bound to one ``audience`` (member slug / loopback target),
    ``method``, ``context`` and ``operation`` (plus the ``task_id`` for the loopback binding).

    The caller must already be an authenticated human operator — the fleet proxy checks
    ``is_human_operator`` before calling this, and a peer caller is never handed a token. Minting
    needs the hub's PRIVATE key (``signing_key`` overrides it for tests); holding the fleet
    service token reaches nothing here.
    """
    now = time.time() if now is None else now
    rev = current_revocation_generation() if revocation_gen is None else revocation_gen
    claims = {
        "iss": issuer,
        "aud": audience,
        "mth": method,
        "ctx": context,
        "op": operation,
        "tid": task_id,
        "nonce": secrets.token_urlsafe(16),
        "exp": now + ttl,
        "rev": rev,
        "pk": DELEGATED_PRINCIPAL_KIND,
    }
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sk = signing_key or _private_key()
    sig = _b64(sk.sign(payload.encode("ascii")))
    return f"{payload}.{sig}"


def verify_delegation(
    token: str,
    *,
    audience: str,
    method: str | None = None,
    context: str | None = None,
    operation: str | None = None,
    task_id: str | None = None,
    allowed_issuers: frozenset[str] = _KNOWN_ISSUERS,
    now: float | None = None,
    revocation_check=current_revocation_generation,
    verifying_key: Ed25519PublicKey | None = None,
) -> Delegation:
    """Verify a delegation (no ledger side effect) and return it, or raise ``DelegationError``.

    The owner checks, in order: the issuer (foreign → refuse), the Ed25519 signature against the
    public key for that issuer (forged / signed with the wrong key → refuse), the target
    ``audience`` (wrong member → refuse), any bound ``method``/``context``/``operation``/``task_id``
    the caller pins, the expiry, and the revocation generation. If the revocation check cannot be
    read (raises or returns None) the result is ``RevocationUnavailable`` — private access fails
    CLOSED rather than assuming the grant is still live.
    """
    now = time.time() if now is None else now
    try:
        payload, sig = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise DelegationError("malformed delegation") from exc
    # Decode the claims UNTRUSTED, only to read ``iss`` so we pick the right public key (JWT
    # ``kid`` style). A lie about the issuer merely selects a key the signature won't verify
    # under — the signature check below is what makes it binding.
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, TypeError) as exc:
        raise DelegationError("undecodable delegation") from exc
    if not isinstance(claims, dict):
        raise DelegationError("undecodable delegation")

    issuer = claims.get("iss")
    if issuer not in allowed_issuers:
        raise DelegationError(f"untrusted issuer {issuer!r}")
    vk = verifying_key or _public_key(issuer)
    try:
        vk.verify(_unb64(sig), payload.encode("ascii"))
    except (InvalidSignature, ValueError) as exc:  # bad sig, wrong key, or undecodable signature
        raise DelegationError("bad signature") from exc

    if claims.get("aud") != audience:
        raise DelegationError("wrong target audience")
    for pinned, field, label in (
        (method, "mth", "method"),
        (context, "ctx", "context"),
        (operation, "op", "operation"),
        (task_id, "tid", "task"),
    ):
        if pinned is not None and claims.get(field) != pinned:
            raise DelegationError(f"delegation {label} mismatch")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp <= now:
        raise DelegationError("expired delegation")

    # Revocation last, and fail CLOSED: an unreadable revocation service must deny, never admit.
    try:
        live_gen = revocation_check()
    except Exception as exc:  # noqa: BLE001 — any failure to read revocation is "unavailable"
        raise RevocationUnavailable("revocation generation unavailable") from exc
    if live_gen is None:
        raise RevocationUnavailable("revocation generation unavailable")
    if claims.get("rev") != live_gen:
        raise DelegationError("revoked delegation generation")

    return Delegation(
        issuer=issuer,
        audience=audience,
        method=str(claims.get("mth", "")),
        context=str(claims.get("ctx", "")),
        operation=str(claims.get("op", "")),
        task_id=str(claims.get("tid", "")),
        nonce=str(claims.get("nonce", "")),
        expires_at=float(exp),
        revocation_gen=int(live_gen),
        principal_kind=str(claims.get("pk", DELEGATED_PRINCIPAL_KIND)),
    )


def _evict_expired(now: float) -> None:
    for nonce, exp in list(_redeemed.items()):
        if exp <= now:
            del _redeemed[nonce]
    # Capacity backstop. Only the hub mints (the private key is hub-held), so a flood of LIVE
    # nonces isn't attacker-reachable; if the cap is still hit we drop the soonest-to-expire
    # entry — the one whose remaining replay window is smallest — never a longer-lived live one.
    while len(_redeemed) >= _LEDGER_LIMIT:
        del _redeemed[min(_redeemed, key=_redeemed.get)]


def redeem_delegation(
    token: str,
    *,
    audience: str,
    idempotent: bool = False,
    now: float | None = None,
    **verify_kwargs,
) -> Delegation:
    """Verify ``token`` and spend it against the redemption ledger.

    The fleet proxy hop redeems with ``idempotent=False`` — a nonce seen before is a REPLAY and
    is refused. The loopback self-POST redeems with ``idempotent=True`` — re-presenting the SAME
    single-operation delegation (a transport retry) returns the same grant rather than failing,
    while a fresh nonce is recorded once. Either way verification runs first, so a forged, wrong
    target, expired or revoked token never reaches the ledger.
    """
    now = time.time() if now is None else now
    delegation = verify_delegation(token, audience=audience, now=now, **verify_kwargs)
    _evict_expired(now)
    seen = delegation.nonce in _redeemed
    if seen and not idempotent:
        raise DelegationError("replayed delegation")
    _redeemed[delegation.nonce] = delegation.expires_at
    return delegation


def reset_ledger() -> None:
    """Clear the redemption ledger (test seam; mirrors the proxy's ``_slug_cache.clear()``)."""
    _redeemed.clear()
