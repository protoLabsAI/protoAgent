"""Fleet discovery (ADR 0042 §I) — find OTHER protoAgents to add as remote fleet members.

Three channels, merged:
  • **Local port-scan** — probe ``127.0.0.1`` ports for ``/.well-known/agent-card.json`` (a
    co-located agent, e.g. a freshly-spun Roxy on another port).
  • **mDNS / Bonjour** — every agent advertises a ``_protoagent._tcp`` service on boot; we
    browse the LAN for siblings on *other* machines.
  • **Tailnet port-scan** — mDNS is link-local multicast and never crosses a Tailscale
    overlay, so tailnet siblings are found by asking the local ``tailscale`` CLI for online
    peers and probing their agent-cards over the same port range. (A machine on both the
    LAN and the tailnet can surface twice — LAN IP via mDNS, ``100.x`` via this channel;
    both URLs are real, we don't guess which one to keep.)

Pure discovery: returns reachable protoAgents (name + url). Registering one as a remote fleet
member + proxying into it is the supervisor/proxy's job. All best-effort — a discovery hiccup
never blocks the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time
from collections.abc import Iterable

import httpx

log = logging.getLogger(__name__)

_SERVICE_TYPE = "_protoagent._tcp.local."
_zc = None  # the advertised Zeroconf instance (None until advertise())
_info = None

# ── boot-sweep peer cache (ADR 0042 §I) ───────────────────────────────────────
# Peers found by the at-boot background sweep (and every subsequent ``discover()``)
# are remembered here keyed by ``(host, port)`` with a wall-clock timestamp, so the
# FIRST console ``GET /api/fleet/discover`` is instant — siblings that booted
# alongside us are surfaced from the cache while the live scan runs, rather than only
# after a manual rescan. Entries age out after ``_CACHE_TTL_S`` so a peer that goes
# away stops surfacing. Read/written only on the event loop (inside ``discover()`` or
# the sweep task), so no lock is needed.
_peer_cache: dict[tuple, tuple[dict, float]] = {}
_CACHE_TTL_S = 300.0  # cached peers surface for 5 min after they were last seen
_sweep_task = None  # holds the boot-sweep task so it isn't GC'd mid-flight


def _remember(peers: Iterable[dict]) -> None:
    """Stamp ``peers`` into the boot-sweep cache (each with ``time.time()``)."""
    now = time.time()
    for p in peers:
        _peer_cache[(p["host"], p["port"])] = (dict(p), now)


def _cached_peers() -> list[tuple[tuple, dict]]:
    """``(key, peer)`` for cached peers still within TTL; ages the stale ones out."""
    now = time.time()
    live: list[tuple[tuple, dict]] = []
    for key, (peer, ts) in list(_peer_cache.items()):
        if now - ts <= _CACHE_TTL_S:
            live.append((key, peer))
        else:
            _peer_cache.pop(key, None)
    return live


def cached_peers() -> list[dict]:
    """Currently-cached discovery candidates (within TTL). Best-effort, may be stale."""
    return [peer for _, peer in _cached_peers()]

# App-layer defaults for the discovery knobs (Host layer, ADR 0047 D8) — used when no
# live config is loaded (CLI/test context). Mirror the LangGraphConfig dataclass defaults.
_DEFAULT_PORT_RANGE = (7860, 7910)


def _cfg():
    """The live ``LangGraphConfig`` (or ``None`` in a CLI/no-STATE context). Lazy import
    to avoid an import-time cycle. Lets the discovery knobs (port range + mDNS toggle,
    ADR 0047 D8 ``fleet.discovery.*``) be resolved from the Host cascade while keeping
    ``discover``/``advertise`` parametric for tests."""
    try:
        from runtime.state import STATE

        return getattr(STATE, "graph_config", None)
    except Exception:  # noqa: BLE001 — no live config ⇒ the app defaults
        return None


def _mdns_enabled() -> bool:
    cfg = _cfg()
    return bool(getattr(cfg, "discovery_mdns", True)) if cfg is not None else True


def _config_port_range() -> tuple[int, int]:
    cfg = _cfg()
    if cfg is None:
        return _DEFAULT_PORT_RANGE
    return (
        int(getattr(cfg, "discovery_port_min", _DEFAULT_PORT_RANGE[0])),
        int(getattr(cfg, "discovery_port_max", _DEFAULT_PORT_RANGE[1])),
    )


def _local_ip() -> str:
    """Best-effort LAN IP (the address other machines reach us on); 127.0.0.1 if offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet leaves; just selects the egress interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ── mDNS advertise (wired into server startup) ────────────────────────────────
def advertise(name: str, port: int) -> None:
    """Announce this agent on mDNS so LAN siblings can discover it. Idempotent + best-effort.

    Sync zeroconf — call via ``asyncio.to_thread`` from async code (the ``_browse_mdns``
    convention): constructed on a running event loop it attaches to that loop, and
    ``register_service`` then blocks the same loop waiting on its own future — a ~10s
    ``EventLoopBlocked`` stall at boot. Guarded below so a regressed call site logs and
    bails instantly instead.
    """
    global _zc, _info
    if _zc is not None or not port:
        return
    if not _mdns_enabled():  # Host layer (ADR 0047 D8): fleet.discovery.mdns=false ⇒ no advert
        log.info("[discovery] mDNS disabled (fleet.discovery.mdns=false) — not advertising %s", name)
        return
    if _on_event_loop():
        log.warning(
            "[discovery] advertise() called on an event loop thread — refusing "
            "(sync zeroconf would deadlock it); call via asyncio.to_thread"
        )
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf

        ip = _local_ip()
        _info = ServiceInfo(
            _SERVICE_TYPE,
            f"{name}.{_SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=int(port),
            properties={"name": name, "v": "1"},
        )
        _zc = Zeroconf()
        _zc.register_service(_info)
        log.info("[discovery] advertising %s on mDNS (%s:%d)", name, ip, port)
    except Exception:  # noqa: BLE001 — never block boot on mDNS
        log.exception("[discovery] mDNS advertise failed — continuing without it")
        _zc = None


def _on_event_loop() -> bool:
    """True when the current thread is running an asyncio loop — where sync zeroconf
    calls (register/unregister/close all wait on loop-scheduled futures) would deadlock."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def stop_advertise() -> None:
    """Withdraw the advertisement. Sync zeroconf — call via ``asyncio.to_thread`` (see
    ``advertise``); ``unregister``/``close`` block on the loop the same way."""
    global _zc, _info
    if _zc is not None and _on_event_loop():
        log.warning(
            "[discovery] stop_advertise() called on an event loop thread — refusing "
            "(sync zeroconf would deadlock it); call via asyncio.to_thread"
        )
        return
    try:
        if _zc is not None:
            if _info is not None:
                _zc.unregister_service(_info)
            _zc.close()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _zc, _info = None, None


# ── discover ──────────────────────────────────────────────────────────────────
async def _probe(client: httpx.AsyncClient, host: str, port: int) -> dict | None:
    """Is there a protoAgent at host:port? Return {name, url, host, port} from its agent-card."""
    url = f"http://{host}:{port}"
    try:
        r = await client.get(f"{url}/.well-known/agent-card.json", timeout=1.0)
        if r.status_code != 200:
            return None
        card = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return {"name": card.get("name") or f"{host}:{port}", "url": url, "host": host, "port": port}


# macOS App-Store/standalone Tailscale ships the CLI inside the app bundle, not on PATH.
_TAILSCALE_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale_cli() -> str | None:
    """The tailscale CLI to ask about the tailnet, or None when not installed."""
    import shutil

    return shutil.which("tailscale") or (_TAILSCALE_APP_CLI if os.path.exists(_TAILSCALE_APP_CLI) else None)


def _tailnet_peer_ips(timeout: float = 3.0) -> list[str]:
    """IPv4 tailnet addresses of ONLINE peers (sync subprocess — call via
    ``asyncio.to_thread``). Empty when tailscale isn't installed or isn't up — the
    channel just goes quiet, never errors."""
    cli = _tailscale_cli()
    if not cli:
        return []
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return []
        peers = (json.loads(out.stdout).get("Peer") or {}).values()
        return [ip for p in peers if p.get("Online") for ip in (p.get("TailscaleIPs") or []) if "." in ip]
    except Exception:  # noqa: BLE001 — discovery is best-effort, never blocks the endpoint
        log.debug("[discovery] tailscale status failed", exc_info=True)
        return []


async def _scan_tailnet(port_range: tuple[int, int], known: set) -> list[dict]:
    """Probe every online tailnet peer's agent-card over the fleet port range."""
    ips = await asyncio.to_thread(_tailnet_peer_ips)
    if not ips:
        return []
    async with httpx.AsyncClient() as client:
        tasks = [
            _probe(client, ip, p) for ip in ips for p in range(port_range[0], port_range[1] + 1) if (ip, p) not in known
        ]
        return [r for r in await asyncio.gather(*tasks) if r]


async def _scan_local(port_range: tuple[int, int], skip_ports: set[int]) -> list[dict]:
    async with httpx.AsyncClient() as client:
        tasks = [_probe(client, "127.0.0.1", p) for p in range(port_range[0], port_range[1] + 1) if p not in skip_ports]
        return [r for r in await asyncio.gather(*tasks) if r]


def _browse_mdns(timeout: float) -> list[dict]:
    """One-shot LAN browse for `_protoagent._tcp` (sync zeroconf — call via asyncio.to_thread)."""
    found: list[dict] = []
    try:
        import time

        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        zc = Zeroconf()

        class _L(ServiceListener):
            def add_service(self, zc_, type_, name):
                info = zc_.get_service_info(type_, name, timeout=int(timeout * 1000))
                if not info or not info.addresses:
                    return
                ip = socket.inet_ntoa(info.addresses[0])
                props = info.properties or {}
                nm = (props.get(b"name") or b"").decode() or name.split(".")[0]
                found.append({"name": nm, "url": f"http://{ip}:{info.port}", "host": ip, "port": info.port})

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        ServiceBrowser(zc, _SERVICE_TYPE, _L())
        time.sleep(timeout)
        zc.close()
    except Exception:  # noqa: BLE001
        log.exception("[discovery] mDNS browse failed")
    return found


async def _no_results() -> list[dict]:
    """A do-nothing channel — substituted for the mDNS browse when it's disabled."""
    return []


async def discover(
    *,
    known: set | None = None,
    port_range: tuple[int, int] | None = None,
    timeout: float = 1.5,
    mdns: bool | None = None,
) -> list[dict]:
    """Other protoAgents (local + LAN + tailnet) minus the ones already in the fleet.

    ``known`` is a set of ``(host, port)`` already known (the host itself + existing members);
    those are filtered out. Returns ``[{name, url, host, port}]`` — duplicates by
    ``(host, port)`` are collapsed; a dual-homed machine (LAN IP via mDNS + ``100.x``
    via tailnet) intentionally keeps both addresses.

    ``port_range`` and ``mdns`` default to the resolved Host-layer config (ADR 0047 D8
    ``fleet.discovery.*``) when left ``None`` — pass them explicitly in tests."""
    known = known or set()
    if port_range is None:
        port_range = _config_port_range()
    if mdns is None:
        mdns = _mdns_enabled()
    skip_local = {p for (h, p) in known if h in ("127.0.0.1", "localhost")}
    local, network, tailnet = await asyncio.gather(  # independent channels — scan concurrently
        _scan_local(port_range, skip_local),
        asyncio.to_thread(_browse_mdns, timeout) if mdns else _no_results(),
        _scan_tailnet(port_range, known),
    )
    # An mDNS advert carrying THIS machine's own IP is a co-located agent — the same
    # agent the local scan finds at 127.0.0.1:<port>. Normalize it to loopback so the
    # (host, port) dedupe collapses the pair (it surfaced twice otherwise, once per
    # channel). Genuinely-remote LAN/tailnet siblings keep their own addresses.
    own_ip = _local_ip()
    for a in network:
        if a["host"] == own_ip:
            a["host"] = "127.0.0.1"
            a["url"] = f"http://127.0.0.1:{a['port']}"
    out: dict[tuple, dict] = {}
    for a in network + tailnet + local:  # local wins on a url clash (it's the more specific probe)
        if (a["host"], a["port"]) in known:
            continue
        out[(a["host"], a["port"])] = a
    _remember(out.values())  # warm the boot-sweep cache with THIS scan's live hits
    # Merge in still-fresh cached peers (e.g. from the at-boot sweep) that this live scan
    # missed, so the first console open is instant instead of waiting on a manual rescan.
    # Filtered by ``known`` (don't surface a peer already in the fleet) and aged out by TTL.
    for key, peer in _cached_peers():
        if key in known:
            continue
        out.setdefault(key, peer)
    return list(out.values())


# ── boot sweep ─────────────────────────────────────────────────────────────────
async def boot_sweep(
    *,
    known: set | None = None,
    port_range: tuple[int, int] | None = None,
    timeout: float = 1.5,
    mdns: bool | None = None,
) -> list[dict]:
    """One-shot background discovery sweep run at hub boot. Warms ``_peer_cache`` so peers
    that booted alongside us are surfaced on the first ``discover()`` without a manual scan.

    Best-effort: every failure (a channel raising, no network, etc.) is swallowed and logged
    at info — discovery only ever *surfaces* candidates, so a miss never breaks anything."""
    try:
        peers = await discover(known=known, port_range=port_range, timeout=timeout, mdns=mdns)
        log.info("[discovery] boot sweep cached %d peer(s)", len(peers))
        return peers
    except Exception:  # noqa: BLE001 — discovery is best-effort, never blocks/breaks boot
        log.info("[discovery] boot sweep failed — continuing without a warm cache", exc_info=True)
        return []


def start_boot_sweep(**kwargs) -> None:
    """Fire-and-forget the boot sweep on the running loop (idempotent, non-blocking).

    ``discover()`` offloads the sync zeroconf browse to a thread itself, so scheduling the
    coroutine on the loop is safe. A reference is held in ``_sweep_task`` so the task isn't
    garbage-collected mid-flight. No running loop (CLI context) ⇒ skipped, never raises."""
    global _sweep_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("[discovery] start_boot_sweep called without a running loop — skipping")
        return
    if _sweep_task is not None and not _sweep_task.done():
        return  # already sweeping — don't pile on
    _sweep_task = loop.create_task(boot_sweep(**kwargs))
