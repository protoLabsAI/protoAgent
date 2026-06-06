"""Opt-in CIDR allowlist for outbound A2A destinations (#572).

A *positive* CIDR allowlist for the two outbound surfaces that POST to an
address the agent doesn't fully control — A2A push callbacks (caller-supplied
webhook URLs) and ``peer_consult`` (operator-configured peer URLs). When the
allowlist is set, a destination is permitted IFF **every** resolved IP of its
host falls inside an allowlisted CIDR.

**Empty/unset is permissive** (off): push callbacks keep their default
private-IP denylist (``a2a_stores.is_safe_webhook_url``) and ``peer_consult``
is unrestricted — so existing deployments are unchanged until they opt in via
``security.callback_allowlist``.

Mirrors ``egress.py`` (host allowlist for ``fetch_url``) and the
``PUSH_NOTIFICATION_ALLOWED_CIDRS`` bypass in ``a2a_stores`` — this is the
unified, config-driven knob covering both outbound A2A surfaces. Set once at
startup and on live config reload (``server/agent_init``).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

log = logging.getLogger("protoagent.security")

# Parsed ip_network objects; empty = off (permissive). Re-set on config reload.
_cidrs: list = []


def set_callback_allowlist(cidrs) -> None:
    """Set the allowlist (called at startup + on live config reload). Empty = off."""
    global _cidrs
    parsed = []
    for c in cidrs or []:
        c = str(c).strip()
        if not c:
            continue
        try:
            parsed.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            log.warning("[security] ignoring malformed CIDR in callback_allowlist: %s", c)
    _cidrs = parsed


def allowlist() -> list[str]:
    return [str(c) for c in _cidrs]


def is_enabled() -> bool:
    return bool(_cidrs)


def _resolve_ips(host: str) -> list[str] | None:
    """Resolve ``host`` to IP literals (one-shot). ``None`` on failure. A literal
    IP is returned as-is. (One-time resolution isn't a full DNS-rebinding defence
    but closes the trivial literal-address vector — same posture as a2a_stores.)"""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        return None


def check_url(url: str) -> str | None:
    """Return an error string if ``url``'s host is outside the allowlist, else
    ``None``. Permissive (``None``) when the allowlist is unset.

    When set, EVERY resolved IP of the host must fall inside an allowlisted CIDR
    — a host resolving to a mix of in- and out-of-allowlist addresses is rejected.
    """
    if not _cidrs:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Error: malformed URL: {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"Error: refusing non-http(s) destination: {url!r}"
    host = parsed.hostname
    if not host:
        return f"Error: no host in URL: {url!r}"
    ips = _resolve_ips(host)
    if not ips:
        return f"Error: could not resolve host {host!r}"
    for addr in ips:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return f"Error: unparseable address {addr!r} for {host!r}"
        if not any(ip in cidr for cidr in _cidrs):
            return (
                f"Error: destination {host} ({addr}) is not in the callback "
                f"allowlist ({', '.join(str(c) for c in _cidrs)}). Set "
                f"security.callback_allowlist to permit it."
            )
    return None


def is_allowed(url: str) -> bool:
    """Convenience boolean wrapper around :func:`check_url`."""
    return check_url(url) is None
