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

**Signing key.** Domain-separated from the loopback-only fleet service token
(``graph.fleet.service_token``): every agent in one instance resolves the same service token
(the hub from its file, a member from the injected env var), so both ends of the hop derive the
same delegation key without a second distributed secret. The capability to MINT is a hub-held
server secret; possessing the raw fleet bearer as a transport credential does not by itself
reach this signing path on the authenticated hub, and on the owner side a presented fleet
bearer only ever earns the ``fleet-service`` peer kind.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

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
_KEY_INFO = b"protoagent-fleet-delegation-v1"  # HKDF-style domain separator off the service token

# Bounded redemption ledger (nonce → expiry): replay rejection for the proxy hop, idempotent
# re-redeem for loopback. In-memory and process-local — a restart drops it, which fails CLOSED
# (an unknown nonce after a restart is treated as fresh for idempotent loopback, and the proxy
# hop's tokens are 30s-lived so a restart window cannot be replayed into). Capped like the
# proxy's own ``_ticket_bindings`` so a flood can't grow it without bound.
_LEDGER_LIMIT = 8192
_redeemed: dict[str, float] = {}

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


def _signing_key() -> bytes:
    """HMAC key, domain-separated from the loopback-only fleet service token so a delegation
    signature is not interchangeable with anything else signed by that token."""
    from graph.fleet.service_token import resolve_service_token

    return hmac.new(resolve_service_token().encode("utf-8"), _KEY_INFO, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


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
    key: bytes | None = None,
) -> str:
    """Mint a signed delegation bound to one ``audience`` (member slug / loopback target),
    ``method``, ``context`` and ``operation`` (plus the ``task_id`` for the loopback binding).

    The caller must already be an authenticated human operator — the fleet proxy checks
    ``is_human_operator`` before calling this, and a peer caller is never handed a token.
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
    sig = _b64(hmac.new(key or _signing_key(), payload.encode("ascii"), hashlib.sha256).digest())
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
    key: bytes | None = None,
) -> Delegation:
    """Verify a delegation (no ledger side effect) and return it, or raise ``DelegationError``.

    The owner checks, in order: the HMAC signature (forged → refuse), the issuer (foreign →
    refuse), the target ``audience`` (wrong member → refuse), any bound ``method``/``context``/
    ``operation``/``task_id`` the caller pins, the expiry, and the revocation generation. If the
    revocation check cannot be read (raises or returns None) the result is ``RevocationUnavailable``
    — private access fails CLOSED rather than assuming the grant is still live.
    """
    now = time.time() if now is None else now
    try:
        payload, sig = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise DelegationError("malformed delegation") from exc
    expected = _b64(hmac.new(key or _signing_key(), payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise DelegationError("bad signature")  # forged or signed with the wrong key
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, TypeError) as exc:
        raise DelegationError("undecodable delegation") from exc
    if not isinstance(claims, dict):
        raise DelegationError("undecodable delegation")

    issuer = claims.get("iss")
    if issuer not in allowed_issuers:
        raise DelegationError(f"untrusted issuer {issuer!r}")
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
    while len(_redeemed) >= _LEDGER_LIMIT:
        del _redeemed[next(iter(_redeemed))]


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
