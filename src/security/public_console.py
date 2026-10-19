"""The operator's public console URL (ADR 0121 D2, ``network.public_console_url``).

An operator who wants a phone to reach the console WITHOUT Tailscale puts it behind their own
HTTPS edge (Cloudflare Tunnel + Access, nginx, Caddy) and declares that origin here. It is the
only public address this instance ever offers: discovered addresses stay tailnet/LAN only
(``operator_api.pairing_routes._pairable_addresses``).

This module owns three things:

* **Validation** (:func:`normalize_public_console_url`): ``https://`` only, an ORIGIN only (no
  path, query, fragment or userinfo), and never loopback. Called at config load, so a bad
  value fails loudly with the key named instead of half-working.
* **The live value** (:func:`set_public_console_url` / :func:`public_origin` /
  :func:`public_host`), pushed in by the server at boot and on every config reload. The auth
  layer folds it into ``A2A_ALLOWED_ORIGINS`` and the trusted Host names; the claim throttle
  keys ``public`` claims on its host.
* **The boot rule** (:func:`open_mode_refusal`): a public console with no credential is never
  a state the agent runs in. ``PROTOAGENT_ALLOW_OPEN`` does not override it.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

KEY = "network.public_console_url"
ENV_VAR = "PROTOAGENT_PUBLIC_CONSOLE_URL"


def _is_loopback_host(host: str) -> bool:
    h = host.strip().lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def normalize_public_console_url(value: object) -> str:
    """The canonical ``https://host[:port]`` for ``value``, or ``""`` when unset.

    Raises ``ValueError`` (message names :data:`KEY`) for anything that isn't an https origin
    on a non-loopback host. A trailing ``/`` is normalised away; so is an explicit ``:443``,
    because a browser omits the default port from ``Host``/``Origin`` and the comparisons
    downstream are exact."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{KEY} must be a string like https://agent.example.com")
    raw = value.strip()
    if not raw:
        return ""
    example = "for example https://agent.example.com"
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError as exc:
        raise ValueError(f"{KEY} is not a valid URL ({exc}); use an https origin, {example}") from None
    if parts.scheme.lower() != "https":
        raise ValueError(
            f"{KEY} must start with https:// (got {raw!r}). The public console is only served over "
            f"HTTPS: terminate TLS at your proxy and declare the https origin, {example}."
        )
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{KEY} must not contain a user name or password; use an https origin, {example}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"{KEY} has no host name; use an https origin, {example}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise ValueError(
            f"{KEY} must be an origin only, with no path, query or fragment (got {raw!r}). The "
            "console needs a whole hostname, because it serves /app and /api from the root."
        )
    if _is_loopback_host(host):
        raise ValueError(
            f"{KEY} must be a public name, not loopback (got {raw!r}). It is the address a phone "
            "opens from the internet; this machine's own localhost needs no setting."
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}"


# The live, already-normalised origin. "" = not configured (the default: nothing public).
_ORIGIN: list[str] = [""]


def set_public_console_url(url: str | None) -> None:
    """Record the live public console origin (server boot and config reload) and key the
    claim throttle's ``public`` ingress class on its host. An invalid value clears it rather
    than half-applying: config load already refused it with the key named."""
    from security import claim_throttle

    try:
        origin = normalize_public_console_url(url)
    except ValueError:
        origin = ""
    _ORIGIN[0] = origin
    claim_throttle.set_public_origin(origin or None)


def public_origin() -> str:
    """``https://host[:port]`` of the declared public console, or ``""``."""
    return _ORIGIN[0]


def public_host() -> str:
    """The ``Host`` header a request through the public console carries (host[:port],
    lowercase), or ``""``."""
    origin = _ORIGIN[0]
    return origin.split("://", 1)[1] if origin else ""


def open_mode_refusal(url: str | None, *, open_mode: bool) -> str | None:
    """Why the agent must refuse to start, or None.

    ADR 0121 D2: a public console with no credential (no bearer, no X-API-Key) is never a state
    the agent will run in, so there is deliberately no override. ``PROTOAGENT_ALLOW_OPEN=1``
    covers a port fenced by a network boundary; a declared public URL is the opposite of that."""
    if not open_mode:
        return None
    try:
        origin = normalize_public_console_url(url)
    except ValueError:
        origin = (url or "").strip()
    if not origin:
        return None
    return (
        f"[security] refusing to start: {KEY} is set ({origin}) but this agent has NO auth token, "
        "so anyone on the internet would get full control of it. Set auth.token in "
        "langgraph-config.yaml (or A2A_AUTH_TOKEN), or remove network.public_console_url. "
        "PROTOAGENT_ALLOW_OPEN does not override this."
    )
