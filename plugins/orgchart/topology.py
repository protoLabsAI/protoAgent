"""orgChart topology — the crawl engine behind ``GET /api/plugins/orgchart/topology``.

The graph is assembled from three sources, cheapest first:

1. **The effective delegate roster** (ADR 0105): ``plugins.delegates.store.merged_delegates()``
   — this instance's entries ∪ the box's fleet-shared (``scope: host``) entries, with
   secrets overlaid, so a coder registered once on the hub appears (and can be crawled)
   from every member. Falls back to the raw ``langgraph-config.yaml`` list when the
   delegates plugin is unavailable.
2. **The delegates health snapshot** — the background prober already maintains per-name
   ``{ok, latency_ms, checked_at}`` with adaptive backoff; reading it here also signals
   "someone is watching" so its cadence tightens. We never re-probe what it holds.
3. **Fleet-supervised remotes** (hub only): ``graph.fleet.supervisor.list_remotes()`` —
   members the hub supervises but doesn't necessarily delegate to, drawn with a
   ``member`` edge. Their stored bearer also widens the crawl.

Network reads are TTL-cached per peer (positive + negative — an unreachable peer is
remembered for ``_NEG_TTL`` so it can't stall every reload), fetched concurrently per
BFS layer, and the assembled snapshot itself is served stale-while-revalidate: a stale
snapshot returns immediately (marked ``stale: true``) while a single-flight rebuild
runs in the background. Tokens are resolved and used entirely server-side; the payload
carries none.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("protoagent.plugins.orgchart")

# ── defaults (overridable via the manifest `config:` / Settings) ────────────────────
DEFAULTS = {
    "cache_ttl_s": 15,  # topology snapshot freshness window
    "poll_interval_s": 8,  # how often the view re-fetches (served to the client)
    "probe_timeout_s": 3.0,  # per-request read timeout (connect is capped at 2s)
    "max_nodes": 80,
    "include_fleet_members": True,
}

_CARD_TTL = 300.0  # a peer's identity (card) changes rarely
_PEER_TTL = 60.0  # a peer's own delegate list
_NEG_TTL = 30.0  # an unreachable peer — don't hammer it, don't stall on it

# base -> (expires_monotonic, identity|None); None = known-unreachable (negative hit)
_card_cache: dict[str, tuple[float, dict | None]] = {}
# base -> (expires_monotonic, redacted delegate list|None)
_peer_cache: dict[str, tuple[float, list | None]] = {}
# the assembled snapshot: served stale-while-revalidate
_snap: dict = {"expires": 0.0, "data": None}
_build_lock: asyncio.Lock | None = None
_refresh_task: asyncio.Task | None = None


def reset() -> None:
    """Drop every cache — for tests and hot-reload."""
    global _build_lock, _refresh_task
    _card_cache.clear()
    _peer_cache.clear()
    _snap.update(expires=0.0, data=None)
    _build_lock = None
    _refresh_task = None


def _lock() -> asyncio.Lock:
    # Created lazily so the module imports fine outside a running loop.
    global _build_lock
    if _build_lock is None:
        _build_lock = asyncio.Lock()
    return _build_lock


# ── sources ─────────────────────────────────────────────────────────────────────────
def _roster() -> list[dict]:
    """The EFFECTIVE delegate roster (agent ∪ host, secrets overlaid — ADR 0105).
    Reading the raw YAML instead would miss every fleet-shared entry AND every token
    stored in the ``delegate_secrets`` overlay."""
    try:
        from plugins.delegates.store import merged_delegates

        return [e for e in merged_delegates() if isinstance(e, dict)]
    except Exception:  # noqa: BLE001 — delegates plugin unavailable → raw config view
        from graph.config_io import load_yaml_doc

        doc = load_yaml_doc() or {}
        val = doc.get("delegates")
        return [dict(e) for e in val if isinstance(e, dict)] if isinstance(val, list) else []


def _health() -> dict[str, dict]:
    """The delegates plugin's cached health per delegate NAME — never re-probe what the
    background prober already holds (reading it also tightens its cadence)."""
    try:
        from plugins.delegates.health import health_snapshot

        return health_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _fleet_remotes() -> list[dict]:
    """Hub-supervised remote members (ADR 0042) — present only on a hub; token included
    (internal use: it widens the crawl, it never reaches the payload)."""
    try:
        from graph.fleet.supervisor import list_remotes

        return [r for r in list_remotes() if isinstance(r, dict) and r.get("url")]
    except Exception:  # noqa: BLE001 — not a hub / fleet module unavailable
        return []


def _self_identity() -> tuple[str, str, str]:
    """(name, base, role) for the host agent. The base prefers ``A2A_PUBLIC_URL`` (what
    peers would report back, so edge de-dup works), else the local listener."""
    from graph.config_io import load_yaml_doc

    doc = load_yaml_doc() or {}
    name = (doc.get("identity") or {}).get("name") or os.environ.get("AGENT_NAME") or "this agent"
    role = _short(((doc.get("a2a") or {}).get("description") or "").strip()) or "orchestrator"
    base = _norm(os.environ.get("A2A_PUBLIC_URL") or "")
    if not base:
        try:
            from runtime.state import STATE

            base = f"http://127.0.0.1:{STATE.active_port}"
        except Exception:  # noqa: BLE001
            base = "self"
    return name, base, role


# ── helpers ─────────────────────────────────────────────────────────────────────────
def _norm(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/a2a"):
        u = u[:-4]
    return u.rstrip("/")


def _short(s: str, cap: int = 34) -> str:
    """A compact role label from a card/config description: strip a leading "name — "
    label, keep the first clause, cap the length."""
    s = " ".join((s or "").split())
    for sep in (" — ", " - ", ": "):
        i = s.find(sep)
        if 0 < i < 22:
            s = s[i + len(sep) :]
            break
    for stop in (". ", " — ", "; "):
        j = s.find(stop)
        if 0 < j:
            s = s[:j]
            break
    if len(s) > cap:
        s = s[: cap - 1].rstrip() + "…"
    return s


def _token_for(raw: dict) -> str:
    auth = raw.get("auth") or {}
    # The live env var wins over an inline/overlaid token (the pre-v0.2 priority): an
    # entry carrying both fields most likely rotates via the env, and the stored token
    # is the stale copy. The inline token — where merged_delegates() overlays the
    # secret back — is the fallback, not a dead end, when the env var is unset.
    env = auth.get("credentialsEnv")
    if env:
        tok = os.environ.get(env, "")
        if tok:
            return tok
    return str(auth.get("token") or "")


def _a2a_edges(dlist, via: str = "delegate") -> list[dict]:
    """[{name, base, desc, token, via, scope}] for the a2a-type entries in a delegate
    list. Peer-reported lists are redacted (no auth) → token='' → the peer's peers stay
    leaves."""
    out = []
    for d in dlist or []:
        if not isinstance(d, dict) or str(d.get("type")) != "a2a":
            continue
        url = d.get("url")
        if not url:
            continue
        out.append(
            {
                "name": d.get("name") or "",
                "base": _norm(url),
                "desc": (d.get("description") or "").strip(),
                "token": _token_for(d),
                "via": via,
                "scope": str(d.get("scope") or "") or None,
            }
        )
    return out


def _make_client(cfg: dict) -> httpx.AsyncClient:
    # No verify=False: OS/private-CA trust is handled once at boot via truststore
    # (see tests/test_os_trust_store.py — fail closed on genuinely untrusted certs).
    timeout = httpx.Timeout(float(cfg.get("probe_timeout_s", DEFAULTS["probe_timeout_s"])), connect=2.0)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


# ── per-peer fetches (TTL-cached, positive AND negative) ────────────────────────────
async def _identity(client: httpx.AsyncClient, base: str) -> dict | None:
    """Identity + liveness from a peer's PUBLIC surfaces (card, else /healthz).
    ``None`` = unreachable, and that answer is cached for ``_NEG_TTL``."""
    now = time.monotonic()
    hit = _card_cache.get(base)
    if hit and now < hit[0]:
        return hit[1]
    ident: dict | None = None
    t0 = time.monotonic()  # timed by hand — httpx's .elapsed isn't set on every transport
    try:
        r = await client.get(base + "/.well-known/agent-card.json")
        if r.status_code == 200:
            c = r.json()
            ident = {
                "up": True,
                "name": c.get("name") or "",
                "version": c.get("version") or "",
                "role": _short((c.get("description") or "").strip()),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
    except Exception:  # noqa: BLE001 — fall through to /healthz
        pass
    if ident is None:
        t0 = time.monotonic()
        try:
            h = await client.get(base + "/healthz")
            if h.status_code == 200:
                ident = {"up": True, "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception:  # noqa: BLE001
            ident = None
    _card_cache[base] = (now + (_CARD_TTL if ident else _NEG_TTL), ident)
    return ident


async def _peer_delegates(client: httpx.AsyncClient, base: str, token: str) -> list | None:
    """A peer's own delegate list (its redacted ``GET /api/delegates`` view). Needs the
    peer's bearer — held only for this agent's direct delegates and hub remotes."""
    now = time.monotonic()
    hit = _peer_cache.get(base)
    if hit and now < hit[0]:
        return hit[1]
    dels: list | None = None
    try:
        r = await client.get(base + "/api/delegates", headers={"Authorization": "Bearer " + token})
        if r.status_code == 200:
            dels = (r.json() or {}).get("delegates") or []
    except Exception:  # noqa: BLE001
        dels = None
    _peer_cache[base] = (now + (_PEER_TTL if dels is not None else _NEG_TTL), dels)
    return dels


# ── the crawl ───────────────────────────────────────────────────────────────────────
async def _build(cfg: dict) -> dict:
    """One full crawl → topology dict. BFS from this agent; every layer's probes and
    peer-delegate fetches run concurrently; per-peer results are TTL-cached above."""
    max_nodes = int(cfg.get("max_nodes", DEFAULTS["max_nodes"]))
    self_name, self_base, self_role = _self_identity()
    health = _health()  # delegate NAME -> {ok, latency_ms, checked_at}

    seed = _a2a_edges(_roster())
    name_of = {d["base"]: d["name"] for d in seed}  # direct delegates only — health is name-keyed
    tokens = {d["base"]: d["token"] for d in seed if d["token"]}
    if cfg.get("include_fleet_members", DEFAULTS["include_fleet_members"]):
        for rec in _fleet_remotes():
            b = _norm(str(rec.get("url")))
            if b and b != self_base and b not in {d["base"] for d in seed}:
                seed.append(
                    {
                        "name": rec.get("name") or str(rec.get("id") or ""),
                        "base": b,
                        "desc": "fleet member",
                        "token": str(rec.get("token") or ""),
                        "via": "member",
                        "scope": None,
                    }
                )
            if rec.get("token"):
                tokens.setdefault(b, str(rec["token"]))

    nodes: dict[str, dict] = {
        self_base: {"id": self_base, "name": self_name, "role": self_role, "up": True, "version": "", "kind": "self", "url": self_base}
    }
    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()

    def add_edge(a: str, b: str, via: str = "delegate", scope: str | None = None) -> None:
        if a != b and (a, b) not in seen_edges:
            seen_edges.add((a, b))
            e = {"from": a, "to": b, "kind": via}
            if scope:
                e["scope"] = scope
            edges.append(e)

    async with _make_client(cfg) as client:
        frontier: list[tuple[str, list[dict]]] = [(self_base, seed)]
        while frontier and len(nodes) < max_nodes:
            fresh: list[dict] = []
            fresh_bases: set[str] = set()
            for owner, dels in frontier:
                for d in dels:
                    add_edge(owner, d["base"], d.get("via") or "delegate", d.get("scope"))
                    if d["base"] not in nodes and d["base"] not in fresh_bases and len(nodes) + len(fresh) < max_nodes:
                        fresh.append(d)
                        fresh_bases.add(d["base"])
            idents = await asyncio.gather(*[_identity(client, d["base"]) for d in fresh], return_exceptions=True)
            crawlable: list[str] = []
            for d, ident in zip(fresh, idents):
                b = d["base"]
                ident = ident if isinstance(ident, dict) else None
                hs = health.get(name_of.get(b, ""))  # background prober's view, direct delegates only
                node = {
                    "id": b,
                    "name": (ident or {}).get("name") or d["name"] or b,
                    "role": (ident or {}).get("role") or _short(d["desc"]),
                    "up": bool(ident) or bool(hs and hs.get("ok")),
                    "version": (ident or {}).get("version") or "",
                    "kind": "agent",
                    "url": b,
                }
                lat = (hs or {}).get("latency_ms", (ident or {}).get("latency_ms"))
                if lat is not None:
                    node["latency_ms"] = int(lat)
                if hs and hs.get("checked_at"):
                    node["checked_at"] = hs["checked_at"]
                nodes[b] = node
                if node["up"] and tokens.get(b):
                    crawlable.append(b)
            peer_lists = await asyncio.gather(
                *[_peer_delegates(client, b, tokens[b]) for b in crawlable], return_exceptions=True
            )
            frontier = [
                (b, _a2a_edges(pl))
                for b, pl in zip(crawlable, peer_lists)
                if isinstance(pl, list) and pl
            ]

    return {
        "self": self_base,
        "nodes": list(nodes.values()),
        "edges": edges,
        "count": len(nodes),
        "generated_at": time.time(),
        "ttl_s": int(cfg.get("cache_ttl_s", DEFAULTS["cache_ttl_s"])),
        "poll_s": int(cfg.get("poll_interval_s", DEFAULTS["poll_interval_s"])),
    }


async def _rebuild(cfg: dict) -> dict:
    """Single-flight build → snapshot. Callers that only want freshness should check
    the snapshot again after acquiring the lock (another waiter may have built it)."""
    async with _lock():
        if _snap["data"] is not None and time.monotonic() < _snap["expires"]:
            return _snap["data"]
        data = await _build(cfg)
        _snap.update(data=data, expires=time.monotonic() + float(data["ttl_s"]))
        return data


def _kick_refresh(cfg: dict) -> None:
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        return

    async def _run() -> None:
        try:
            await _rebuild(cfg)
        except Exception:  # noqa: BLE001 — a failed refresh keeps serving the stale snapshot
            log.exception("[orgchart] background topology refresh failed")

    _refresh_task = asyncio.create_task(_run())


async def get_topology(cfg: dict, *, force: bool = False) -> dict:
    """The topology snapshot, stale-while-revalidate: fresh → serve it; stale → serve it
    NOW (marked ``stale: true``) and rebuild in the background; absent or ``force`` →
    build inline (single-flight)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    if force:
        _snap.update(expires=0.0)
        _card_cache.clear()
        _peer_cache.clear()
        return {**(await _rebuild(cfg)), "stale": False}
    if _snap["data"] is not None:
        if time.monotonic() < _snap["expires"]:
            return {**_snap["data"], "stale": False}
        stale = _snap["data"]
        _kick_refresh(cfg)
        return {**stale, "stale": True}
    return {**(await _rebuild(cfg)), "stale": False}
