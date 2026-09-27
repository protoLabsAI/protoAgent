"""Open-mode request hardening: a ``Host`` allowlist and a JSON content-type rule (#3668).

An OPEN instance (no bearer, no X-API-Key — the desktop default on its loopback bind) rates
every caller operator, so it should only answer requests addressed to a name it is actually
served under. That is the standard allowed-hosts hardening for local servers (Jupyter's
``local_hostnames``, VS Code's and Vite's allowed hosts), and ``HostGuardMiddleware`` applies it
to EVERY path and to WebSockets.

Separately, ``/a2a`` and ``/v1/*`` (and their ``/agents/<slug>/…`` proxied forms) are consumer
surfaces whose bodies are always JSON. In open mode a state-changing request to them must say so
(``Content-Type: application/json`` or a ``+json`` type); anything else is ``415``. A JSON
content type is never one a browser will send cross-site without a CORS preflight, and the
server's CORS policy only admits the console's own origins (loopback and the desktop app).

**Gated instances are unaffected.** With a bearer or X-API-Key configured, the only credential is
a header the client attaches itself (the console keeps it in per-origin localStorage; nothing
auth-bearing is a cookie), so a page on any other origin — or on a different NAME for the same
address — has nothing ambient to ride on, and ``A2AAuthMiddleware`` already refuses it. Leaving
the gated path alone also keeps reverse-proxy deployments (which forward arbitrary public names)
working without new configuration.

The helpers here were introduced for the fleet proxy's remote-member gate (#3662, which still
calls ``host_allowed``) and moved into ``a2a_impl`` so the auth layer and ``graph/fleet`` share one
definition — ``graph/`` may import ``a2a_impl``, never the server.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Resolved bind interface, pushed in by the server bootstrap once it knows ``--host`` /
# ``network.bind``. Only a NAME matters here — any IP literal passes on its own.
_BIND_HOST: list[str] = ["127.0.0.1"]


def set_bind_host(host: str) -> None:
    """Record the resolved bind interface (called from the server bootstrap)."""
    _BIND_HOST[0] = (host or "").strip().lower() or "127.0.0.1"


def host_name(host: str) -> str:
    """The name part of a ``Host`` header, lowercased: port dropped, IPv6 brackets and a
    trailing root dot removed (``[::1]:7870`` → ``::1``, ``Localhost.:7870`` → ``localhost``).
    An RFC 6874 zone id stays as written (``fe80::1%25en0``): ``ipaddress`` accepts any scope
    string, so it still reads as the IP literal it is."""
    h = host.strip().lower()
    if h.startswith("["):
        return h[1 : h.find("]")] if "]" in h else h[1:]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _own_names() -> set[str]:
    """This machine's own mDNS name, ``<short>.local`` — and deliberately nothing else.

    The bare short name and a dotted FQDN are both resolved through the NETWORK's DNS: a DHCP
    search domain turns ``joshs-mbp`` into ``joshs-mbp.<their-domain>``, and a DHCP-assigned
    FQDN sits in a zone the network runs — so a hostile Wi-Fi could answer either with
    127.0.0.1. ``.local`` is multicast DNS, and this machine's own responder owns its own name
    (a conflicting answer forces a rename rather than winning). ``gethostname`` reads the
    kernel's name — no resolution, so no 5s DNS stall. Anything else goes in
    ``PROTOAGENT_TRUSTED_HOSTS``."""
    try:
        name = socket.gethostname().strip().lower().rstrip(".")
    except OSError:
        return set()
    if not name:
        return set()
    short = name.split(".", 1)[0]
    return {f"{short}.local"} if short else set()


def _trusted_host_names() -> set[str]:
    """Operator-declared names the instance is served under: ``PROTOAGENT_TRUSTED_HOSTS``
    (comma-separated — a reverse proxy that forwards its own public name, or a container
    network's service name) plus the host of every ``A2A_ALLOWED_ORIGINS`` entry (an origin you
    trust to CALL the instance names a host that serves it). Read per call — both are cheap and
    an env change must not need a cache flush."""
    from a2a_impl.auth import allowed_origins

    names = {host_name(n) for n in os.environ.get("PROTOAGENT_TRUSTED_HOSTS", "").split(",") if n.strip()}
    for o in allowed_origins() or []:
        try:
            netloc = urlsplit(o).netloc
        except ValueError:
            continue
        if netloc:
            names.add(host_name(netloc))
    return names


def host_allowed(host: str | None) -> bool:
    """Is ``host`` (the request's ``Host`` header) a name this instance is honestly served under?

    Allowed:
    - **Any IP literal** (v4, v6, bracketed, with a zone id): 127.x, ::1, this box's LAN/tailnet
      IPs, the bind address. A browser only sends an IP ``Host`` when it connected to that IP by
      address, so the page came from whatever serves there. That makes an explicit "this
      machine's addresses" list unnecessary (and it can't go stale).
    - ``localhost`` and ``*.localhost`` (RFC 6761: browsers resolve them to loopback
      themselves, no DNS involved).
    - ``*.ts.net`` — Tailscale MagicDNS (``tailscale serve`` forwards its own
      ``<machine>.<tailnet>.ts.net``). That zone is Tailscale's.
    - This machine's ``<short>.local`` mDNS name (see ``_own_names`` for why not the bare or
      DNS hostname), and the bind address when it is a name.
    - ``PROTOAGENT_TRUSTED_HOSTS`` and the hosts of ``A2A_ALLOWED_ORIGINS``.

    Deliberately NOT auto-trusted: a container network's bare service name (``agent`` in
    ``http://agent:7870``), even under ``PROTOAGENT_ALLOW_OPEN=1``. That opt-in's documented
    posture is a port published to the host's loopback, where a browser on the host reaches it
    — exactly where this check matters — and a bare name is answered by whatever DNS search
    domain the network hands out. A compose fleet that calls an open agent by service name
    declares it in ``PROTOAGENT_TRUSTED_HOSTS`` (the refusal log line says so).

    **No ``Host`` at all passes.** Every browser sends one (HTTP/1.1 requires it, and uvicorn's
    h11 rejects an HTTP/1.1 request without it before the app runs), so a missing header means
    an HTTP/1.0 or hand-rolled non-browser client, which has no same-origin policy to confuse
    and could send any ``Host`` it liked anyway."""
    if host is None or not host.strip():
        return True
    name = host_name(host)
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".ts.net"):
        return True
    if name == host_name(_BIND_HOST[0]) or name in _own_names():
        return True
    return name in _trusted_host_names()


# ── Cross-site requests (Origin + Fetch Metadata) ─────────────────────────────────────────

# The desktop app's webview origins (apps/desktop/src-tauri/src/lib.rs ``is_own_origin``;
# ``tauri://localhost`` on macOS/Linux, ``http://tauri.localhost`` on Windows) — the same pair
# the server's CORS ``allow_origin_regex`` admits. The webview's origin never equals the hub's
# ``Host``, so without these the desktop console couldn't open a remote member's live view.
_DESKTOP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost"})

# The origins the server's CORS middleware grants (``server/__init__.py`` passes this as
# ``allow_origin_regex``; one definition so the two can't drift): the desktop webview and
# loopback on any port. Matched against a lowercased Origin.
CONSOLE_ORIGIN_REGEX = r"^(tauri://localhost|http://tauri\.localhost|https?://(localhost|127\.0\.0\.1)(:\d+)?)$"
CONSOLE_ORIGIN_RE = re.compile(CONSOLE_ORIGIN_REGEX)


def origin_allowed(origin: str | None, host: str | None, *, console_origins: bool = False) -> bool:
    """May a browser request with this ``Origin`` go through?

    Written for the fleet proxy's remote-member gate — the WS handshake (every remote upgrade)
    and the HTTP proxy (remote targets on an open hub, #3662) — and shared with the open-mode
    ``HostGuardMiddleware`` (#3668), which passes ``console_origins=True`` to also admit the
    origins the server's own CORS policy grants (``CONSOLE_ORIGIN_RE``: loopback on any port,
    so the Vite dev server and a sibling console keep working). Those origins can already make
    preflighted, readable requests to this instance, so blocking their blind sends would add
    nothing. The proxy keeps the stricter default: a sibling port is not the hub's own console,
    and a remote's token is lent to the hub, not to every local page. The HTTP ``A2A_ALLOWED_ORIGINS`` check is middleware and never sees a WS
    scope, and a browser lets ANY page open a WebSocket to any origin (no CORS on WS) — so
    without this a page in the operator's browser could drive a remote's live sockets through
    the hub. Allowed: no ``Origin`` at all (a non-browser client — browsers always send one on
    a WS), same-origin with the hub (the Origin's host[:port] equals the ``Host`` header;
    scheme-agnostic so a TLS front such as ``tailscale serve`` still matches), the desktop
    webview, and anything in the ``A2A_ALLOWED_ORIGINS`` allowlist when one is set. Everything
    else — including the opaque ``null`` origin — is refused."""
    if origin is None:
        return True
    o = origin.strip().lower()
    if o in _DESKTOP_ORIGINS:
        return True
    if console_origins and CONSOLE_ORIGIN_RE.match(o):
        return True
    from a2a_impl.auth import allowed_origins

    allow = allowed_origins()
    if allow and o in allow:
        return True
    try:
        parts = urlsplit(o)
    except ValueError:
        return False
    return bool(parts.scheme in ("http", "https") and parts.netloc and host and parts.netloc == host.strip().lower())


# A navigation (top-level or iframe) may be cross-site — links and the desktop webview's plugin
# view iframes are exactly that — but never into an <object>/<embed> (web.dev's isolation policy).
_NAV_BLOCKED_DESTS = frozenset({"object", "embed"})

# Cross-site no-cors MEDIA loads a GET may make. The desktop console (``tauri://localhost``)
# renders a remote's media as ``<img src="http://127.0.0.1:<port>/agents/<rid>/media/…">``, and
# WebKit sends NO Referer from a non-http(s) page (``SecurityPolicy::generateReferrerHeader``
# bails outside the HTTP family) — so that load carries neither Origin nor Referer, only
# ``Sec-Fetch-Site: cross-site`` + ``Sec-Fetch-Dest: image``. Same threat level as the GET
# navigation exemption: a media response is opaque to the page (at most an image's size or a
# clip's duration leaks), and a GET with side effects would be the remote's bug. ``script`` and
# ``style`` stay refused: a cross-site page can EXECUTE or APPLY those responses in its own
# context (script inclusion / CSS-parsing leaks of a JSON or JS body), which reads data rather
# than just displaying it — and nothing in the console loads a remote's script or stylesheet
# from a foreign origin (plugin views are iframes, whose subresources are same-origin).
_MEDIA_DESTS = frozenset({"image", "audio", "video", "track", "font"})


def _origin_of(url: str) -> str | None:
    """``scheme://netloc`` of a URL (a ``Referer``), or None when it has neither."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def cross_site_refusal(method: str, headers, *, console_origins: bool = False) -> str | None:
    """Why a browser request to a remote member must be refused, or None to let it through.

    - **An ``Origin`` is present** (every cross-origin CORS request, and every POST from a
      modern browser): it must pass ``origin_allowed`` — same-origin with ``Host``, the desktop
      webview, or ``A2A_ALLOWED_ORIGINS``. That decides it, whatever ``Sec-Fetch-Site`` says:
      the desktop console (``tauri://localhost``) calling ``http://127.0.0.1:<port>`` IS
      cross-site to the browser, and its Origin is what proves it is the operator's app.
    - **No Origin, ``Sec-Fetch-Site: cross-site``** — a no-cors subresource or a navigation from
      a foreign page. Refused, except a GET/HEAD that is
      - a navigation (``Sec-Fetch-Mode: navigate``, not into an object/embed: the desktop
        webview's iframe of a remote plugin view, or a link) — the attacker can't read it;
      - a media load (``Sec-Fetch-Dest`` in ``_MEDIA_DESTS``: the desktop chat's ``<img>`` of a
        remote's media, which WebKit sends with no Referer) — opaque to the page;
      and, as a fallback for any other shape, a request whose ``Referer`` origin is trusted (a
      page can suppress Referer, never forge it; Chromium-based webviews send one).
    - ``same-origin``, ``same-site`` and ``none`` (typed URL / bookmark) pass, as does a request
      with no Fetch Metadata and no Origin (curl, the D4 fleet-token delegate path, a browser
      too old to send either). ``same-site`` passes because on this surface it only means
      "another port of the same host" — the Vite dev server at :5173, a sibling instance — or a
      sibling name in the same tailnet; any such request that carries an Origin (every POST)
      still has to be same-origin or allowlisted by the rule above.
    """
    host = headers.get("host")
    origin = headers.get("origin")
    if origin is not None:
        return None if origin_allowed(origin, host, console_origins=console_origins) else "origin not allowed"
    if (headers.get("sec-fetch-site") or "").strip().lower() != "cross-site":
        return None
    mode = (headers.get("sec-fetch-mode") or "").strip().lower()
    dest = (headers.get("sec-fetch-dest") or "").strip().lower()
    if method.upper() in ("GET", "HEAD"):
        if mode == "navigate" and dest not in _NAV_BLOCKED_DESTS:
            return None
        if dest in _MEDIA_DESTS:
            return None
    referer = headers.get("referer")
    ref_origin = _origin_of(referer) if referer else None
    if ref_origin and origin_allowed(ref_origin, host, console_origins=console_origins):
        return None
    return "cross-site request"


# ── JSON content-type rule for the consumer surfaces ───────────────────────────────────────

_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _json_surface(path: str) -> bool:
    """``/a2a`` (+ sub-paths), ``/v1/*``, and the same under a fleet ``/agents/<slug>/`` prefix
    — the proxy forwards those to a member that may be credential-gated (a fleet member holds
    the service token), so the hub is where an open instance applies the rule."""
    if path.startswith("/agents/"):
        rest = path[len("/agents/") :]
        slash = rest.find("/")
        if slash <= 0:
            return False
        path = rest[slash:]
    return path == "/a2a" or path.startswith("/a2a/") or path.startswith("/v1/")


def json_content_type(value: str | None) -> bool:
    """``application/json`` or any ``application/*+json``, parameters (``; charset=…``) ignored."""
    if not value:
        return False
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


# ── The middleware ──────────────────────────────────────────────────────────────────────────

# Hosts already logged, so a burst of refused requests is one WARNING per name, not a flood.
_LOGGED_HOSTS: set[str] = set()
_LOGGED_HOSTS_MAX = 64


def _log_host_refusal(host: str, path: str) -> None:
    shown = host[:120]
    if shown in _LOGGED_HOSTS:
        return
    if len(_LOGGED_HOSTS) >= _LOGGED_HOSTS_MAX:
        return  # cap reached — a flood of distinct names must not become a flood of lines
    _LOGGED_HOSTS.add(shown)
    if len(_LOGGED_HOSTS) == _LOGGED_HOSTS_MAX:
        logger.warning("[auth] untrusted-Host refusal log cap reached; further names are not logged")
    logger.warning(
        "[auth] refusing request with untrusted Host %r (path %s) — this instance has no auth token, "
        "so it only answers the names it is served under. Add the name to PROTOAGENT_TRUSTED_HOSTS "
        "if it is yours, or set an auth token.",
        shown,
        path[:200],
    )


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1")
    return None


async def _send_json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class HostGuardMiddleware:
    """Pure-ASGI gate in front of the whole app — HTTP **and** WebSocket scopes.

    It is pure ASGI (not ``BaseHTTPMiddleware``) because ``@app.websocket`` routes — the fleet
    WS proxy, plugin live sockets — never pass through ``A2AAuthMiddleware``, which only sees
    ``http`` scopes. Installed outermost so a refused request touches nothing: no route, no
    CORS, no static file.

    Open mode only (see the module docstring for why a gated instance is left alone):

    - **Untrusted ``Host``** (``host_allowed``) → ``403`` with a JSON ``detail`` for HTTP; a
      WebSocket handshake is closed ``1008`` before ``accept`` (uvicorn answers it with an HTTP
      403). ``403`` rather than ``421 Misdirected Request``: 421 tells a client to retry on a
      different connection (browsers do so automatically under HTTP/2 coalescing), and this is
      a policy refusal, not a routing hint.
    - **A cross-site state-changing request or WebSocket** (``cross_site_refusal`` with the
      console origins admitted) → ``403`` / close ``1008``. Applies to every method but
      GET/HEAD/OPTIONS, and to every WebSocket handshake. A trusted ``Host`` says the request
      was addressed to this instance; it says nothing about which page sent it, and CORS only
      stops a foreign page from READING a response, never from sending a request that needs
      no preflight (and WebSockets have no CORS at all). Allowed: no ``Origin`` and no
      ``Sec-Fetch-Site: cross-site`` (curl, SDKs, the hub's own loopback calls), same-origin
      with ``Host`` (the console and its plugin-view iframes, which are
      ``allow-same-origin``), the desktop webview, loopback console origins on any port, and
      ``A2A_ALLOWED_ORIGINS``. The opaque ``null`` origin (a fully sandboxed frame, such as
      generated artifact content) is refused — no protoAgent client sends one. GET and HEAD
      stay open: they must not change state (the one GET with a minor side effect is chat
      attendance, which only records that a console is watching).
    - **A body-bearing request to a JSON surface without a JSON content type** → ``415``.

    ``open_mode()`` is read per request, so a bearer set or cleared at runtime takes effect
    without re-installing anything."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        from a2a_impl.auth import open_mode

        if not open_mode():
            return await self.app(scope, receive, send)
        host = _header(scope, b"host")
        path = scope.get("path") or ""
        if not host_allowed(host):
            _log_host_refusal(host or "", path)
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "host not allowed"})
                return
            return await _send_json(send, 403, "Forbidden: untrusted Host header")
        method = "GET" if kind == "websocket" else (scope.get("method") or "GET").upper()
        if kind == "websocket" or method not in _BODYLESS_METHODS:
            from starlette.datastructures import Headers

            # A WebSocket handshake is judged as a GET, but it is never a navigation or a media
            # load, so the GET exemptions don't apply to it: Origin (which browsers always send
            # on a WS) decides.
            if cross_site_refusal(method, Headers(scope=scope), console_origins=True) is not None:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": "cross-site request"})
                    return
                return await _send_json(send, 403, "Forbidden: cross-site request")
        if (
            kind == "http"
            and method not in _BODYLESS_METHODS
            and _json_surface(path)
            and not json_content_type(_header(scope, b"content-type"))
        ):
            return await _send_json(send, 415, "Unsupported Media Type: expected Content-Type: application/json")
        return await self.app(scope, receive, send)
