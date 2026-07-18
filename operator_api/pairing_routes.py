"""Device pairing + the device registry (ADR 0087).

    POST   /api/pairing/start   → mint a short-TTL pairing code + the URLs it works on
    POST   /api/pairing/cancel  → drop pending codes (operator closed the dialog)
    POST   /api/pairing/claim   → redeem a code for a device token  ← UNAUTHENTICATED
    GET    /api/devices         → list paired devices
    DELETE /api/devices/{id}    → revoke one

Every route here is behind the ``/api/*`` operator bearer (a2a_impl/auth.py) EXCEPT
``claim``, which is on the auth allowlist by necessity — obtaining auth is its purpose. Its
guards live in ``security.devices``: ~190-bit codes, 120s TTL, single-use consumption, and a
failed-attempt counter. See ADR 0087 D4 for the residual risk that buys.

``operator_api`` may import ``security``/``infra`` but never ``server`` (import-linter).
"""

from __future__ import annotations

import ipaddress
import logging
import socket

from fastapi import Request  # module-level so the stringized `request: Request` annotations
# resolve — under `from __future__ import annotations` FastAPI evaluates them against MODULE
# globals, so a function-local import silently turns `request` into a required QUERY param.
# (Same trap fleet_routes.py documents.)

log = logging.getLogger("protoagent.server.pairing")

# The interface the server actually bound to, pushed in by server/__init__ once resolved
# (server → operator_api is the allowed direction; the reverse is not). Enumerating the
# host's interfaces is NOT enough on its own: a loopback-bound server still *has* a LAN
# address, and a QR pointing at it would fail with no explanation.
_BIND_HOST: list[str] = ["127.0.0.1"]

# 0.0.0.0 / :: mean "every interface", so every enumerated address is genuinely reachable.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})

# RFC 6598 shared address space — what Tailscale allocates. Called out explicitly because
# Python classifies it as neither private nor global, so it falls through naive filters.
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


def set_bind_host(host: str) -> None:
    """Record the resolved bind interface (called from the server bootstrap)."""
    _BIND_HOST[0] = (host or "").strip() or "127.0.0.1"


def _reachable(addr: str) -> bool:
    """Can a phone reach this instance on ``addr``, given what we actually bound to?"""
    bind = _BIND_HOST[0]
    if bind in _WILDCARD_BINDS:
        return True
    return addr == bind


def _source_ip_for(target: str) -> str | None:
    """The local address the OS would use to reach ``target``.

    A UDP ``connect`` only fixes the socket's peer — no packet is sent and nothing has to be
    listening — so this is a cheap, dependency-free way to ask the routing table "which of my
    interfaces faces this?". Returns None when there's no route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.2)
        sock.connect((target, 9))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def _local_addresses() -> list[str]:
    """Every local address worth offering as a pairing target.

    Hostname resolution alone is unreliable here — on macOS `gethostbyname_ex` frequently
    resolves only to loopback, which silently produced an EMPTY candidate list and a "bind to
    a reachable address" error on a correctly-bound server. So probe the routing table
    directly, per network, and treat hostname lookup as a supplement rather than the source.
    """
    found: list[str] = []
    # 100.100.100.100 is Tailscale's MagicDNS resolver — routed only when a tailnet is up, so
    # the source address for it IS the tailnet address.
    for target in ("100.100.100.100", "8.8.8.8"):
        ip = _source_ip_for(target)
        if ip and ip not in found:
            found.append(ip)
    # Supplement with hostname resolution — catches secondary interfaces the two probes above
    # don't face (a second LAN, a VPN with its own route).
    try:
        _, _, extra = socket.gethostbyname_ex(socket.gethostname())
        for ip in extra:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def _candidate_hosts() -> list[dict]:
    """Addresses a phone might actually reach this instance on, best first.

    Tailnet before LAN: a tailnet address works from anywhere the operator's devices are,
    survives changing networks, and is already authenticated at the network layer. A LAN
    address only works while both devices sit on the same Wi-Fi.

    Filtered by the real bind (``_reachable``) and never loopback — a QR pointing at
    127.0.0.1 encodes the phone's *own* loopback and can never work. An empty list is the
    honest answer the caller turns into "bind to a reachable address first" (ADR 0087 D6)
    rather than a QR that fails mysteriously.
    """
    addrs = _local_addresses()

    out: list[dict] = []
    seen: set[str] = set()
    for raw in addrs:
        if raw in seen:
            continue
        seen.add(raw)
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not _reachable(raw):
            continue  # the server isn't listening on this interface
        # Classify by ALLOWLIST, not by `is_private`. Tailscale hands out 100.64.0.0/10
        # (RFC 6598 CGNAT), which Python reports as NEITHER is_private NOR is_global — so a
        # `not ip.is_private` filter silently drops the tailnet address, the one most worth
        # offering. Everything not named here (loopback, link-local, and genuinely public
        # addresses) is rejected: advertising a routable address as a "scan me" target is
        # how an instance ends up exposed to the internet.
        if ip in _TAILNET_NET:
            out.append({"host": raw, "kind": "tailnet"})
        elif ip.is_private and not ip.is_loopback and not ip.is_link_local:
            out.append({"host": raw, "kind": "lan"})

    out.sort(key=lambda h: 0 if h["kind"] == "tailnet" else 1)
    return out


def register_pairing_routes(app) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.post("/api/pairing/start")
    async def _start(request: Request):  # noqa: ANN202
        from security.devices import PAIRING_TTL_SECONDS, start_pairing

        hosts = _candidate_hosts()
        if not hosts:
            # Nothing to encode. Say why, and do NOT suggest PROTOAGENT_ALLOW_OPEN — the
            # fix is to bind a reachable address WITH a token, not to open the instance.
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no reachable address — this instance looks bound to loopback. "
                    "Restart it bound to your tailnet or LAN address to pair a device.",
                    "hosts": [],
                },
                status_code=409,
            )
        code, expires_at = start_pairing()
        port = request.url.port or 7870
        # The code rides the FRAGMENT (ADR 0087 D5): fragments are never sent to the server,
        # so it stays out of access logs, proxy logs and Referer headers.
        for host in hosts:
            host["url"] = f"http://{host['host']}:{port}/app/#pair={code}"
        return JSONResponse(
            {"ok": True, "code": code, "expires_at": expires_at, "ttl": PAIRING_TTL_SECONDS, "hosts": hosts}
        )

    @router.post("/api/pairing/cancel")
    async def _cancel():  # noqa: ANN202
        from security.devices import cancel_pairings

        cancel_pairings()
        return JSONResponse({"ok": True})

    @router.post("/api/pairing/claim")
    async def _claim(request: Request):  # noqa: ANN202
        """UNAUTHENTICATED (allowlisted in a2a_impl/auth.py). See the module docstring."""
        from security.devices import claim_pairing

        try:
            body = await request.json()
        except ValueError:
            body = {}
        code = str(body.get("code") or "")
        name = str(body.get("name") or "")
        result = claim_pairing(code, name)
        if result is None:
            # One message for every failure mode (unknown / expired / already used) — an
            # attacker learns nothing about which codes exist.
            return JSONResponse({"ok": False, "error": "invalid or expired pairing code"}, status_code=403)
        device, token = result
        log.info("[pairing] device %s claimed a pairing code", device["id"])
        return JSONResponse({"ok": True, "device": device, "token": token})

    @router.get("/api/devices")
    async def _list():  # noqa: ANN202
        from security.devices import list_devices

        return JSONResponse({"devices": list_devices()})

    @router.delete("/api/devices/{device_id}")
    async def _revoke(device_id: str):  # noqa: ANN202
        from security.devices import revoke_device

        if not revoke_device(device_id):
            return JSONResponse({"ok": False, "error": "unknown device"}, status_code=404)
        return JSONResponse({"ok": True})

    app.include_router(router)
