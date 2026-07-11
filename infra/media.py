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

from infra.paths import instance_paths

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
    """The per-instance URL-signing key (``media/.signing-key``, 0600) — created
    on first use. Independent of the bearer token, so saved URLs survive a token
    rotation; never leaves the server."""
    f = media_dir(create=True) / ".signing-key"
    try:
        existing = f.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    fresh = _secrets.token_hex(32)
    fd, tmp = tempfile.mkstemp(dir=str(f.parent), prefix=".signing-key.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(fresh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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
