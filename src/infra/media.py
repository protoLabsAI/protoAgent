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
A link handed to a client OUTSIDE this box (an A2A file part, a ``/v1`` reply) is an
expiring one instead (#4258): ``?exp=<unix>&sig=…`` with the expiry inside the HMAC input,
``media.remote_link_ttl_s`` (default 7 days; 0 = permanent). Past it the gate answers 403.

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
import threading
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


def sign_name(name: str, exp: int | str | None = None) -> str:
    """The URL signature for one stored filename (HMAC-SHA256, hex).

    With ``exp`` (unix seconds) the HMAC input is ``"<name>|exp=<exp>"`` — an EXPIRING link
    (#4258): the expiry is inside the signed bytes, so it can't be stretched without the key.
    A store name never contains ``|`` (``_NAME_RE``), so the two input spaces can't collide:
    no permanent signature verifies as an expiring one, or the reverse."""
    msg = name if exp is None else f"{name}|exp={exp}"
    return hmac.new(_signing_key().encode(), msg.encode(), hashlib.sha256).hexdigest()


def verify_name(name: str, sig: str, exp: str | None = None) -> bool:
    """Constant-time check of a ``?sig=`` value against a stored filename (and, for an
    expiring link, its ``?exp=``). Signature only — :func:`check_access` adds the clock."""
    if not name or not sig:
        return False
    if exp is not None and not _EXP_RE.match(str(exp)):
        return False
    return hmac.compare_digest(sign_name(name, exp), sig)


# The query values a link this store minted can carry: a 64-hex HMAC, and an optional
# canonical unix-seconds expiry (no sign, no leading zero — exactly one spelling per instant).
SIG_RE = re.compile(r"^[0-9a-f]{64}$")
_EXP_RE = re.compile(r"^[1-9][0-9]{0,11}$")

# Default lifetime of a link handed to a client outside this box (``media.remote_link_ttl_s``).
DEFAULT_REMOTE_LINK_TTL_S = 7 * 86400


def is_media_name(name: str) -> bool:
    """Whether ``name`` is a servable store filename (one plain segment, no dotfile)."""
    return bool(_NAME_RE.match(name or ""))


def is_link_query_value(key: str, value: str) -> bool:
    """Whether ``value`` is a well-formed ``sig`` / ``exp`` for a media link — shape only,
    never a verification (the fleet hub uses it on a member's link it can't verify)."""
    if key == "sig":
        return bool(SIG_RE.match(value or ""))
    if key == "exp":
        return bool(_EXP_RE.match(value or ""))
    return False


def media_url(name: str, *, ttl_s: int = 0, now: float | None = None) -> str:
    """The signed server-relative link for one stored file: ``/media/<name>?sig=…`` (permanent,
    the console's inline-``<img>`` shape), or with ``ttl_s > 0`` an expiring
    ``/media/<name>?exp=<unix>&sig=…`` for a client outside this box (#4258)."""
    ttl = int(ttl_s or 0)
    if ttl <= 0:
        return f"{MEDIA_URL_PREFIX}{name}?sig={sign_name(name)}"
    exp = str(int(now if now is not None else time.time()) + ttl)
    return f"{MEDIA_URL_PREFIX}{name}?exp={exp}&sig={sign_name(name, exp)}"


def check_access(path: str, sig: str, exp: str = "", *, now: float | None = None) -> str:
    """Auth-middleware hook: may this ``/media/…`` request pass WITHOUT a bearer?

    ``"ok"`` when the store is public (explicit opt-in) or the signature verifies (and an
    expiring link hasn't passed its ``exp``); ``"expired"`` when the signature verifies but
    the link is past its expiry (the middleware answers 403 with a clear body); ``"deny"``
    for anything else — which falls back to the normal bearer/X-API-Key checks, so
    default-deny holds. A link with no ``exp`` is a permanent one (the pre-#4258 shape,
    still valid)."""
    if not path.startswith(MEDIA_URL_PREFIX):
        return "deny"
    name = path[len(MEDIA_URL_PREFIX) :]
    if not _NAME_RE.match(name):
        return "deny"  # traversal / dotfile / nested path — never exempt
    if media_public():
        return "ok"
    if not exp:
        return "ok" if verify_name(name, sig) else "deny"
    if not verify_name(name, sig, exp):
        return "deny"
    return "ok" if int(exp) > (now if now is not None else time.time()) else "expired"


def request_allowed(path: str, sig: str, exp: str = "") -> bool:
    """``check_access(...) == "ok"`` — kept for callers of the pre-expiry seam."""
    return check_access(path, sig, exp) == "ok"


def remote_link_ttl_s() -> int:
    """Lifetime of the links handed to a client outside this box — an A2A file part, a
    ``/v1`` reply (``media.remote_link_ttl_s``, default 7 days; 0 = never expire). Read LIVE
    from the server config; the default outside a server context."""
    try:
        from runtime.state import STATE

        cfg = getattr(STATE, "graph_config", None)
        if cfg is not None:
            return max(0, int(getattr(cfg, "media_remote_link_ttl_s", DEFAULT_REMOTE_LINK_TTL_S) or 0))
    except Exception:  # noqa: BLE001 — a policy probe must never break a turn
        pass
    return DEFAULT_REMOTE_LINK_TTL_S


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

    url = media_url(name)
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


def configured_public_url() -> str:
    """``A2A_PUBLIC_URL`` normalised (whitespace + trailing slash trimmed), or ``""``."""
    return (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")


def _bound_port() -> int | None:
    """``STATE.active_port`` when it's a real port, else ``None``. Note its class default is
    7870 — set before any socket is bound — so it alone doesn't prove a server is serving."""
    try:
        from runtime.state import STATE

        port = getattr(STATE, "active_port", None)
    except Exception:  # noqa: BLE001 — no server state
        return None
    if isinstance(port, int) and not isinstance(port, bool) and port > 0:
        return port
    return None


def public_base_url() -> str:
    """The agent's externally-reachable base URL (no trailing slash), or ``""``.

    The SAME address the A2A card advertises (``server/a2a.py::_a2a_card_url``): an explicit
    ``A2A_PUBLIC_URL`` (set for any deployed agent — behind a proxy or in a container the
    bound port isn't the public address), else the actually-bound loopback port for local and
    desktop runs, where the client is on the same host. Never the bind host or a request's
    forwarded host — so no internal hostname leaks into a link that wasn't already public in
    the card. ``""`` outside a running server (no port bound yet)."""
    base = configured_public_url()
    if base:
        return base
    port = _bound_port()
    return f"http://127.0.0.1:{port}" if port else ""


# Bind spellings a same-host client reaches as ``127.0.0.1``: the wildcards (a ``0.0.0.0`` /
# ``::`` bind listens on loopback too — ``::`` dual-stack) and the ``localhost`` name. ``::1``
# is NOT here: an IPv6-only loopback bind isn't reachable at 127.0.0.1 — it's bracketed.
_WILDCARD_OR_LOCALHOST = {"", "0.0.0.0", "::", "localhost", "localhost."}


def bound_base_url() -> str:
    """``http://<host>:<port>`` the server running in THIS process is bound to, or ``""``
    when none is serving (the server's event loop isn't up — ``active_port``'s class default
    is not a bound port).

    The host is the resolved bind interface (``--host`` / ``network.bind``): the wildcards
    (``0.0.0.0`` / ``::``) and ``localhost`` normalise to ``127.0.0.1``; any other address is
    kept as bound — a loopback URL can't reach a server bound only to a LAN interface (or to
    ``::1``) — with an IPv6 literal bracketed."""
    try:
        from runtime.state import STATE

        if getattr(STATE, "main_loop", None) is None:
            return ""
        host = str(getattr(STATE, "bind_host", "") or "")
    except Exception:  # noqa: BLE001 — no server state: nothing bound
        return ""
    port = _bound_port()
    if not port:
        return ""
    host = host.strip().lower().strip("[]")
    if host in _WILDCARD_OR_LOCALHOST:
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def console_base_url() -> str:
    """The URL this agent's console is served on (no trailing slash), or ``""`` — the
    implementation behind ``graph.sdk.console_url`` (#4256). ``A2A_PUBLIC_URL`` (normalised,
    the same value the card and absolute ``/media`` links use) when set, else
    :func:`bound_base_url`. ``graph.sdk`` adds the fleet-member rule on top."""
    return configured_public_url() or bound_base_url()


# A signed, server-relative media link as this store mints it (permanent or expiring), not
# already part of a URL (no scheme/host/path char immediately before the slash).
_RELATIVE_MEDIA_LINK = re.compile(
    r"(?<![\w/:.\-])/media/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\?(?:exp=(?P<exp>[0-9]+)&)?sig=(?P<sig>[0-9a-f]+)"
)


def absolute_media_url(url: str, base: str | None = None) -> str:
    """A server-relative ``/media/…`` URL made absolute against ``base`` (default
    :func:`public_base_url`). An already-absolute URL, or any URL when no base is known, is
    returned unchanged."""
    url = str(url or "")
    if not url.startswith(MEDIA_URL_PREFIX):
        return url
    b = (public_base_url() if base is None else str(base)).strip().rstrip("/")
    return f"{b}{url}" if b else url


def absolutize_media_links(
    text: str, base: str | None = None, *, ttl_s: int | None = None, links: dict[str, str] | None = None
) -> str:
    """Every signed server-relative ``/media/…?sig=…`` link in ``text`` made absolute — for a
    reply going to a client with no origin to resolve it against (``/v1``). Not for the
    console's stream: it resolves relative links itself, and through a proxy (the fleet hub)
    a relative link is the only one that still works.

    A permanent link whose signature verifies is re-minted as an EXPIRING one (``ttl_s``,
    default :func:`remote_link_ttl_s`; 0 keeps it permanent) — the reply leaves this box, so
    it carries the remote-link lifetime, never the console's permanent signature (#4258).
    ``links`` maps a store filename to the link already minted for it (an attachment), so
    the text and the ``attachments`` field name the same URL. A link that doesn't verify is
    only made absolute, never re-signed."""
    b = (public_base_url() if base is None else str(base)).strip().rstrip("/")
    if not text or not b:
        return text
    ttl = remote_link_ttl_s() if ttl_s is None else max(0, int(ttl_s))
    known = links if links is not None else {}

    def _sub(m: re.Match) -> str:
        name = m.group("name")
        if m.group("exp") is None and ttl > 0 and verify_name(name, m.group("sig")):
            if name not in known:
                known[name] = absolute_media_url(media_url(name, ttl_s=ttl), b)
            return known[name]
        return b + m.group(0)

    return _RELATIVE_MEDIA_LINK.sub(_sub, text)


# ── per-turn file attachments ─────────────────────────────────────────────────────────────
#
# The files a tool attached to the CURRENT turn's answer (``attach_file``), armed per turn by
# the surface that can deliver them — the A2A executor (as FileParts on the answer artifact)
# and ``/v1`` (as links + an ``attachments`` field). ``None`` = no turn armed, and an attach
# is a no-op. One shared, LOCKED holder per turn so a tool's copied context (LangGraph runs
# sync tools in a copy_context thread; parallel tool calls run concurrently) appends to the
# turn's one list, and the 16-file cap is a single check-and-append (#4258).
#
# Its lifecycle (#4258):
#   * ``close_turn_files`` / ``end_turn_files`` CLOSE the holder. A background job or delegate
#     that copied the turn's context and attaches after the answer was built is refused with a
#     log line — never appended to a list nobody will read again.
#   * A turn parking at a HITL interrupt stashes its attachments under the conversation key
#     (``park_turn_files``); the turn that answers the pause re-arms with them
#     (``begin_turn_files(carry=key)``), re-minting each link so its lifetime starts there.
#   * Dedupe is by the stored FILE (the same ref attached twice is one attachment, whatever its
#     link's signature/expiry), and a later attach under the same display ``name`` replaces the
#     earlier one — an overflow retry that re-runs a tool and saves a fresh copy doesn't
#     deliver the file twice.

_MAX_TURN_FILES = 16


class _TurnFiles:
    __slots__ = ("lock", "items", "closed")

    def __init__(self, items: list[dict] | None = None) -> None:
        self.lock = threading.Lock()
        self.items: list[dict] = list(items or [])
        self.closed = False


_TURN_FILES: contextvars.ContextVar[_TurnFiles | None] = contextvars.ContextVar("protoagent_turn_files", default=None)

# Attachments of turns parked at a HITL interrupt, by conversation key → (parked_at, entries).
# In-memory and bounded: a restart drops them (the resumed turn then carries none), an old
# park ages out.
_PARKED: dict[str, tuple[float, list[dict]]] = {}
_PARKED_LOCK = threading.Lock()
_PARKED_MAX = 256
_PARKED_TTL_S = 7 * 86400


def _public(entry: dict) -> dict:
    return {"url": entry["url"], "mime": entry["mime"], "name": entry["name"]}


def _remint(entry: dict) -> dict:
    return {**entry, "url": absolute_media_url(media_url(entry["file"], ttl_s=entry.get("ttl", 0)))}


def begin_turn_files(carry: str = "") -> contextvars.Token:
    """Arm file attachments for the turn running in this context; pass the token to
    :func:`end_turn_files` when the turn ends. ``carry`` names a conversation whose parked
    turn's attachments (:func:`park_turn_files`) this turn takes over — a HITL resume."""
    items: list[dict] = []
    if carry:
        with _PARKED_LOCK:
            got = _PARKED.pop(carry, None)
        if got is not None and time.time() - got[0] <= _PARKED_TTL_S:
            items = [_remint(e) for e in got[1] if resolve_media(e["file"]) is not None]
    return _TURN_FILES.set(_TurnFiles(items))


def park_turn_files(key: str) -> None:
    """Stash the armed turn's attachments under ``key`` (the conversation) when the turn parks
    at a HITL interrupt, merged with any already parked there (a held message re-parks on the
    same pause) — the turn that answers it re-arms with them."""
    tf = _TURN_FILES.get()
    if not key or tf is None:
        return
    with tf.lock:
        mine = [dict(e) for e in tf.items]
    if not mine:
        return
    now = time.time()
    with _PARKED_LOCK:
        prev = _PARKED.pop(key, None)
        merged = list(prev[1]) if prev is not None and now - prev[0] <= _PARKED_TTL_S else []
        have = {e["file"] for e in merged}
        merged.extend(e for e in mine if e["file"] not in have)
        _PARKED[key] = (now, merged[:_MAX_TURN_FILES])
        while len(_PARKED) > _PARKED_MAX:
            _PARKED.pop(next(iter(_PARKED)))


def close_turn_files() -> list[dict]:
    """The armed turn's attachments, read ONCE as the answer is built — the holder closes, so
    any later attach (a background job that copied the turn's context) is refused instead of
    landing in a list nobody reads again."""
    tf = _TURN_FILES.get()
    if tf is None:
        return []
    with tf.lock:
        tf.closed = True
        return [_public(e) for e in tf.items]


def end_turn_files(token: contextvars.Token | None) -> None:
    if token is None:
        return
    tf = _TURN_FILES.get()
    if tf is not None:
        with tf.lock:
            tf.closed = True
    try:
        _TURN_FILES.reset(token)
    except ValueError:  # reset from another context — the turn's own context drops it anyway
        pass


def turn_files() -> list[dict]:
    """The armed turn's attachments so far: ``[{url, mime, name}]`` (a copy)."""
    tf = _TURN_FILES.get()
    if tf is None:
        return []
    with tf.lock:
        return [_public(e) for e in tf.items]


def _query_value(url: str, key: str) -> str:
    query = url.partition("?")[2]
    for pair in query.split("&"):
        k, _, v = pair.partition("=")
        if k == key:
            return v
    return ""


def attach_file(ref: MediaRef | str, *, name: str = "", mime: str = "", ttl_s: int | None = None) -> bool:
    """Attach a media-store file to the CURRENT turn's answer (#4252). True when attached.

    ``ref`` is a :class:`MediaRef` from ``save_media`` or its signed ``/media/…?sig=…`` URL —
    only files the media store serves can be attached, so a tool can't hand a client an
    arbitrary (internal) URL. The delivered link is made absolute against
    :func:`public_base_url` and EXPIRES after ``ttl_s`` seconds (default
    :func:`remote_link_ttl_s`, ``media.remote_link_ttl_s``; 0 = a permanent link) — it goes
    to a client outside this box (#4258).

    False — and nothing happens — outside an armed turn (a background job), after the turn's
    answer was built (a late background/delegated attach), for a non-media, unsigned or
    expired URL, or past 16 files a turn. Attaching the same file twice is one attachment; a
    second file under the same ``name`` replaces the first. A console turn IS armed (it rides
    A2A ``message/stream``): the file part lands on the console's task, which ignores it and
    renders the inline link."""
    tf = _TURN_FILES.get()
    if tf is None:
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
    if check_access(rel, _query_value(url, "sig"), _query_value(url, "exp")) != "ok":
        log.warning("[media] attach_file: %r carries no valid, unexpired signature — not attached", rel)
        return False
    if not mime:
        found = resolve_media(file_name)
        mime = found[1] if found else (mimetypes.guess_type(file_name)[0] or "application/octet-stream")
    ttl = remote_link_ttl_s() if ttl_s is None else max(0, int(ttl_s))
    entry = {
        "file": file_name,
        "url": absolute_media_url(media_url(file_name, ttl_s=ttl)),
        "mime": mime,
        "name": (name or path_name or file_name).strip()[:255],
        "ttl": ttl,
    }
    with tf.lock:
        if tf.closed:
            log.warning(
                "[media] attach_file: the turn's answer was already built — %r not attached "
                "(a background or delegated attach that outlived its turn)",
                rel,
            )
            return False
        for i, f in enumerate(tf.items):
            if f["file"] == file_name:
                return True  # the same file again: one attachment
            if f["name"] == entry["name"]:
                tf.items[i] = entry  # a fresh copy under the same name replaces the first
                return True
        if len(tf.items) >= _MAX_TURN_FILES:
            log.warning("[media] attach_file: turn already carries %d files — %r not attached", _MAX_TURN_FILES, rel)
            return False
        tf.items.append(entry)
    return True
