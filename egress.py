"""Egress allowlist for outbound HTTP from agent tools (ADR 0008).

Deny-by-default host allowlist enforced in ``fetch_url`` — the tool where the
model picks an arbitrary host, i.e. the main in-process exfiltration / SSRF
vector. An **empty allowlist is permissive** (off), so existing deployments are
unchanged until they opt in. This is also the single source of truth the
OpenShell network policy is generated from (``scripts/gen_openshell_policy.py``).

Mirrors the ``PUSH_NOTIFICATION_ALLOWED_HOSTS`` SSRF-guard pattern in
``a2a_stores``. Wildcards: a leading ``*.`` matches any subdomain
(``*.proto-labs.ai`` allows ``api.proto-labs.ai`` and ``proto-labs.ai``).

This is the in-process half. Process-level egress (subprocess escapes via
``execute_code`` / ``run_command``, raw sockets) is only truly fenced by running
under OpenShell's network namespace + proxy — see ADR 0008.
"""

from __future__ import annotations

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


def check_url(url: str) -> str | None:
    """Return an error string if the URL's host is not allowed, else ``None``.

    Permissive (returns ``None``) when no allowlist is configured.
    """
    if not _allowed:
        return None
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return f"Error: malformed URL: {url!r}"
    if _host_allowed(host):
        return None
    return (
        f"Error: egress to {host or url!r} is blocked — not in the egress "
        f"allowlist ({', '.join(_allowed)}). Set egress.allowed_hosts to permit it."
    )
