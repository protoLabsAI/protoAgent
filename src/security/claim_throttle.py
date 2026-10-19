"""The pairing-claim throttle (ADR 0121 D3).

``POST /api/pairing/claim`` is unauthenticated by necessity (it is how a device GETS auth),
so something has to cap how fast anyone can guess at it. Until ADR 0121 that was a lockout:
five misses dropped every pending code. That made the guard itself the attack, because
anyone who could reach the endpoint could cancel the operator's codes at will.

This replaces it with two token buckets that only FAILED claims draw from:

* **per client** (the client IP, resolved through ``network.trusted_proxies``): 5 misses
  per 5 minutes;
* **per ingress class** (``public`` when the Host is the configured public console origin,
  ``private`` otherwise): 30 misses per minute.

A claim that finds either bucket empty is refused with ``429`` + ``Retry-After`` and is
**not evaluated** against any code. So a throttled request is not a guess, and no request,
throttled or not, can make a pending code go away. The codes' entropy (~50 bits, 300 s)
does the real work; the buckets cap cost and log noise.

Process-local by design: pending codes are memory-only, so their guard is too. The clock is
injectable (:func:`reset`) so tests can step time instead of sleeping.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# (capacity, window seconds): the bucket holds `capacity` misses and refills to full over
# `window` — i.e. one miss back every window/capacity seconds.
PER_CLIENT_BUDGET = (5, 300.0)
PER_CLASS_BUDGET = (30, 60.0)

INGRESS_PUBLIC = "public"
INGRESS_PRIVATE = "private"

# A flood of distinct source addresses must not grow the per-client table without bound.
# Full buckets carry no state worth keeping, so they are swept first; past the cap the oldest
# entries go (that address simply starts again with a full bucket).
_MAX_CLIENT_ENTRIES = 10_000


class ClaimThrottled(Exception):
    """A claim refused before evaluation because a bucket is empty."""

    def __init__(self, retry_after: int, bucket: str) -> None:
        super().__init__(f"too many pairing attempts; retry in {retry_after}s")
        self.retry_after = retry_after
        self.bucket = bucket


@dataclass
class _Bucket:
    tokens: float
    stamp: float


class TokenBuckets:
    """A keyed family of token buckets sharing one budget. Not thread-safe on its own; the
    owning :class:`ClaimThrottle` holds a lock."""

    def __init__(self, capacity: int, window: float, clock: Callable[[], float]) -> None:
        self.capacity = float(capacity)
        self.rate = capacity / window  # tokens per second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _level(self, key: str, now: float) -> float:
        b = self._buckets.get(key)
        if b is None:
            return self.capacity
        return min(self.capacity, b.tokens + (now - b.stamp) * self.rate)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` has a whole token again; 0 when it has one now."""
        level = self._level(key, self._clock())
        # The epsilon absorbs float drift: exactly one refill period later is a whole token.
        return 0.0 if level >= 1.0 - 1e-9 else (1.0 - level) / self.rate

    def draw(self, key: str) -> None:
        now = self._clock()
        level = self._level(key, now)
        self._buckets.pop(key, None)  # re-insert at the end: dict order = least recently drawn first
        self._buckets[key] = _Bucket(max(0.0, level - 1.0), now)
        if len(self._buckets) > _MAX_CLIENT_ENTRIES:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        for key in [k for k in self._buckets if self._level(k, now) >= self.capacity]:
            del self._buckets[key]
        while len(self._buckets) > _MAX_CLIENT_ENTRIES:
            del self._buckets[next(iter(self._buckets))]

    def __len__(self) -> int:
        return len(self._buckets)


class ClaimThrottle:
    """The two D3 buckets plus the order they are consulted in."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.monotonic
        self.per_client = TokenBuckets(*PER_CLIENT_BUDGET, clock=self.clock)
        self.per_class = TokenBuckets(*PER_CLASS_BUDGET, clock=self.clock)
        self._lock = threading.RLock()

    def check(self, client: str, ingress: str) -> None:
        """Raise :class:`ClaimThrottled` if either bucket is empty. Draws nothing.

        Both buckets are checked on EVERY claim, before the caller looks at what is pending:
        if a nothing-pending claim skipped the class bucket, a ``403`` vs ``429`` under a
        drained class bucket would tell a prober whether any code is pending."""
        with self._lock:
            for name, buckets, key in (
                ("client", self.per_client, client),
                ("ingress", self.per_class, ingress),
            ):
                wait = buckets.retry_after(key)
                if wait > 0:
                    raise ClaimThrottled(max(1, math.ceil(wait - 1e-6)), name)  # 1.0000000002 s is 1

    def miss(self, client: str, ingress: str, *, anything_pending: bool) -> None:
        """Record a failed claim. A miss with nothing pending draws only per-client (D3): it
        was never compared with anything, so it shouldn't spend the shared class budget."""
        with self._lock:
            self.per_client.draw(client)
            if self.per_client.retry_after(client) > 0:
                logger.warning("[pairing] claim throttle: client %s is out of attempts", client or "(unknown)")
            if anything_pending:
                self.per_class.draw(ingress)
                if self.per_class.retry_after(ingress) > 0:
                    logger.warning("[pairing] claim throttle: %s ingress is out of attempts", ingress)


_THROTTLE = ClaimThrottle()


def throttle() -> ClaimThrottle:
    return _THROTTLE


def reset(clock: Callable[[], float] | None = None) -> ClaimThrottle:
    """Fresh, empty buckets (optionally on an injected clock). Tests and nothing else."""
    global _THROTTLE
    _THROTTLE = ClaimThrottle(clock)
    return _THROTTLE


# ── Ingress class ──────────────────────────────────────────────────────────────────────
# The public console origin (ADR 0121 D2, ``network.public_console_url``) arrives in S7.4.
# Until then nothing sets it and every claim is `private`; the hook exists so the class
# bucket is keyed correctly the day it does.
_PUBLIC_HOST: list[str] = [""]


def set_public_origin(url: str | None) -> None:
    """Record the operator's public console URL; its host (with port) is what a ``public``
    claim's Host header matches. Empty/None clears it."""
    host = ""
    if url:
        parts = urlsplit(url.strip())
        host = (parts.netloc or "").lower().rsplit("@", 1)[-1]
    _PUBLIC_HOST[0] = host


def ingress_class(host_header: str | None) -> str:
    """``public`` when the request's Host is the configured public origin, else ``private``.

    A forged Host only moves a request INTO the public bucket — it can drain that one, never
    the private bucket LAN and tailnet pairing use (D3)."""
    public = _PUBLIC_HOST[0]
    if public and (host_header or "").strip().lower() == public:
        return INGRESS_PUBLIC
    return INGRESS_PRIVATE


# ── Client IP through trusted proxies ──────────────────────────────────────────────────
# Loopback is always trusted, which is uvicorn's own default (FORWARDED_ALLOW_IPS=127.0.0.1)
# and what a same-machine proxy (tailscale serve, cloudflared) needs to key the real client
# rather than one shared 127.0.0.1. A local process forging X-Forwarded-For can only spend
# some other address's misses, which grants it nothing.
_LOOPBACK = ("127.0.0.1", "::1")
_TRUSTED: list[list[str]] = [list(_LOOPBACK)]


def effective_trusted_proxies(configured: Iterable[str] | None) -> list[str]:
    """Loopback plus ``network.trusted_proxies``, de-duplicated, in order."""
    out: list[str] = []
    for item in [*_LOOPBACK, *(configured or [])]:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def set_trusted_proxies(configured: Iterable[str] | None) -> None:
    _TRUSTED[0] = effective_trusted_proxies(configured)


def trusted_proxies() -> list[str]:
    return list(_TRUSTED[0])


def _networks(entries: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(e, strict=False))
        except ValueError:
            logger.warning("[network] ignoring trusted_proxies entry %r (not an IP or CIDR)", e)
    return nets


def _is_trusted(host: str, nets) -> bool:  # noqa: ANN001
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr.version == n.version and addr in n for n in nets)


def resolve_client_ip(peer: str | None, forwarded_for: Iterable[str], trusted: Iterable[str] | None = None) -> str:
    """The client a claim is keyed on.

    ``X-Forwarded-For`` counts only when the TCP peer is a trusted proxy; the chain is then
    walked right to left past trusted hops, and the first untrusted address is the client.
    This is the same rule uvicorn's ``ProxyHeadersMiddleware`` applies with the same list, so
    running it again on a request uvicorn already rewrote changes nothing."""
    entries = trusted_proxies() if trusted is None else effective_trusted_proxies(trusted)
    trust_all = "*" in entries  # uvicorn's "trust everyone"; only sane behind a fenced proxy
    nets = _networks(e for e in entries if e != "*")

    def ok(host: str) -> bool:
        return trust_all or _is_trusted(host, nets)

    peer = (peer or "").strip()
    if not peer or not ok(peer):
        return peer
    hops = [h.strip() for value in forwarded_for for h in str(value).split(",") if h.strip()]
    if not hops:
        return peer
    for hop in reversed(hops):
        if not ok(hop):
            return hop
    return hops[0]
