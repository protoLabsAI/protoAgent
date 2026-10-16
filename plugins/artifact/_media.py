"""Inline media for `file` artifacts (#4257): images + video play IN the panel.

A ``<video>``/``<img>`` element can't carry the operator bearer, and the nested artifact frame is
an opaque-origin sandbox, so the panel can't simply point an element at the gated ``/blob`` route.
Fetching the whole blob first (the pptx/pdf byte feed) would make a 100 MB video download before
it can play or seek. Instead the shell mints a short-lived MEDIA TICKET over the gated data
router (bearer-checked like every data route) and points the element at the PUBLIC
``/plugins/artifact/media/{id}`` route with it; that route serves HTTP Range requests, so the
browser streams and seeks.

A ticket is an unguessable 256-bit token bound to ONE artifact version (``id`` + explicit 1-based
version, resolved at mint time), valid for ``TTL_S`` after its last use and never past
``MAX_AGE_S`` from its mint. In memory only: a restart drops every ticket (the panel re-mints on
its next render). Only allowlisted image/video types are ever served this way, each under
``nosniff`` + a sandboxing CSP, so a ticket URL opened top-level can't run script on this origin.
"""

from __future__ import annotations

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
    "Cache-Control": "private, no-cache",
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
_tickets: dict[str, tuple[str, int, float, float]] = {}  # token → (art_id, version, minted, last_used)


def mint(art_id: str, version: int) -> str:
    """A fresh ticket for ``art_id`` version ``version`` (1-based, explicit)."""
    tok = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _lock:
        _prune(now)
        while len(_tickets) >= MAX_TICKETS:
            _tickets.pop(next(iter(_tickets)))
        _tickets[tok] = (art_id, int(version), now, now)
    return tok


def check(token: str, art_id: str, version: int) -> bool:
    """Does ``token`` grant ``art_id`` version ``version``? A hit slides its idle expiry."""
    if not token:
        return False
    now = time.monotonic()
    with _lock:
        t = _tickets.get(token)
        if t is None:
            return False
        tid, tver, minted, used = t
        if now - used > TTL_S or now - minted > MAX_AGE_S:
            _tickets.pop(token, None)
            return False
        if not (secrets.compare_digest(tid, art_id) and tver == int(version)):
            return False
        _tickets[token] = (tid, tver, minted, now)
    return True


def _prune(now: float) -> None:
    dead = [k for k, (_, _, m, u) in _tickets.items() if now - u > TTL_S or now - m > MAX_AGE_S]
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
