"""Core media output store (#1929) — save + serve tool-generated binary artifacts.

A plugin tool that produces an image/audio/video has no way to hand the user the
bytes through the string-only tool channel — so every media-producing plugin used
to hand-roll the same stack: persist under the instance dir, mount a serving
route, embed a markdown URL. This module is the ONE core copy of that stack's
storage half:

- ``save_media(data, mime, meta)`` persists the bytes under the instance media
  store (``instance_paths().store("media")``) and returns a :class:`MediaRef`
  whose ``url`` is servable by the core ``/media/<file>`` route (``server/media.py``).
- The convention: a tool embeds ``ref.url`` in its returned markdown
  (``![alt](url)``) — the console chat renders inline markdown images, so the
  artifact shows up with zero UI changes.

Auth model — signed URLs under the default-deny gate. The console renders the
markdown image as a plain ``<img>`` tag, which cannot carry an ``Authorization``
header, so a bearer-gated route would 401 every inline image. Instead each saved
file's URL carries a per-file HMAC signature (``?sig=…``) minted from a random
per-instance signing key (``media/.signing-key``): the auth middleware
(``a2a_impl/auth.py``) admits a ``/media/`` request iff the signature verifies —
default-deny holds (no unsigned enumeration; a bearer header still works), the
key never leaves the server, and URLs survive both restart AND bearer rotation.
``media.public: true`` (config) opts the whole store out of the gate explicitly.

Layering: this is an ``infra`` leaf — ``graph/plugins/registry.py`` (save side)
and ``server/media.py`` + ``a2a_impl/auth.py`` (serve side) both import it; it
imports neither. Policy (public flag, retention) is read LIVE from
``runtime.state.STATE.graph_config`` per call, so config edits apply on reload
with no re-wiring.

Retention: ``media.retention_days`` (0 = keep forever) prunes files older than N
days opportunistically on each save — self-contained, no maintenance-loop wiring,
which means expired files linger only until the next save. Good enough for v1.
"""

from __future__ import annotations

import contextvars
import hmac
import hashlib
import logging
import mimetypes
import os
import re
import secrets as _secrets
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from infra.paths import atomic_write, instance_paths

log = logging.getLogger("protoagent.media")

# URL prefix the core serving route mounts at (server/media.py) and the auth
# middleware special-cases (a2a_impl/auth.py). One constant so they never drift.
MEDIA_URL_PREFIX = "/media/"

# A servable media filename: a single path segment, no leading dot (dotfiles are
# store internals — the signing key and the ``.<id>.json`` meta sidecars).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# ``mimetypes.guess_extension`` is platform-table-dependent (and historically
# picked ``.jpe`` for image/jpeg) — pin the extensions for the types media tools
# actually produce; anything else falls back to the table, then ``.bin``.
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
}


@dataclass(frozen=True)
class MediaRef:
    """Handle to one saved artifact: embed ``url`` in returned markdown."""

    id: str  # opaque store id (the filename stem)
    url: str  # server-relative signed URL — ``/media/<file>?sig=…``
    path: Path  # absolute on-disk location (survives restart)
    mime: str  # content type it will be served with

    def absolute_url(self, base: str | None = None) -> str:
        """``url`` made absolute against ``base`` (default :func:`public_base_url`) — for a
        client that isn't the console (an A2A peer, a ``/v1`` caller), which has nothing to
        resolve a server-relative ``/media/…`` link against (#4252). Unchanged when no base
        is known."""
        return absolute_media_url(self.url, base)


def media_dir(*, create: bool = False) -> Path:
    """The instance media store (``instance_root/media``) — never hand-computed."""
    d = instance_paths().store("media")
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _live_policy() -> tuple[bool, int]:
    """``(public, retention_days)`` from the LIVE server config, so a Settings
    save applies on reload with no re-wiring. Falls back to the gated defaults
    outside a server context (unit tests, headless save)."""
    try:
        from runtime.state import STATE

        cfg = getattr(STATE, "graph_config", None)
        if cfg is not None:
            return (
                bool(getattr(cfg, "media_public", False)),
                int(getattr(cfg, "media_retention_days", 0) or 0),
            )
    except Exception:  # noqa: BLE001 — policy probe must never break a save/serve
        pass
    return False, 0


def media_public() -> bool:
    """Whether the store is opted OUT of the auth gate (``media.public: true``)."""
    return _live_policy()[0]


def _signing_key() -> str:
    """The per-instance URL-signing key (``media/.signing-key``, owner-only) —
    created on first use. Independent of the bearer token, so saved URLs survive a
    token rotation; never leaves the server."""
    f = media_dir(create=True) / ".signing-key"
    try:
        existing = f.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    fresh = _secrets.token_hex(32)
    # Through the ACL-hardening funnel: 0600 on POSIX, an owner-only ACL on Windows.
    # A bare os.chmod there only flips the read-only bit, so the key would inherit the
    # parent dir's ACL — unlike every other credential file in the tree (#2448).
    atomic_write(f, fresh, mode=0o600)
    return fresh


def sign_name(name: str) -> str:
    """The URL signature for one stored filename (HMAC-SHA256, hex)."""
    return hmac.new(_signing_key().encode(), name.encode(), hashlib.sha256).hexdigest()


def verify_name(name: str, sig: str) -> bool:
    """Constant-time check of a ``?sig=`` value against a stored filename."""
    if not name or not sig:
        return False
    return hmac.compare_digest(sign_name(name), sig)


def request_allowed(path: str, sig: str) -> bool:
    """Auth-middleware hook: may this ``/media/…`` request pass WITHOUT a bearer?

    True when the store is public (explicit opt-in) or the query signature
    verifies for the requested filename. Anything else falls back to the normal
    bearer/X-API-Key checks — default-deny holds.
    """
    if not path.startswith(MEDIA_URL_PREFIX):
        return False
    name = path[len(MEDIA_URL_PREFIX) :]
    if not _NAME_RE.match(name):
        return False  # traversal / dotfile / nested path — never exempt
    if media_public():
        return True
    return verify_name(name, sig)


def _extension_for(mime: str, source: Path | None) -> str:
    ext = _EXT_BY_MIME.get((mime or "").lower())
    if ext:
        return ext
    if source is not None and source.suffix and _NAME_RE.match(source.name):
        return source.suffix
    return mimetypes.guess_extension(mime or "") or ".bin"


def _gc(retention_days: int) -> None:
    """Prune stored files (and their meta sidecars) older than the retention
    window. Best-effort — a GC hiccup must never fail the save that triggered it."""
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    try:
        for f in media_dir().iterdir():
            if f.name.startswith(".") or not f.is_file():
                continue  # store internals (.signing-key, sidecars) are exempt
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    (f.parent / f".{f.stem}.json").unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def save_media(data: bytes | str | Path, mime: str, meta: dict | None = None) -> MediaRef:
    """Persist one generated artifact into the instance media store.

    Args:
        data: the raw bytes, or a source file path to copy in (large files are
            streamed, never loaded whole).
        mime: content type it will be served with (drives the file extension).
        meta: optional provenance (plugin id, prompt, model, …) — kept in a
            hidden ``.<id>.json`` sidecar, never served.

    Returns a :class:`MediaRef`; embed ``ref.url`` in the tool's returned
    markdown (``![alt](url)``) to render it inline in the console chat.
    """
    import json

    source: Path | None = None
    if isinstance(data, (str, Path)):
        source = Path(data)
        if not source.is_file():
            raise FileNotFoundError(f"save_media: source file not found: {source}")

    media_id = uuid.uuid4().hex
    name = f"{media_id}{_extension_for(mime, source)}"
    d = media_dir(create=True)
    dest = d / name

    # Crash-safe write: same-dir temp + os.replace (the binary sibling of
    # infra.paths.atomic_write, which is text-only).
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=f".{name}.")
    try:
        if source is not None:
            with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
                shutil.copyfileobj(src, out)
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    sidecar = {"mime": mime or "application/octet-stream", "created_at": time.time(), "meta": dict(meta or {})}
    try:
        from infra.paths import atomic_write

        atomic_write(d / f".{media_id}.json", json.dumps(sidecar))
    except Exception:  # noqa: BLE001 — the artifact is saved; a lost sidecar only degrades the served mime
        log.warning("[media] failed to write meta sidecar for %s", name, exc_info=True)

    _gc(_live_policy()[1])

    url = f"{MEDIA_URL_PREFIX}{name}?sig={sign_name(name)}"
    return MediaRef(id=media_id, url=url, path=dest, mime=sidecar["mime"])


def resolve_media(name: str) -> tuple[Path, str] | None:
    """Serving-route lookup: a validated store filename → ``(path, mime)``.

    None for anything unsafe (traversal, dotfiles/internals, nested paths) or
    absent — the route answers 404 either way, disclosing nothing.
    """
    import json

    if not _NAME_RE.match(name or ""):
        return None
    f = media_dir() / name
    if not f.is_file():
        return None
    mime = ""
    try:
        mime = json.loads((f.parent / f".{f.stem}.json").read_text()).get("mime", "")
    except (OSError, ValueError):
        pass
    if not mime:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return f, mime


# ── absolute links + per-turn file attachments (#4252) ──────────────────────────────────


def public_base_url() -> str:
    """The agent's externally-reachable base URL (no trailing slash), or ``""``.

    The SAME address the A2A card advertises (``server/a2a.py::_a2a_card_url``): an explicit
    ``A2A_PUBLIC_URL`` (set for any deployed agent — behind a proxy or in a container the
    bound port isn't the public address), else the actually-bound loopback port for local and
    desktop runs, where the client is on the same host. Never the bind host or a request's
    forwarded host — so no internal hostname leaks into a link that wasn't already public in
    the card. ``""`` outside a running server (no port bound yet)."""
    base = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    if base:
        return base
    try:
        from runtime.state import STATE

        port = getattr(STATE, "active_port", None)
        if isinstance(port, int) and not isinstance(port, bool) and port > 0:
            return f"http://127.0.0.1:{port}"
    except Exception:  # noqa: BLE001 — no server state: no base
        pass
    return ""


# A signed, server-relative media link as save_media mints it, not already part of a URL
# (no scheme/host/path char immediately before the slash).
_RELATIVE_MEDIA_LINK = re.compile(r"(?<![\w/:.\-])/media/[A-Za-z0-9][A-Za-z0-9._-]*\?sig=[0-9a-f]+")


def absolute_media_url(url: str, base: str | None = None) -> str:
    """A server-relative ``/media/…`` URL made absolute against ``base`` (default
    :func:`public_base_url`). An already-absolute URL, or any URL when no base is known, is
    returned unchanged."""
    url = str(url or "")
    if not url.startswith(MEDIA_URL_PREFIX):
        return url
    b = (public_base_url() if base is None else str(base)).strip().rstrip("/")
    return f"{b}{url}" if b else url


def absolutize_media_links(text: str, base: str | None = None) -> str:
    """Every signed server-relative ``/media/…?sig=…`` link in ``text`` made absolute — for a
    reply going to a client with no origin to resolve it against (``/v1``). Not for the
    console's stream: it resolves relative links itself, and through a proxy (the fleet hub)
    a relative link is the only one that still works."""
    b = (public_base_url() if base is None else str(base)).strip().rstrip("/")
    if not text or not b:
        return text
    return _RELATIVE_MEDIA_LINK.sub(lambda m: b + m.group(0), text)


# The files a tool attached to the CURRENT turn's answer (``attach_file``), armed per turn by
# the surface that can deliver them — the A2A executor (as FileParts on the answer artifact)
# and ``/v1`` (as links + an ``attachments`` field). ``None`` = no turn armed, and an attach
# is a no-op. A mutable list so a tool's copied context (LangGraph runs sync tools in a
# copy_context thread) appends to the turn's one list.
_TURN_FILES: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("protoagent_turn_files", default=None)
_MAX_TURN_FILES = 16


def begin_turn_files() -> contextvars.Token:
    """Arm file attachments for the turn running in this context; pass the token to
    :func:`end_turn_files` when the turn ends."""
    return _TURN_FILES.set([])


def end_turn_files(token: contextvars.Token | None) -> None:
    if token is None:
        return
    try:
        _TURN_FILES.reset(token)
    except ValueError:  # reset from another context — the turn's own context drops it anyway
        pass


def turn_files() -> list[dict]:
    """The armed turn's attachments so far: ``[{url, mime, name}]`` (a copy)."""
    files = _TURN_FILES.get()
    return [dict(f) for f in files] if files else []


def attach_file(ref: MediaRef | str, *, name: str = "", mime: str = "") -> bool:
    """Attach a media-store file to the CURRENT turn's answer (#4252). True when attached.

    ``ref`` is a :class:`MediaRef` from ``save_media`` or its signed ``/media/…?sig=…`` URL —
    only files the media store serves can be attached, so a tool can't hand a client an
    arbitrary (internal) URL. The link is made absolute against :func:`public_base_url`.
    False — and nothing happens — outside an armed turn (a background job), for a non-media
    URL, or past 16 files a turn. A console turn IS armed (it rides A2A ``message/stream``):
    the file part lands on the console's task, which ignores it and renders the inline link."""
    files = _TURN_FILES.get()
    if files is None:
        return False
    if isinstance(ref, MediaRef):
        url, mime, path_name = ref.url, mime or ref.mime, ref.path.name
    else:
        url, path_name = str(ref or ""), ""
    rel = url.split("?", 1)[0]
    if not rel.startswith(MEDIA_URL_PREFIX) or not _NAME_RE.match(rel[len(MEDIA_URL_PREFIX) :]):
        log.warning("[media] attach_file: %r is not a media-store URL — not attached", url[:200])
        return False
    file_name = rel[len(MEDIA_URL_PREFIX) :]
    if not media_public() and not verify_name(file_name, url.partition("sig=")[2].split("&", 1)[0]):
        log.warning("[media] attach_file: %r carries no valid signature — not attached", rel)
        return False
    if not mime:
        found = resolve_media(file_name)
        mime = found[1] if found else (mimetypes.guess_type(file_name)[0] or "application/octet-stream")
    absolute = absolute_media_url(url)
    if any(f["url"] == absolute for f in files):
        return True
    if len(files) >= _MAX_TURN_FILES:
        log.warning("[media] attach_file: turn already carries %d files — %r not attached", _MAX_TURN_FILES, rel)
        return False
    files.append({"url": absolute, "mime": mime, "name": (name or path_name or file_name).strip()[:255]})
    return True
