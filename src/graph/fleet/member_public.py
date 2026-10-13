"""Member-public deferral for the fleet proxy (#1890).

A plugin view page is auth-exempt public *chrome* on the instance that serves it
(``graph/plugins/manifest.py::_view_public_paths``): the console iframes it with
a plain navigation that cannot carry the operator bearer. Through the hub the
same page lives at ``/agents/<slug>/plugins/<id>/…`` — a path the HUB's public
list knows nothing about (it is built from the hub's own manifests, and a member
may run plugins the hub doesn't). So the hub defers the decision to the member:
every instance serves its live public-prefix list on a ``/.well-known`` endpoint
(public by definition — the listed paths are anonymously reachable anyway), and
the hub checks the slug-stripped path against the MEMBER's list, cached per slug.

Fail-closed: an unreachable member or a bad payload yields no prefixes, so the
hub falls back to normal bearer auth (the pre-#1890 behavior). The auth
middleware scopes consultation to plugin-namespace paths, entries here are
re-validated against the same namespace shape, and the proxy never lends a
stored remote bearer to a request admitted this way — a member can only ever
open its own ``/plugins/<id>/…`` subtree on the hub, exactly as far as it
already opens it to direct callers.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger("protoagent.server")

# Served by every instance (server bootstrap); fetched by the hub below.
WELL_KNOWN_PATH = "/.well-known/protoagent/public-paths"

# Same namespace shape the auth gate enforces on its own list (a2a_impl/auth.py).
_PLUGIN_NS_RE = re.compile(r"^/(?:api/)?plugins/[^/]+/")

_TTL = 30.0  # how long a member's fetched list is trusted
_NEG_TTL = 5.0  # unreachable / bad member — retry soon, don't hammer
# A MISS on an entry older than this may force ONE revalidating refetch — the
# enable→click race (ADR 0096 D8): enabling a view plugin on a member hot-mounts
# its page and the console's push-refresh lands the rail icon instantly, but the
# hub's cached (pre-enable) list 401'd the brand-new page for up to _TTL. A miss
# on a fresh-enough entry stays an answer, so misses can't hammer the member.
_MISS_REVALIDATE_FLOOR = 2.0
_MAX_PREFIXES = 256  # cap a hostile/buggy member's list

# slug -> (expires_at_monotonic, fetched_at_monotonic, prefixes)
_cache: dict[str, tuple[float, float, tuple[str, ...]]] = {}


async def member_public_prefixes(slug: str, *, force: bool = False) -> tuple[str, ...]:
    """The member's live plugin public-prefix list, TTL-cached; ``()`` when unknown.
    ``force=True`` bypasses a live cache entry (the miss-revalidate path)."""
    now = time.monotonic()
    hit = _cache.get(slug)
    if hit and now < hit[0] and not force:
        return hit[2]

    from graph.fleet import proxy

    target = proxy._target_for_slug(slug)
    prefixes: tuple[str, ...] = ()
    ttl = _NEG_TTL
    if target is not None:
        base = target[0]  # never send stored credentials — the endpoint is public
        try:
            resp = await proxy._get_client().get(f"{base}{WELL_KNOWN_PATH}", timeout=httpx.Timeout(3.0, connect=2.0))
            if resp.status_code == 200:
                raw = resp.json().get("public_paths")
                if not isinstance(raw, list):
                    raw = []
                prefixes = tuple(s for s in (str(p) for p in raw[:_MAX_PREFIXES]) if _PLUGIN_NS_RE.match(s))
                ttl = _TTL
        except Exception as exc:  # noqa: BLE001 — unreachable/bad member = no prefixes (fail closed)
            log.debug("[fleet] public-paths fetch for %r failed: %s", slug, exc)
    _cache[slug] = (now + ttl, now, prefixes)
    return prefixes


async def is_member_public(slug: str, rest: str) -> bool:
    """Would the member at ``slug`` serve ``rest`` anonymously? (the auth resolver, #1890)"""
    if any(rest.startswith(p) for p in await member_public_prefixes(slug)):
        return True
    # Miss → bounded revalidate: the member's list may have GROWN since the fetch
    # (a plugin enabled seconds ago — the ADR 0096 enable→click race). Re-ask the
    # member once, at most every _MISS_REVALIDATE_FLOOR per slug; a genuinely
    # private path still fails closed, just one refetch later.
    hit = _cache.get(slug)
    if hit is not None and time.monotonic() - hit[1] < _MISS_REVALIDATE_FLOOR:
        return False
    return any(rest.startswith(p) for p in await member_public_prefixes(slug, force=True))
