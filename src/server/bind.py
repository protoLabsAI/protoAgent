"""Listen on the interface the operator picked, AND on loopback (#4336 S4, #2147).

uvicorn binds ONE host. Pinning ``network.bind`` to one interface (the tailnet IP, a LAN
IP) used to stop serving ``127.0.0.1`` — locking the desktop app's webview, which reaches its
sidecar over loopback, out of its own console. So the Devices panel bound ``0.0.0.0`` (every
interface) instead of the address the operator actually chose.

This opens the sockets ourselves and hands them to ``uvicorn.Server.run(sockets=…)``:

- a non-loopback **IP literal** bind listens on that IP **plus** ``127.0.0.1`` — exactly
  "reachable on my tailnet (or Wi-Fi) and locally, nothing else";
- wildcard, loopback and hostname binds are left to uvicorn, as before;
- an IP that is **no longer on this machine** (DHCP moved the LAN address, the tailnet isn't
  up yet at login) falls back to loopback only with a loud warning, instead of a server that
  won't start — the safe direction, and the operator can still open the console to fix it;
- if loopback itself can't be bound (something else holds ``127.0.0.1:<port>``) the picked
  IP is served alone and the #2147 warning explains the consequence.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
from dataclasses import dataclass, field

log = logging.getLogger("protoagent.server.bind")

_WILDCARD = frozenset({"0.0.0.0", "::", "*", ""})
LOOPBACK = "127.0.0.1"


@dataclass
class ListenPlan:
    """What to hand uvicorn. ``sockets`` empty → let uvicorn bind ``host`` itself."""

    host: str  # the effective bind (what pairing/auth are told)
    sockets: list[socket.socket] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)  # addresses served besides ``host``
    warnings: list[str] = field(default_factory=list)

    @property
    def loopback_served(self) -> bool:
        return LOOPBACK in self.extra or _is_loopback_or_wild(self.host)

    def close(self) -> None:
        for s in self.sockets:
            try:
                s.close()
            except OSError:
                pass


def _is_loopback_or_wild(host: str) -> bool:
    h = (host or "").strip()
    if h in _WILDCARD or h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address((host or "").strip())
    except ValueError:
        return None


def _listen_socket(host: str, port: int, family: int) -> socket.socket:
    """A bound (not yet listening) TCP socket, set up the way uvicorn's own ``bind_socket``
    does. Not inheritable: the restart re-execs this process, and a listener leaked across
    ``execv`` would hold the port the new process needs."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.set_inheritable(False)
    except OSError:
        sock.close()
        raise
    return sock


def plan_listen(host: str, port: int) -> ListenPlan:
    """Decide (and open) the listening sockets for ``host:port``. Never raises for a missing
    address; a port that's genuinely busy is left for uvicorn to report as it always has."""
    h = (host or "").strip()
    ip = _ip_literal(h)
    if ip is None or _is_loopback_or_wild(h):
        return ListenPlan(host=h or LOOPBACK)

    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        picked = _listen_socket(h, port, family)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return ListenPlan(host=h)  # uvicorn says "address already in use", as before
        return ListenPlan(
            host=LOOPBACK,
            warnings=[
                f"[network] network.bind is {h}, but that address isn't on this machine right now "
                f"({exc.strerror or exc}). Serving {LOOPBACK} only, so the local console still works. "
                "If the address changed (a new Wi-Fi lease) pick it again in Settings ▸ Devices; if it's "
                "a tailnet address, Tailscale may not have been connected yet — restart once it is, or "
                "use tailscale serve, which keeps the bind on localhost."
            ],
        )
    try:
        loop = _listen_socket(LOOPBACK, port, socket.AF_INET)
    except OSError as exc:
        return ListenPlan(
            host=h,
            sockets=[picked],
            warnings=[
                f"[network] couldn't also listen on {LOOPBACK}:{port} ({exc.strerror or exc}); "
                f"serving {h} only — the local console and the desktop app can't reach this instance."
            ],
        )
    return ListenPlan(host=h, sockets=[picked, loop], extra=[LOOPBACK])
