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
``a2a_impl.stores``. Wildcards: a leading ``*.`` matches any subdomain
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


def set_allowed_hosts(hosts, *, also_allow_url: str = "") -> None:
    """Set the allowlist (called at startup / live-reload from config). Empty = off.

    ``also_allow_url`` — when an allowlist IS configured, the host of this URL (the
    operator-configured model gateway / ``api_base``) is always included, so a deny-by-default
    allowlist never blocks the operator's own deliberately-set gateway. Mirrors
    ``scripts/gen_openshell_policy.py``, which already auto-adds the api_base host to the
    process-level network policy. Ignored when ``hosts`` is empty — permissive mode must stay
    permissive (adding one host would flip the guard into deny-by-default for everything else).
    """
    global _allowed
    cleaned = [str(h).strip().lower() for h in (hosts or []) if h and str(h).strip()]
    if cleaned and also_allow_url:
        try:
            host = (urlparse(also_allow_url).hostname or "").lower()
        except ValueError:
            host = ""
        if host and host not in cleaned:
            cleaned.append(host)
    _allowed = cleaned


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


def _blocked_ip(host: str, *, allow_private: bool = False) -> str | None:
    """Resolve ``host`` and return the first address that is a private/internal
    SSRF target (loopback / link-local / private / multicast / reserved /
    unspecified), or the literal ``"unresolvable"`` when DNS fails (treated as
    unsafe, matching ``a2a_impl.stores``). ``None`` ⇒ the host resolves only to
    globally-routable addresses. One-shot resolution — not a DNS-rebinding
    defence, but closes the trivial literal/redirect-to-internal vector.

    ``allow_private=True`` permits private + loopback ranges (LAN / tailnet / a
    co-located instance — the *normal* case for a fleet remote) while STILL
    blocking link-local (incl. cloud-metadata ``169.254.169.254``), multicast,
    reserved and unspecified — the actual SSRF/credential-theft targets."""
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
        # link-local covers the cloud-metadata IP — always blocked.
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return addr
        if not allow_private and (ip.is_loopback or ip.is_private):
            return addr
    return None


def check_url(url: str, *, allow_private: bool = False, block_unresolvable: bool = True) -> str | None:
    """Return an error string if the URL's host is not permitted, else ``None``.

    Two layers:
    - **Allowlist set** → only allowlisted hosts pass (wildcards supported). An
      allowlisted host is explicitly trusted and bypasses the IP denylist below
      (you may allowlist an internal host on purpose).
    - **No allowlist (default)** → a host is permitted unless it resolves to a
      private / loopback / link-local / cloud-metadata / reserved address. This
      default-on SSRF guard stops the model `fetch_url`-ing an internal service
      or `169.254.169.254` even when no allowlist is configured.

    ``allow_private=True`` keeps LAN/tailnet/loopback hosts (the normal fleet-remote
    case) while still blocking link-local/metadata/multicast/reserved — for callers
    that legitimately reach private peers but must never be steered at metadata.
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
    bad = _blocked_ip(host, allow_private=allow_private)
    if bad == "unresolvable":
        # An unresolvable host is not itself an SSRF target. Callers that register a
        # URL for later use (a fleet remote that may come online afterwards) pass
        # block_unresolvable=False; request-time callers keep the strict default.
        return None if not block_unresolvable else f"Error: egress to {host} is blocked — host did not resolve."
    if bad:
        kind = "link-local/metadata/reserved" if allow_private else "private/internal"
        return (
            f"Error: egress to {host} ({bad}) is blocked — it resolves to a "
            f"{kind} address (SSRF guard). Allowlist it via "
            f"egress.allowed_hosts if this is intentional."
        )
    return None
