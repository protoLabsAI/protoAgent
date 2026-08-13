"""The hosted-publish wire client (#2179 P2, #2683) — POST a chat-bundle zip to a
configured external service and get back a public link.

Mirrors ``infra/secrets/infisical.py``'s shape (ADR 0080): a typed, never-raise result
with an ``ErrorKind``-style taxonomy, ``httpx`` with an explicit timeout and a ``transport``
test seam. No provider registry here, unlike secrets — there is exactly one publish target
(the endpoint an operator configures), not several pluggable backends.

**The hosted service (#2685) doesn't exist yet** — it's a separate, not-yet-started repo
(deliberately out of protoAgent per the #2179 scoping). ``publish_endpoint_url`` empty is
the expected, honest default until it does: ``publish_bundle`` returns ``NOT_CONFIGURED``
without attempting a network call, the same "capability exists, isn't provisioned" shape
ADR 0094's managed-runtime status uses, rather than crashing or silently no-opping.

The wire contract this client implements (documented for #2685 to build against — see
ADR 0099): ``POST <endpoint_url>`` with the bundle zip bytes as the body
(``Content-Type: application/zip``); a success response is JSON
``{"public_url": str, "revoke_token": str, "expires_at": str | null}``. ``429``/``413``
are treated as a real, informative rejection (quota / bundle too large) rather than a
generic failure, surfacing the response body's ``detail`` when present.

Revocation (#2684) is a SEPARATE configured endpoint (``publish.revoke_endpoint_url``),
not a path convention off ``endpoint_url`` — presuming a URL shape on a service that
doesn't exist yet would be a guess dressed up as a contract. ``POST <revoke_endpoint_url>``
with JSON body ``{"revoke_token": str}``; success is any 2xx.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import httpx


class PublishErrorKind(str, Enum):
    """Why a publish attempt failed — stable vocabulary for the API response and console."""

    NOT_CONFIGURED = "not_configured"  # no publish.endpoint_url set
    REJECTED = "rejected"  # the hosted service's own 4xx (quota, size limit) — informative
    NETWORK = "network"  # connect/TLS/DNS-level failure
    TIMEOUT = "timeout"  # request exceeded timeout_seconds
    BAD_RESPONSE = "bad_response"  # non-2xx (not a REJECTED case) or unparseable body
    INTERNAL = "internal"  # unexpected — anything not covered above


@dataclass
class PublishResult:
    """What ``publish_bundle`` hands back: a public link, or a typed one-line failure."""

    public_url: str | None = None
    revoke_token: str | None = None
    expires_at: str | None = None
    error: str = ""
    error_kind: PublishErrorKind | None = None

    @property
    def ok(self) -> bool:
        return self.public_url is not None


def publish_bundle(
    data: bytes,
    *,
    endpoint_url: str,
    timeout_seconds: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> PublishResult:
    """POST ``data`` (a chat-bundle zip) to ``endpoint_url``. Never raises.

    ``transport`` is a test seam (``httpx.MockTransport``) — ``None`` in production.
    """
    endpoint_url = (endpoint_url or "").strip()
    if not endpoint_url:
        return PublishResult(
            error="hosted publishing isn't configured — set publish.endpoint_url in Settings",
            error_kind=PublishErrorKind.NOT_CONFIGURED,
        )
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            r = client.post(endpoint_url, content=data, headers={"Content-Type": "application/zip"})
        if r.status_code in (429, 413):
            detail = _detail(r)
            reason = "quota exceeded" if r.status_code == 429 else "bundle too large"
            return PublishResult(error=detail or reason, error_kind=PublishErrorKind.REJECTED)
        if r.status_code not in (200, 201):
            return PublishResult(
                error=f"publish endpoint returned HTTP {r.status_code}", error_kind=PublishErrorKind.BAD_RESPONSE
            )
        body = r.json()
        public_url = body["public_url"]
        if not isinstance(public_url, str) or not public_url:
            return PublishResult(
                error="publish endpoint's response had no public_url", error_kind=PublishErrorKind.BAD_RESPONSE
            )
        return PublishResult(
            public_url=public_url,
            revoke_token=body.get("revoke_token"),
            expires_at=body.get("expires_at"),
        )
    except httpx.TimeoutException:
        return PublishResult(
            error=f"timed out after {timeout_seconds:g}s publishing to {endpoint_url}",
            error_kind=PublishErrorKind.TIMEOUT,
        )
    except httpx.HTTPError as e:
        return PublishResult(error=f"network error: {e.__class__.__name__}: {e}", error_kind=PublishErrorKind.NETWORK)
    except (ValueError, KeyError, TypeError) as e:
        return PublishResult(error=f"unexpected response shape: {e}", error_kind=PublishErrorKind.BAD_RESPONSE)
    except Exception as e:  # noqa: BLE001 — the contract is never-raise
        return PublishResult(error=f"{e.__class__.__name__}: {e}", error_kind=PublishErrorKind.INTERNAL)


@dataclass
class RevokeResult:
    """What ``revoke_bundle`` hands back — no payload beyond success/failure, unlike
    publishing there's nothing new to report."""

    ok: bool = False
    error: str = ""
    error_kind: PublishErrorKind | None = None


def revoke_bundle(
    revoke_token: str,
    *,
    endpoint_url: str,
    timeout_seconds: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> RevokeResult:
    """POST ``revoke_token`` to ``endpoint_url`` to un-share a previously published
    bundle. Never raises — same error taxonomy as ``publish_bundle``, reused rather than
    forked since the failure modes (not configured, network, timeout, bad response) are
    identical; ``REJECTED`` here just means the hosted service rejected the token itself
    (already revoked, unknown), not a quota/size limit.
    """
    endpoint_url = (endpoint_url or "").strip()
    if not endpoint_url:
        return RevokeResult(
            error="revocation isn't configured — set publish.revoke_endpoint_url in Settings",
            error_kind=PublishErrorKind.NOT_CONFIGURED,
        )
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            r = client.post(endpoint_url, json={"revoke_token": revoke_token})
        if 200 <= r.status_code < 300:
            return RevokeResult(ok=True)
        detail = _detail(r)
        kind = PublishErrorKind.REJECTED if r.status_code in (400, 404, 409) else PublishErrorKind.BAD_RESPONSE
        return RevokeResult(error=detail or f"revoke endpoint returned HTTP {r.status_code}", error_kind=kind)
    except httpx.TimeoutException:
        return RevokeResult(
            error=f"timed out after {timeout_seconds:g}s revoking at {endpoint_url}",
            error_kind=PublishErrorKind.TIMEOUT,
        )
    except httpx.HTTPError as e:
        return RevokeResult(error=f"network error: {e.__class__.__name__}: {e}", error_kind=PublishErrorKind.NETWORK)
    except Exception as e:  # noqa: BLE001 — the contract is never-raise
        return RevokeResult(error=f"{e.__class__.__name__}: {e}", error_kind=PublishErrorKind.INTERNAL)


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        return str(detail) if detail else ""
    except (ValueError, TypeError):
        return ""
