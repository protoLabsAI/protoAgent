"""Inline media for `file` artifacts (#4257): images + video play IN the panel.

A ``<video>``/``<img>`` element can't carry the operator bearer, and the nested artifact frame is
an opaque-origin sandbox, so the panel can't simply point an element at the gated ``/blob`` route.
Fetching the whole blob first (the pptx/pdf byte feed) would make a 100 MB video download before
it can play or seek. Instead the shell mints a short-lived MEDIA TICKET over the gated data
router (bearer-checked like every data route) and points the element at the PUBLIC
``/plugins/artifact/media/{id}`` route with it; that route serves HTTP Range requests, so the
browser streams and seeks.

A ticket is an unguessable 256-bit token bound to ONE artifact version — its ``id``, explicit
1-based version AND that version's blob (resolved at mint time; a version-cap trim shifts the
indices, so a ticket that named only the index would start serving a NEWER version's bytes —
the route refuses when the version at that index no longer holds the minted blob), valid for ``TTL_S`` after its last use and never past
``MAX_AGE_S`` from its mint. In memory only: a restart drops every ticket (the panel re-mints on
its next render). Only allowlisted image/video types are ever served this way, each under
``nosniff`` + a sandboxing CSP, so a ticket URL opened top-level can't run script on this origin.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import PurePosixPath

TTL_S = 30 * 60  # idle lifetime — a paused video can sit this long and still seek
MAX_AGE_S = 8 * 60 * 60  # absolute lifetime from mint
MAX_TICKETS = 512  # oldest evicted first — bounds memory against a mint loop

# The media types the panel shows inline (and the only ones the public media route serves).
# SVG renders through <img>, which never runs its script; the route's CSP covers a direct open.
_BY_MIME = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "image/gif": "image",
    "image/svg+xml": "image",
    "video/mp4": "video",
    "video/webm": "video",
}
_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
}

# Headers on every media response: no sniffing a "png" into HTML, and a document-level sandbox
# (no script, no same-origin) if someone opens the URL as a page — e.g. an SVG with <script>.
SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, no-store",
}


def media_type(meta: dict) -> str | None:
    """The allowlisted mime a file version is served as inline, or None (not inline media).
    The stored mime wins when it's allowlisted; otherwise the filename's extension decides."""
    mime = str(meta.get("mime") or "").split(";")[0].strip().lower()
    if mime in _BY_MIME:
        return mime
    ext = PurePosixPath(str(meta.get("filename") or "")).suffix.lower()
    return _BY_EXT.get(ext)


def media_kind(meta: dict) -> str | None:
    """``"image"`` / ``"video"`` for inline media, else None."""
    mt = media_type(meta)
    return _BY_MIME.get(mt) if mt else None


_lock = threading.Lock()
# token → (art_id, version, blob file name, minted, last_used)
_tickets: dict[str, tuple[str, int, str, float, float]] = {}


def mint(art_id: str, version: int, blob: str) -> str:
    """A fresh ticket for ``art_id`` version ``version`` (1-based, explicit), whose bytes are the
    sidecar file ``blob`` (the name ``_store._blob_path`` resolves the version's token to)."""
    tok = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _lock:
        _prune(now)
        while len(_tickets) >= MAX_TICKETS:
            _tickets.pop(next(iter(_tickets)))
        _tickets[tok] = (art_id, int(version), str(blob), now, now)
    return tok


def check(token: str, art_id: str, version: int) -> str | None:
    """The blob file name ``token`` grants for ``art_id`` version ``version``, or None. The caller
    must still confirm the version it resolves holds THAT blob. A hit slides its idle expiry."""
    if not token:
        return None
    now = time.monotonic()
    with _lock:
        t = _tickets.get(token)
        if t is None:
            return None
        tid, tver, blob, minted, used = t
        if now - used > TTL_S or now - minted > MAX_AGE_S:
            _tickets.pop(token, None)
            return None
        if not (secrets.compare_digest(tid, art_id) and tver == int(version)):
            return None
        _tickets[token] = (tid, tver, blob, minted, now)
    return blob


def _prune(now: float) -> None:
    dead = [k for k, (_, _, _, m, u) in _tickets.items() if now - u > TTL_S or now - m > MAX_AGE_S]
    for k in dead:
        _tickets.pop(k, None)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """One ``bytes=`` range → ``(start, end)`` inclusive; ``None`` = serve the whole body (no
    header, a multi-range, or a non-bytes unit — all legal to answer with a plain 200);
    ``False`` = unsatisfiable (416)."""
    if not header:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return False
    first, last = first.strip(), last.strip()
    try:
        if first == "":  # suffix: the last N bytes
            n = int(last)
            if n <= 0 or size == 0:
                return False
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return False
    if start < 0 or start >= size or end < start:
        return False
    return start, min(end, size - 1)


# ── access-log redaction ──────────────────────────────────────────────────────
# The ticket rides the media URL's query, and uvicorn's access log writes every request's full
# path — so a log reader could replay a live ticket. Redaction now lives in CORE
# (``infra.access_log_redact``) so it covers the core ``/media/`` and fleet
# ``/agents/<slug>/media/`` links too, and runs regardless of whether this plugin is enabled
# (#4274 item 6). These stay as thin shims: core's redactor already matches any ``/media/``
# path (which includes ``/plugins/artifact/media/``) and the ``t``/``ticket``/``token`` params.
# Host imports stay lazy (like the plugin's ``graph.sdk`` imports) so the module still imports
# cleanly when vendored host-free.


def redact(text: str) -> str:
    """``text`` with every credential query value in a media-route URL replaced by ``…``.
    Delegates to the single core implementation (``infra.access_log_redact.redact``)."""
    from infra.access_log_redact import redact as _core_redact

    return _core_redact(text)


_TICKET_FILTER_CLS: type[logging.Filter] | None = None


def _ticket_filter_cls() -> type[logging.Filter]:
    """The back-compat ``TicketRedactFilter`` class: a pure-name subclass of the core filter, so
    the uvicorn.access filter keeps its historical name (and the logic is defined once, in core)."""
    global _TICKET_FILTER_CLS
    if _TICKET_FILTER_CLS is None:
        from infra.access_log_redact import MediaRedactFilter

        _TICKET_FILTER_CLS = type("TicketRedactFilter", (MediaRedactFilter,), {})
    return _TICKET_FILTER_CLS


def install_log_redaction(logger_name: str = "uvicorn.access") -> None:
    """Attach the access-log redactor to ``logger_name`` once (idempotent across re-registers).

    Core server startup already installs ``MediaRedactFilter``; this re-asserts redaction under
    the historical ``TicketRedactFilter`` name when the plugin's routers are built, so coverage
    holds even if the routers are built without the core install path (e.g. in tests)."""
    from infra.access_log_redact import install

    install(logger_name, filter_cls=_ticket_filter_cls())
