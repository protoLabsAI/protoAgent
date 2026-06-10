"""Fleet discovery (ADR 0042 §I) — find OTHER protoAgents to add as remote fleet members.

Two channels, merged:
  • **Local port-scan** — probe ``127.0.0.1`` ports for ``/.well-known/agent-card.json`` (a
    co-located agent, e.g. a freshly-spun Roxy on another port).
  • **mDNS / Bonjour** — every agent advertises a ``_protoagent._tcp`` service on boot; we
    browse the LAN for siblings on *other* machines.

Pure discovery: returns reachable protoAgents (name + url). Registering one as a remote fleet
member + proxying into it is the supervisor/proxy's job. All best-effort — a discovery hiccup
never blocks the server.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import httpx

log = logging.getLogger(__name__)

_SERVICE_TYPE = "_protoagent._tcp.local."
_zc = None  # the advertised Zeroconf instance (None until advertise())
_info = None


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
    if _on_event_loop():
        log.warning("[discovery] advertise() called on an event loop thread — refusing "
                    "(sync zeroconf would deadlock it); call via asyncio.to_thread")
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
        log.warning("[discovery] stop_advertise() called on an event loop thread — refusing "
                    "(sync zeroconf would deadlock it); call via asyncio.to_thread")
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


async def _scan_local(port_range: tuple[int, int], skip_ports: set[int]) -> list[dict]:
    async with httpx.AsyncClient() as client:
        tasks = [_probe(client, "127.0.0.1", p)
                 for p in range(port_range[0], port_range[1] + 1) if p not in skip_ports]
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


async def discover(*, known: set | None = None,
                   port_range: tuple[int, int] = (7860, 7910), timeout: float = 1.5) -> list[dict]:
    """Other protoAgents (local + LAN) minus the ones already in the fleet.

    ``known`` is a set of ``(host, port)`` already known (the host itself + existing members);
    those are filtered out. Returns deduped ``[{name, url, host, port}]``."""
    known = known or set()
    skip_local = {p for (h, p) in known if h in ("127.0.0.1", "localhost")}
    local = await _scan_local(port_range, skip_local)
    network = await asyncio.to_thread(_browse_mdns, timeout)
    out: dict[tuple, dict] = {}
    for a in network + local:  # local wins on a url clash (it's the more specific probe)
        if (a["host"], a["port"]) in known:
            continue
        out[(a["host"], a["port"])] = a
    return list(out.values())
