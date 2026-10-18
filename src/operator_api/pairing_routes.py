"""Device pairing + the device registry (ADR 0087).

    POST   /api/pairing/start   → mint a short-TTL pairing code + the URLs it works on
                                  (body ``{"kind": "agent"}`` → a typeable agent code, ADR 0113)
    POST   /api/pairing/cancel  → drop pending codes (operator closed the dialog; optional
                                  ``{"kind": …}`` scopes it to that dialog's kind)
    POST   /api/pairing/claim   → redeem a code for a device token  ← UNAUTHENTICATED
    GET    /api/pairing/reach   → how this instance can be reached: bind, token, Tailscale,
                                  and whether the panel can restart it (#4336 S4)
    POST   /api/pairing/tailscale/serve    → serve this instance over tailnet HTTPS
                                             (refused unless a token is configured)
    DELETE /api/pairing/tailscale/serve    → stop serving it (only OUR handler)
    GET    /api/devices         → list paired devices
    PATCH  /api/devices/{id}    → rename one (body ``{"name": …}``)
    DELETE /api/devices/{id}    → revoke one

Every route here is behind the ``/api/*`` operator bearer (a2a_impl/auth.py) EXCEPT
``claim``, which is on the auth allowlist by necessity — obtaining auth is its purpose. Its
guards live in ``security.devices``: ~50-bit typeable codes of either kind (300s),
single-use consumption, and one failed-attempt counter shared by both kinds. See ADR 0087 D4
(and its 2026-10-18 amendment) and ADR 0113 D2 for the residual risk that buys.

``operator_api`` may import ``security``/``infra`` but never ``server`` (import-linter).
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import socket
import time

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


# Addresses served IN ADDITION to the bind. A specific-interface bind also listens on
# loopback (server/bind.py) so the local console keeps working; this records what actually
# bound, so "is loopback served?" (tailscale serve proxies there) has a true answer.
_EXTRA_LISTEN: list[str] = []

# `--host` on the command line beats `network.bind`, and a restart re-execs with the same
# argv — so a bind picked in the panel would never apply. Recorded so the panel can say so.
_BIND_PINNED: list[str] = [""]

# What the bind was ASKED to be at boot. Differs from `_BIND_HOST` when server/bind.py fell
# back to loopback because the picked IP was gone; "restart pending" compares against this,
# so a fallback doesn't read as "restart to apply" forever.
_BIND_REQUESTED: list[str] = [""]


def set_bind_host(
    host: str, *, extra: list[str] | None = None, pinned_by_flag: bool = False, requested: str | None = None
) -> None:
    """Record the resolved bind interface (called from the server bootstrap). ``extra`` is
    any additional address actually listened on; ``pinned_by_flag`` says ``--host`` set it;
    ``requested`` is what the bind was asked to be, when a fallback changed it."""
    _BIND_HOST[0] = (host or "").strip() or "127.0.0.1"
    _BIND_REQUESTED[0] = (requested or "").strip() or _BIND_HOST[0]
    _EXTRA_LISTEN[:] = [a for a in (extra or []) if a]
    _BIND_PINNED[0] = _BIND_HOST[0] if pinned_by_flag else ""


def bind_host() -> str:
    """The resolved bind interface — what uvicorn was actually given, not what config
    says (an explicit ``--host`` wins over ``network.bind``). Read by the runtime-status
    posture warning (#2147)."""
    return _BIND_HOST[0]


def _reachable(addr: str) -> bool:
    """Can a phone reach this instance on ``addr``, given what we actually bound to?"""
    bind = _BIND_HOST[0]
    if bind in _WILDCARD_BINDS:
        return True
    return addr == bind or addr in _EXTRA_LISTEN


def _loopback_served() -> bool:
    """Does this instance answer on 127.0.0.1? ``tailscale serve`` proxies there, so an
    HTTPS tailnet address only works when it does."""
    bind = _BIND_HOST[0]
    if bind in _WILDCARD_BINDS or bind in ("localhost", "127.0.0.1"):
        return True
    try:
        if ipaddress.ip_address(bind).is_loopback:
            return True
    except ValueError:
        pass
    return "127.0.0.1" in _EXTRA_LISTEN


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


# Probed per network rather than resolved from the hostname. Each is a routing-table lookup
# that returns in microseconds; hostname resolution was measured at 5.006s BEFORE raising on
# a normal macOS box (no DNS entry for the .local name), which froze the whole event loop.
# The RFC1918 targets don't have to exist — we only ask which interface *would* face them, so
# a machine on several subnets still reports each source address.
_PROBE_TARGETS = (
    "100.100.100.100",  # Tailscale MagicDNS — routed only when a tailnet is up
    "8.8.8.8",  # the default route (primary LAN)
    "192.168.0.1",
    "192.168.1.1",
    "10.0.0.1",
    "172.16.0.1",
)

# Interfaces change on the order of minutes (joining Wi-Fi, tailnet up/down), so a short
# cache makes repeated "Add a device" clicks instant without going stale in practice.
_ADDR_CACHE: dict[str, object] = {"at": 0.0, "addrs": []}
_ADDR_TTL_SECONDS = 15.0


def _local_addresses() -> list[str]:
    """Every local address worth offering as a pairing target.

    Hostname resolution is deliberately NOT used: `gethostbyname_ex` frequently resolves to
    loopback only (which silently produced an empty candidate list on a correctly-bound
    server) and on a box with no DNS entry for its own name it blocks for 5s before raising.
    The routing-table probes below answer the same question in microseconds and are what
    actually found the tailnet address.
    """
    now = time.monotonic()
    cached = _ADDR_CACHE.get("addrs")
    if cached and now - float(_ADDR_CACHE["at"]) < _ADDR_TTL_SECONDS:  # type: ignore[arg-type]
        return list(cached)  # type: ignore[arg-type]

    found: list[str] = []
    for target in _PROBE_TARGETS:
        ip = _source_ip_for(target)
        if ip and ip not in found:
            found.append(ip)
    _ADDR_CACHE["at"] = now
    _ADDR_CACHE["addrs"] = found
    return found


def _qr_svg(url: str) -> str | None:
    """An inline SVG QR for ``url``, or None if rendering fails.

    Rendered SERVER-SIDE and embedded in the start response rather than served from a
    ``GET /api/pairing/qr.svg?code=…`` endpoint — a code in a query string lands in access
    logs, proxy logs and browser history, which is the exact leak the fragment design
    avoids (ADR 0087 D5). It also keeps the console free of a QR library and the pairing URL
    assembled in exactly one place.

    Returns None rather than raising: a missing QR degrades to "type this URL", which still
    works. Losing the whole pairing flow because an optional render failed would not.
    """
    try:
        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=10, border=2, image_factory=qrcode.image.svg.SvgPathImage)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image().save(buf)
        svg = buf.getvalue().decode("utf-8")
        # Strip the XML prolog. qrcode emits a standalone SVG document, but this is injected
        # INLINE into HTML, where `<?xml …?>` is not valid markup — the browser's HTML parser
        # chokes and the whole element silently fails to render (the QR box came up empty).
        start = svg.find("<svg")
        return svg[start:] if start >= 0 else svg
    except Exception:  # noqa: BLE001 — an optional visual must never break pairing
        log.exception("[pairing] QR render failed; falling back to the URL alone")
        return None


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
    return [h for h in _pairable_addresses() if _reachable(h["host"])]


def _pairable_addresses() -> list[dict]:
    """Every address that is a VALID pairing target on this machine, ignoring what the
    server currently binds to.

    Split out from `_candidate_hosts` so a loopback-bound instance can still answer "here
    is what you *could* expose" — otherwise the console dead-ends on an error with nothing
    actionable in it, which is exactly how the desktop app (which binds 127.0.0.1 by design)
    left pairing unusable.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in _local_addresses():
        if raw in seen:
            continue
        seen.add(raw)
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
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


def _tailscale_offers(port: int, ip_hosts: list[dict]) -> tuple[list[dict], dict]:
    """The Tailscale NAME addresses for this instance, best first, plus the status the panel
    shows (#4336 S4, ADR 0121 D1).

    - ``https://<machine>.<tailnet>.ts.net`` when ``tailscale serve`` already proxies HTTPS
      to this instance — works with a localhost bind, survives IP changes, and is a secure
      context on the phone.
    - ``http://<machine>.<tailnet>.ts.net:<port>`` when MagicDNS is on and a tailnet IP is
      directly reachable (the bind covers it) — the name survives IP changes, but it's http.

    Never raises: no CLI, a stopped daemon or a slow answer just means no name addresses."""
    from infra import tailscale

    st = tailscale.status()
    info: dict = st.as_dict()
    info["served"] = None
    info["https_admin_url"] = tailscale.HTTPS_ADMIN_URL
    out: list[dict] = []
    if not (st.available and st.running and st.dns_name):
        return out, info
    served = tailscale.served_url(port) if _loopback_served() else None
    info["served"] = served
    if served:
        out.append(
            {
                "host": st.dns_name,
                "kind": "tailnet",
                "via": "serve",
                "secure": True,
                "funnel": bool(served.get("funnel")),
                "origin": served["url"],
            }
        )
    tailnet_ip_reachable = any(h["kind"] == "tailnet" for h in ip_hosts)
    if st.magicdns and tailnet_ip_reachable:
        out.append(
            {"host": st.dns_name, "kind": "tailnet", "via": "magicdns", "origin": f"http://{st.dns_name}:{port}"}
        )
    return out, info


def _offer_hosts(port: int) -> tuple[list[dict], dict]:
    """Every address to offer, in ADR 0121 D1 order: tailnet HTTPS → tailnet MagicDNS
    name → tailnet IP → LAN IP. Each carries ``origin`` (scheme://host[:port]); the
    second value is the Tailscale status for the panel. Blocking — run in a thread."""
    ips = [dict(h, via="ip", origin=f"http://{h['host']}:{port}") for h in _candidate_hosts()]
    names, ts_info = _tailscale_offers(port, ips)
    return names + ips, ts_info


def _restart_info() -> dict:
    """Can the panel restart this instance so a new bind applies — and if not, what to do.

    The restart itself is ``POST /api/restart`` (operator_api.runtime_routes): drain, then
    re-exec with the same argv. That covers ``python -m server``/``protoagent``, the desktop
    sidecar and a container's PID 1 alike. What it can't fix: a ``--host`` flag (re-exec keeps
    it, and it beats ``network.bind``), and a container, whose reachable ports are the ones
    the container runtime publishes — not this process's bind."""
    import os

    from runtime.state import STATE

    supported = getattr(STATE, "uvicorn_server", None) is not None
    hints: list[str] = []
    if not supported:
        hints.append("This process can't restart itself. Stop it and start it again the way you launched it.")
    pinned = _BIND_PINNED[0]
    if pinned:
        hints.append(
            f"This agent was started with --host {pinned}, which overrides the bind setting. "
            "Start it again without --host (or with the address you want) for a new bind to apply."
        )
    container = os.path.exists("/.dockerenv")
    if container:
        hints.append(
            "This agent runs in a container: phones reach the ports the container publishes, "
            "not this bind. Publish the port in your compose file instead (see the phone-access guide)."
        )
    hint = " ".join(hints)
    configured = ""
    cfg = getattr(STATE, "graph_config", None)
    if cfg is not None:
        configured = str(getattr(cfg, "bind_host", "") or "").strip()
    return {
        "supported": supported,
        "bind_pinned": pinned,
        "container": container,
        "hint": hint,
        # The saved bind differs from the live one: a restart is still owed.
        "pending": bool(configured) and configured != (_BIND_REQUESTED[0] or _BIND_HOST[0]) and not pinned,
    }


def _server_port(request: Request) -> int:
    """The port this process LISTENS on. Not ``request.url.port``: through ``tailscale
    serve`` the Host is ``<name>.ts.net`` on 443, which says nothing about our socket."""
    server = request.scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) == 2 and server[1]:
        return int(server[1])
    return request.url.port or 7870


async def _json_body(request: Request) -> dict:
    """The request's JSON object, or ``{}`` for an empty / non-JSON / non-object body — the
    console's pre-ADR-0113 calls send no body at all, and that must keep meaning "device"."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _requested_kind(body: dict) -> str | None:
    """``"agent"`` / ``"device"`` from an operator-authed body, else None (unknown → caller's
    default). Never consulted on the UNAUTHENTICATED claim route: a code's kind is fixed at
    mint time (ADR 0113 D2)."""
    from security.devices import KIND_AGENT, KIND_DEVICE

    kind = str(body.get("kind") or "").strip().lower()
    return kind if kind in (KIND_AGENT, KIND_DEVICE) else None


# The refusal a phone code gets on an instance that serves no console. Shared wording so the
# CLI and the Devices panel (a hub console can open a `--ui none` member's Devices) say the
# same thing.
NO_CONSOLE_ERROR = (
    "This agent runs with --ui none, so it serves no console for a phone to open. "
    "Restart it with --ui console (the default) to pair a phone; agent pairing works either way."
)


def _console_served() -> bool:
    """Whether this process mounts the console SPA at ``/app``.

    ``server`` resolves the UI tier from ``--ui`` / ``PROTOAGENT_UI`` / the deprecated
    ``--headless`` and mirrors the FINAL value into ``PROTOAGENT_UI`` before any request is
    served (``graph/config_load.py`` reads it the same way). Read it from there: this
    package may not import ``server``. Unset means a process that never went through that
    resolution (tests, an embedder) — the default tier, ``console``.
    """
    import os

    return os.environ.get("PROTOAGENT_UI", "console").strip().lower() != "none"


def _default_agent_name() -> str:
    """This agent's name when the server didn't inject its resolver. Mirrors
    ``server.agent_name`` (identity.name → ``AGENT_NAME`` → "protoagent"), which this
    package may not import."""
    import os

    from runtime.state import STATE

    cfg = STATE.graph_config
    name = getattr(cfg, "identity_name", "") if cfg else ""
    if name and name != "protoagent":
        return name
    return os.environ.get("AGENT_NAME", "protoagent")


def register_pairing_routes(app, agent_name=None) -> None:
    """Mount the pairing + devices routes.

    ``agent_name`` is the server's own name resolver (the one the A2A card uses), injected
    because ``operator_api`` can't import ``server``. The agent-code start response carries
    it so a hub can default the remote's display name to what its card already says.
    """
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()
    resolve_name = agent_name or _default_agent_name

    @router.post("/api/pairing/start")
    async def _start(request: Request):  # noqa: ANN202
        from security.devices import (
            AGENT_PAIRING_TTL_SECONDS,
            KIND_AGENT,
            KIND_DEVICE,
            PAIRING_TTL_SECONDS,
            start_pairing,
        )

        kind = _requested_kind(await _json_body(request)) or KIND_DEVICE

        if kind == KIND_DEVICE and not _console_served():
            # A phone code is redeemed BY the console (`…/app/#pair=` or the AuthGate's typed
            # path). With `--ui none` there is no `/app` to open, so a code would be a promise
            # nothing can keep — refuse before minting one. Agent codes are claimed by a hub's
            # server over the API and need no console, so they fall through.
            return JSONResponse(
                {"ok": False, "reason": "no_console", "error": NO_CONSOLE_ERROR, "hosts": [], "available": []},
                status_code=409,
            )

        # Socket probes + QR rendering are blocking. Run them in a worker thread: a sync
        # call here stalls the ENTIRE event loop, which is how a 5s hostname lookup managed
        # to freeze every other request on the server, not just this one.
        port = _server_port(request)
        hosts, ts_info = await asyncio.to_thread(_offer_hosts, port)
        from a2a_impl.auth import bearer_configured

        reach = {
            "tailscale": ts_info,
            "restart": _restart_info(),
            "auth_configured": bearer_configured(),
            # This server co-binds loopback beside a picked interface (server/bind.py), so the
            # panel may write that IP rather than 0.0.0.0. An older server omits it.
            "bind_interface": True,
        }
        if not hosts:
            # Nothing to encode — but don't dead-end. Same answer for BOTH kinds: a hub on
            # another machine can't reach a loopback-bound instance any more than a phone can.
            # Report what this machine COULD be reached on so the console can offer to bind
            # there (the desktop app ships loopback-only by design, which made pairing
            # unusable in exactly the place it was asked for). Still no PROTOAGENT_ALLOW_OPEN
            # suggestion: the fix is a reachable bind WITH a token, never an open instance.
            available = await asyncio.to_thread(_pairable_addresses)

            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "This agent only listens on localhost, so another agent can't reach it."
                        if kind == KIND_AGENT
                        else "This agent only listens on localhost, so a phone can't reach it."
                    ),
                    "hosts": [],
                    "available": available,
                    "bind": _BIND_HOST[0],
                    # Whether THIS SERVER has a token, so the console can decide to mint one
                    # from fact rather than from what its own localStorage happens to hold.
                    # Those diverge whenever a token is rotated or removed, and acting on the
                    # wrong one writes a bind the server then refuses to boot with.
                    "auth_configured": bearer_configured(),
                    # Tailscale + restart facts, so the panel can offer "Enable HTTPS via
                    # Tailscale" (no bind change at all) before "listen on my network".
                    "reach": reach,
                },
                status_code=409,
            )
        if kind == KIND_AGENT:
            # A hub pairs against a BASE URL (it POSTs /api/pairing/claim there itself), so
            # each host carries that — no QR, no fragment: the code is typed, not scanned.
            code, expires_at = start_pairing(KIND_AGENT)
            for host in hosts:
                host["url"] = host["origin"]
            return JSONResponse(
                {
                    "ok": True,
                    "kind": KIND_AGENT,
                    "code": code,
                    "expires_at": expires_at,
                    "ttl": AGENT_PAIRING_TTL_SECONDS,
                    "hosts": hosts,
                    "name": resolve_name(),
                    "reach": reach,
                }
            )
        code, expires_at = start_pairing()

        # The code rides the FRAGMENT (ADR 0087 D5): fragments are never sent to the server,
        # so it stays out of access logs, proxy logs and Referer headers. The QR and the
        # typed path carry the SAME short code (ADR 0087 D3 amendment): `base_url` is what a
        # phone that can't scan opens before typing the code into "Have a pairing code?".
        def _render() -> None:
            for host in hosts:
                host["base_url"] = f"{host['origin']}/app/"
                host["url"] = f"{host['base_url']}#pair={code}"
                host["qr"] = _qr_svg(host["url"])

        await asyncio.to_thread(_render)
        return JSONResponse(
            {
                "ok": True,
                "kind": KIND_DEVICE,
                "code": code,
                "expires_at": expires_at,
                "ttl": PAIRING_TTL_SECONDS,
                "hosts": hosts,
                "reach": reach,
            }
        )

    @router.get("/api/pairing/reach")
    async def _reach(request: Request):  # noqa: ANN202
        """How this instance can be reached right now — what the "Add a device" dialog's
        reach section renders, and what it re-reads after enabling serve or a restart."""
        from a2a_impl.auth import bearer_configured

        port = _server_port(request)
        hosts, ts_info = await asyncio.to_thread(_offer_hosts, port)
        available = await asyncio.to_thread(_pairable_addresses)
        return JSONResponse(
            {
                "bind": _BIND_HOST[0],
                "port": port,
                "hosts": hosts,
                "available": available,
                "auth_configured": bearer_configured(),
                "tailscale": ts_info,
                "restart": _restart_info(),
                "bind_interface": True,
            }
        )

    @router.post("/api/pairing/tailscale/serve")
    async def _serve_on(request: Request):  # noqa: ANN202
        """Serve this instance over tailnet HTTPS with ``tailscale serve --bg``.

        SECURITY: serve's requests reach us from 127.0.0.1. Auth never trusts loopback (the
        bearer is checked on every request, ``a2a_impl.auth`` R5), so with a token set a
        proxied request without one is a 401 — but an instance with NO token answers
        everyone as operator, and serve would hand that to the whole tailnet. So: refused
        until a bearer token is configured. (``a2a_impl.hosts`` also refuses serve-forwarded
        requests on a tokenless loopback instance, for a token removed later.)"""
        from a2a_impl.auth import bearer_configured
        from infra import tailscale

        if not bearer_configured():
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "no_token",
                    "error": (
                        "Set an auth token first. Tailscale forwards requests from this machine, "
                        "so without a token anyone on your tailnet would get full control of this agent."
                    ),
                },
                status_code=409,
            )
        if not _loopback_served():
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "no_loopback",
                    "error": "This agent isn't listening on 127.0.0.1, which is where Tailscale forwards to.",
                },
                status_code=409,
            )
        result = await asyncio.to_thread(tailscale.enable_serve, _server_port(request))
        return JSONResponse(result.as_dict(), status_code=200 if result.ok else 409)

    @router.delete("/api/pairing/tailscale/serve")
    async def _serve_off(request: Request):  # noqa: ANN202
        """Stop serving THIS instance over tailnet HTTPS. Other serve config is untouched."""
        from infra import tailscale

        result = await asyncio.to_thread(tailscale.disable_serve, _server_port(request))
        return JSONResponse(result.as_dict(), status_code=200 if result.ok else 409)

    @router.post("/api/pairing/cancel")
    async def _cancel(request: Request):  # noqa: ANN202
        from security.devices import cancel_pairings

        # No kind → drop everything (the pre-ADR-0113 console sends no body). A kind scopes
        # it to the dialog that closed, so the phone and agent flows can't cancel each other.
        cancel_pairings(_requested_kind(await _json_body(request)))
        return JSONResponse({"ok": True})

    @router.post("/api/pairing/claim")
    async def _claim(request: Request):  # noqa: ANN202
        """UNAUTHENTICATED (allowlisted in a2a_impl/auth.py). See the module docstring."""
        from security.devices import claim_pairing

        body = await _json_body(request)
        # Deliberately NOT read: body["kind"]. The device's kind is the code's kind, fixed
        # when the operator minted it — a claimer can't relabel itself (ADR 0113 D2).
        code = str(body.get("code") or "")
        name = str(body.get("name") or "")
        result = claim_pairing(code, name)
        if result is None:
            # One message for every failure mode (unknown / expired / already used) — an
            # attacker learns nothing about which codes exist.
            return JSONResponse({"ok": False, "error": "invalid or expired pairing code"}, status_code=403)
        device, token = result
        log.info("[pairing] %s %s claimed a pairing code", device["kind"], device["id"])
        return JSONResponse({"ok": True, "device": device, "token": token})

    @router.get("/api/devices")
    async def _list():  # noqa: ANN202
        from security.devices import list_devices

        return JSONResponse({"devices": list_devices()})

    @router.patch("/api/devices/{device_id}")
    async def _rename(device_id: str, request: Request):  # noqa: ANN202
        from security.devices import rename_device

        body = await _json_body(request)
        # `detail` alongside `error`: the console's request() surfaces FastAPI's `detail`, so
        # the operator sees "name must be 64 characters or fewer", not a JSON blob.
        try:
            device = rename_device(device_id, body.get("name"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "detail": str(exc)}, status_code=400)
        if device is None:
            return JSONResponse({"ok": False, "error": "unknown device", "detail": "unknown device"}, status_code=404)
        return JSONResponse({"ok": True, "device": device})

    @router.delete("/api/devices/{device_id}")
    async def _revoke(device_id: str):  # noqa: ANN202
        from security.devices import revoke_device

        if not revoke_device(device_id):
            return JSONResponse({"ok": False, "error": "unknown device"}, status_code=404)
        return JSONResponse({"ok": True})

    app.include_router(router)
