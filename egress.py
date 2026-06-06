"""Egress allowlist for outbound HTTP from agent tools (ADR 0008).

Enforced in ``fetch_url`` — the tool where the model picks an arbitrary host,
i.e. the main in-process exfiltration / SSRF vector. Two layers: an optional
host **allowlist** (deny-by-default when set; the single source of truth the
OpenShell network policy is generated from, ``scripts/gen_openshell_policy.py``),
and — when no allowlist is set — a **default-on private-IP denylist** so the
model can't reach an internal service or cloud-metadata (``169.254.169.254``)
out of the box. Public hosts still work with no allowlist; allowlisting a host
explicitly trusts it (bypasses the denylist).

Mirrors the ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` SSRF-guard pattern in
``a2a_stores``. Wildcards: a leading ``*.`` matches any subdomain
(``*.proto-labs.ai`` allows ``api.proto-labs.ai`` and ``proto-labs.ai``).

This is the in-process half. Process-level egress (subprocess escapes via
``execute_code`` / ``run_command``, raw sockets) is only truly fenced by running
under OpenShell's network namespace + proxy — see ADR 0008.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_allowed: list[str] = []  # lowercased host patterns; empty = permissive (off)


def set_allowed_hosts(hosts) -> None:
    """Set the allowlist (called once at startup from config). Empty = off."""
    global _allowed
    _allowed = [str(h).strip().lower() for h in (hosts or []) if h and str(h).strip()]


def allowed_hosts() -> list[str]:
    return list(_allowed)


def is_enabled() -> bool:
    return bool(_allowed)


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    for pat in _allowed:
        if pat.startswith("*."):
            # "*.example.com" → match the apex and any subdomain.
            if host == pat[2:] or host.endswith(pat[1:]):
                return True
        elif host == pat:
            return True
    return False


def _blocked_ip(host: str) -> str | None:
    """Resolve ``host`` and return the first address that is a private/internal
    SSRF target (loopback / link-local / private / multicast / reserved /
    unspecified), or the literal ``"unresolvable"`` when DNS fails (treated as
    unsafe, matching ``a2a_stores``). ``None`` ⇒ the host resolves only to
    globally-routable addresses. One-shot resolution — not a DNS-rebinding
    defence, but closes the trivial literal/redirect-to-internal vector."""
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            return "unresolvable"
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return addr
        if (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return addr
    return None


def check_url(url: str) -> str | None:
    """Return an error string if the URL's host is not permitted, else ``None``.

    Two layers:
    - **Allowlist set** → only allowlisted hosts pass (wildcards supported). An
      allowlisted host is explicitly trusted and bypasses the IP denylist below
      (you may allowlist an internal host on purpose).
    - **No allowlist (default)** → a host is permitted unless it resolves to a
      private / loopback / link-local / cloud-metadata / reserved address. This
      default-on SSRF guard stops the model `fetch_url`-ing an internal service
      or `169.254.169.254` even when no allowlist is configured.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return f"Error: malformed URL: {url!r}"
    if not host:
        return f"Error: no host in URL: {url!r}"
    if _allowed:
        if _host_allowed(host):
            return None
        return (
            f"Error: egress to {host} is blocked — not in the egress allowlist "
            f"({', '.join(_allowed)}). Set egress.allowed_hosts to permit it."
        )
    bad = _blocked_ip(host)
    if bad == "unresolvable":
        return f"Error: egress to {host} is blocked — host did not resolve."
    if bad:
        return (
            f"Error: egress to {host} ({bad}) is blocked — it resolves to a "
            f"private/internal address (SSRF guard). Allowlist it via "
            f"egress.allowed_hosts if this is intentional."
        )
    return None
