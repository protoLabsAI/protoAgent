"""Proof that a fenced turn is this process's own detached background job (#1639).

A fenced streaming turn is refused on an ACP runtime (the external agent's toolset can't
be fenced). The ONE exception is a detached registry-subagent job the background manager
fires at its own ``/a2a`` (``BackgroundManager._fire``): those must keep running there.
The fire is an HTTP self-POST, and its request metadata is exactly what any remote A2A
caller can also send — so no metadata VALUE (``origin: background``, a job id, a fence)
can mark a turn as ours. What a remote caller cannot produce is a secret that never left
this process.

So each fire mints a fresh 256-bit token bound to its job id, keeps the expected value
here (in-process memory; this module never writes it to config, disk or logs), and sends
it in the fire's metadata. The token does travel with that metadata — the A2A layer may
persist the request, and the turn's tools can read it via ``request_metadata_scope`` —
but by then it is already spent: the turn entry redeems it with :func:`redeem` BEFORE the
turn runs — constant-time compare, bound to the job id AND the job's dedicated context,
single use (removed on success) — and the fire drops it when its POST returns whatever
happened. A token seen later proves nothing. A process that didn't mint it (a
second worker, a restarted server) holds no entry and fails CLOSED: the turn is refused
on ACP, exactly as an arbitrary fenced caller's is.

Rejected: a static per-process secret (one leak exempts every later caller); loopback +
the A2A bearer (the bearer is the SAME credential remote callers hold, and a fleet hub
proxies onto loopback); an in-process ``contextvar`` (the fire crosses an HTTP hop, so
nothing in-process survives to the turn).
"""

from __future__ import annotations

import hmac
import secrets

# job_id -> token. Bounded by in-flight fires (each entry lives for one POST).
_TOKENS: dict[str, str] = {}

# The request-metadata key the token rides on.
METADATA_KEY = "background_fire_token"


def context_for(job_id: str) -> str:
    """The dedicated A2A context a background job's turn runs in."""
    return f"background:{job_id}"


def mint(job_id: str) -> str:
    """A fresh single-use token for ``job_id``'s fire (replacing any earlier one)."""
    token = secrets.token_urlsafe(32)
    _TOKENS[str(job_id)] = token
    return token


def discard(job_id: str) -> None:
    """Drop ``job_id``'s token (the fire's POST returned, however it ended). Idempotent."""
    _TOKENS.pop(str(job_id), None)


def redeem(request_metadata: dict | None, session_id: str) -> bool:
    """Is this turn the background manager's own fire? True only when the metadata
    carries the job id and the token minted for it, the turn runs in that job's
    dedicated context, and the token is still unredeemed; it is then consumed. Any
    other shape → False."""
    md = request_metadata or {}
    job_id = md.get("background_job_id")
    token = md.get(METADATA_KEY)
    if not isinstance(job_id, str) or not isinstance(token, str) or not job_id or not token:
        return False
    if session_id != context_for(job_id):
        return False
    expected = _TOKENS.get(job_id)
    if expected is None or not hmac.compare_digest(expected.encode(), token.encode()):
        return False
    _TOKENS.pop(job_id, None)
    return True
