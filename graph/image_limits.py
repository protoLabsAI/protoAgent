"""Keep images inside the limits a vision provider enforces (session-poisoning fix).

Why this exists: Anthropic rejects a request outright — HTTP 400 — when any image in it
is too large: more than 8000 px on a side, or more than 2000 px on a side once the
request carries over 20 images ("many-image requests"). An image a tool returned
(``graph.multimodal``) or a user attached lands in the CHECKPOINTED history, so a
single 2560×1600 screenshot is harmless at first and then, the moment the conversation
holds its 21st image, makes EVERY later turn of that session fail. The session is
poisoned, permanently, by data the operator cannot easily see or remove.

Two layers, both here:

1. **At the source** — :func:`fit_image` downsizes an image to ``image_max_side``
   (default 1568 px on the long side, Anthropic's recommended size: larger is
   downscaled server-side anyway, so the extra pixels only cost bytes) and a byte cap
   before it is stored. ``graph.multimodal`` (tool results) and ``server/chat.py``
   (user attachments) call it.
2. **At the request boundary** — :func:`clamp_request_images` rewrites an OUTGOING
   request body (Anthropic Messages wire or OpenAI chat wire) so every inline image
   honours the hard limits, the oldest images past ``max_images_per_request`` become a
   short ``[image omitted: …]`` note, and the images' total size stays inside a
   request budget. It builds new containers instead of mutating, so the stored history
   is never touched — which is what UNPOISONS a session that is already broken: its
   checkpoint still holds the big image, but no request carries it any more.

Downscaling needs Pillow, which core imports lazily and does not require (the desktop
runtime bundles it). Without it, :func:`fit_image` keeps the original (the boundary is
the safety net) and the boundary replaces an image that breaks a HARD limit with the
omitted-image note — degraded, but the turn goes through. Dimensions are always known:
they are read from the PNG/JPEG/GIF/WebP header in pure Python.
"""

from __future__ import annotations

import base64
import binascii
import logging
import struct
from collections import OrderedDict
from io import BytesIO
from typing import Any

log = logging.getLogger("protoagent.image_limits")

# ── Provider limits (Anthropic's; the strictest mainstream vision API) ─────────────────
#: Longest side Anthropic accepts for any image.
HARD_MAX_SIDE = 8000
#: Longest side Anthropic accepts once a request carries more than
#: :data:`MANY_IMAGES_THRESHOLD` images.
MANY_IMAGES_MAX_SIDE = 2000
MANY_IMAGES_THRESHOLD = 20
#: Anthropic caps one image at 5 MB of base64; keep a margin.
HARD_MAX_IMAGE_B64_CHARS = 4_800_000
#: Total base64 image payload one request may carry. Anthropic's request ceiling is
#: 32 MB; the rest of the conversation needs room too.
REQUEST_IMAGE_B64_BUDGET = 20_000_000

# ── Defaults for the configurable knobs (``model.image_max_side`` /
# ``model.max_images_per_request``) — pushed in by :func:`configure` ────────────────────
DEFAULT_MAX_SIDE = 1568
DEFAULT_MAX_IMAGES_PER_REQUEST = 20
#: Source-side byte target for a stored image (decoded bytes).
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

_limits = {"max_side": DEFAULT_MAX_SIDE, "max_images": DEFAULT_MAX_IMAGES_PER_REQUEST}


def configure(*, max_side: int | None = None, max_images_per_request: int | None = None) -> None:
    """Push the operator's limits in (called from ``graph.llm.create_llm``, the one place
    every chat-model build passes through — the same seam the in-flight limiter uses).

    ``max_side`` is clamped into ``[64, MANY_IMAGES_MAX_SIDE]`` so no setting can store
    an image that poisons a many-image request; ``max_images_per_request`` ≤ 0 means no
    count cap (the hard dimension/size limits still apply)."""
    if max_side is not None:
        try:
            _limits["max_side"] = max(64, min(int(max_side), MANY_IMAGES_MAX_SIDE))
        except (TypeError, ValueError):
            _limits["max_side"] = DEFAULT_MAX_SIDE
    if max_images_per_request is not None:
        try:
            _limits["max_images"] = max(0, int(max_images_per_request))
        except (TypeError, ValueError):
            _limits["max_images"] = DEFAULT_MAX_IMAGES_PER_REQUEST


def max_side() -> int:
    return _limits["max_side"]


def max_images_per_request() -> int:
    return _limits["max_images"]


# ── Header sniffing (pure Python) ──────────────────────────────────────────────────────


def image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG / JPEG / GIF / WebP header, or None when the format
    is not recognised or the header is truncated. Never decodes pixels."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = raw[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
            return None
        if raw[:2] == b"\xff\xd8":
            return _jpeg_dimensions(raw)
    except (struct.error, IndexError):
        return None
    return None


_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    i, n = 2, len(raw)
    while i + 4 <= n:
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # standalone markers
            i += 2
            continue
        (seg_len,) = struct.unpack(">H", raw[i + 2 : i + 4])
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", raw[i + 5 : i + 9])
            return w, h
        i += 2 + seg_len
    return None


def b64_dimensions(data: str) -> tuple[int, int] | None:
    """Dimensions of a base64 image, decoding only a prefix when the header is in it."""
    try:
        head = base64.b64decode(data[:65536], validate=False)
    except (binascii.Error, ValueError):
        head = b""
    dims = image_dimensions(head)
    if dims is None and len(data) > 65536:  # a JPEG whose SOF sits behind big APP segments
        try:
            dims = image_dimensions(base64.b64decode(data, validate=False))
        except (binascii.Error, ValueError):
            return None
    return dims


# ── Downscaling (Pillow, optional) ─────────────────────────────────────────────────────


def _pil():
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 — Pillow absent (lean install) → callers degrade
        return None
    return Image, ImageOps


def pillow_available() -> bool:
    return _pil() is not None


def downscale(raw: bytes, *, max_side: int, max_bytes: int) -> tuple[bytes, str] | None:
    """Re-encode ``raw`` to fit ``max_side`` (long side, aspect preserved) and
    ``max_bytes``. JPEG for opaque images, PNG (then WebP) for ones with transparency.
    Returns ``(bytes, mime)``, or None when Pillow is missing or the image can't be
    decoded/fitted (callers then keep or omit the original)."""
    pil = _pil()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        with Image.open(BytesIO(raw)) as src:
            src.seek(0)  # first frame of an animation
            im = ImageOps.exif_transpose(src) or src
            im.load()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            side = max_side
            for _ in range(6):
                w, h = im.size
                scale = min(1.0, side / max(w, h))
                frame = im if scale >= 1.0 else im.resize(
                    (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS
                )
                encodings = (
                    [("PNG", "image/png", {"optimize": True}), ("WEBP", "image/webp", {"quality": 85})]
                    if has_alpha
                    else [("JPEG", "image/jpeg", {"quality": q, "optimize": True}) for q in (85, 70, 55)]
                )
                for fmt, mime, kw in encodings:
                    buf = BytesIO()
                    frame.save(buf, format=fmt, **kw)
                    if buf.tell() <= max_bytes:
                        return buf.getvalue(), mime
                side = int(max(frame.size) * 0.75)
                if side < 64:
                    return None
    except Exception:  # noqa: BLE001 — undecodable / decompression bomb / codec gap
        log.debug("[image_limits] downscale failed", exc_info=True)
    return None


def fit_image(raw: bytes, mime: str, *, max_side: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[bytes, str]:
    """Source-side fit: the image unchanged when it is already within ``max_side`` and
    ``max_bytes``, else a downscaled re-encode. Without Pillow (or for an image Pillow
    can't read) the ORIGINAL comes back — the request boundary still guards it."""
    side = max_side or _limits["max_side"]
    dims = image_dimensions(raw)
    if dims is not None and max(dims) <= side and len(raw) <= max_bytes:
        return raw, mime
    if dims is None and len(raw) <= max_bytes and not pillow_available():
        return raw, mime
    out = downscale(raw, max_side=side, max_bytes=max_bytes)
    if out is None:
        return raw, mime
    if dims is not None and max(dims) <= side and len(out[0]) >= len(raw):
        return raw, mime  # nothing to gain
    return out


def fit_data_uri(uri: str, *, max_side: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """:func:`fit_image` for a ``data:<mime>;base64,…`` URI. Any other URI (http, a
    malformed data URI) is returned unchanged."""
    parsed = _parse_data_uri(uri)
    if parsed is None:
        return uri
    mime, data = parsed
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return uri
    new_raw, new_mime = fit_image(raw, mime, max_side=max_side, max_bytes=max_bytes)
    if new_raw is raw:
        return uri
    return f"data:{new_mime};base64,{base64.b64encode(new_raw).decode()}"


def _parse_data_uri(uri: Any) -> tuple[str, str] | None:
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return None
    header, sep, data = uri.partition(",")
    if not sep or ";base64" not in header:
        return None
    mime = header[5:].split(";", 1)[0] or "image/png"
    return mime, data


# ── Request boundary ───────────────────────────────────────────────────────────────────

# (len, hash, limit) → fitted (b64, mime) or None. Every model call in a tool loop
# re-sends the same history, so an over-limit image would otherwise be re-decoded and
# re-encoded on each one.
_fit_cache: OrderedDict[tuple, tuple[str, str] | None] = OrderedDict()
_FIT_CACHE_MAX = 64


def _fit_b64_hard(data: str, mime: str, side_limit: int) -> tuple[str, str] | None:
    """``(data, mime)`` unchanged when inside the hard limits, a downscaled copy when not,
    or None when it breaks a limit and can't be fixed (→ the caller omits it)."""
    dims = b64_dimensions(data)
    if dims is not None and max(dims) <= side_limit and len(data) <= HARD_MAX_IMAGE_B64_CHARS:
        return data, mime
    if dims is None and len(data) <= HARD_MAX_IMAGE_B64_CHARS:
        return data, mime  # unknown format: nothing to measure, the provider decides
    key = (len(data), hash(data), side_limit)
    if key in _fit_cache:
        _fit_cache.move_to_end(key)
        return _fit_cache[key]
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        raw = b""
    target = min(side_limit, _limits["max_side"]) if dims is not None and max(dims) > side_limit else side_limit
    out = downscale(raw, max_side=target, max_bytes=HARD_MAX_IMAGE_B64_CHARS * 3 // 4) if raw else None
    result = (base64.b64encode(out[0]).decode(), out[1]) if out else None
    _fit_cache[key] = result
    while len(_fit_cache) > _FIT_CACHE_MAX:
        _fit_cache.popitem(last=False)
    if result is None:
        log.warning(
            "[image_limits] an image (%s, %s b64 chars) breaks the provider limit and could not be "
            "downscaled%s; sending a placeholder instead",
            f"{dims[0]}x{dims[1]}" if dims else "unknown size",
            len(data),
            "" if pillow_available() else " (Pillow is not installed)",
        )
    return result


def _image_ref(block: Any, wire: str) -> tuple[str, str] | None:
    """``(mime, base64)`` for an inline image block of ``wire``, else None."""
    if not isinstance(block, dict):
        return None
    if wire == "anthropic":
        src = block.get("source") if block.get("type") == "image" else None
        if isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str):
            return str(src.get("media_type") or "image/png"), src["data"]
        return None
    if block.get("type") == "image_url":
        iu = block.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else iu
        return _parse_data_uri(url)
    return None


def _with_image(block: dict, wire: str, data: str, mime: str) -> dict:
    if wire == "anthropic":
        return {**block, "source": {**block["source"], "media_type": mime, "data": data}}
    iu = block.get("image_url")
    uri = f"data:{mime};base64,{data}"
    return {**block, "image_url": {**iu, "url": uri} if isinstance(iu, dict) else uri}


def _caption(content: list) -> str:
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text") or "").strip():
            text = " ".join(str(b["text"]).split())
            return text[:117] + "…" if len(text) > 120 else text
        if isinstance(b, str) and b.strip():
            text = " ".join(b.split())
            return text[:117] + "…" if len(text) > 120 else text
    return "earlier image"


def _iter_images(messages: list, wire: str):
    """Yield ``(msg_idx, path)`` for every inline image, in conversation order. ``path``
    is a tuple of content indexes (one level of ``tool_result`` nesting on Anthropic)."""
    for mi, msg in enumerate(messages):
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if _image_ref(block, wire):
                yield mi, (bi,)
            elif isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                for ci, inner in enumerate(block["content"]):
                    if _image_ref(inner, wire):
                        yield mi, (bi, ci)


def clamp_request_images(payload: dict, *, wire: str, max_images: int | None = None) -> dict:
    """Return ``payload`` with every inline image inside the provider limits.

    ``wire`` is ``"anthropic"`` (Messages API ``image`` blocks, including those nested in
    ``tool_result``) or ``"openai"`` (chat-completions ``image_url`` data URIs). Steps:

    1. Keep the newest ``max_images`` images (default ``model.max_images_per_request``);
       older ones become ``[image omitted: <caption>]`` text.
    2. Each kept image must fit the hard side limit (2000 px when more than 20 images
       remain, else 8000) and the per-image size cap — downscaled when it doesn't, or
       replaced by the note when it can't be.
    3. Newest-first, images past the request's total image budget are omitted too.

    Copy-on-write: only the messages/blocks that change are rebuilt, so the message
    objects the caller built from (the checkpointed history) are never mutated. A
    payload with no images is returned as the same object.
    """
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return payload
    refs = list(_iter_images(messages, wire))
    if not refs:
        return payload

    cap = _limits["max_images"] if max_images is None else max(0, int(max_images))
    n_drop = len(refs) - cap if cap and len(refs) > cap else 0
    kept = len(refs) - n_drop
    side_limit = MANY_IMAGES_MAX_SIDE if kept > MANY_IMAGES_THRESHOLD else HARD_MAX_SIDE

    def _block_at(mi: int, path: tuple) -> dict:
        b = messages[mi]["content"][path[0]]
        return b["content"][path[1]] if len(path) == 2 else b

    # replacement per image: ("omit", None) | ("image", (data, mime)) | None (unchanged)
    decisions: dict[tuple, tuple[str, Any] | None] = {}
    budget = REQUEST_IMAGE_B64_BUDGET
    for idx in range(len(refs) - 1, -1, -1):  # newest first, so the budget favours recent
        mi, path = refs[idx]
        if idx < n_drop or budget <= 0:
            decisions[(mi, path)] = ("omit", None)
            continue
        block = _block_at(mi, path)
        mime, data = _image_ref(block, wire)
        fitted = _fit_b64_hard(data, mime, side_limit)
        if fitted is None or len(fitted[0]) > budget:
            decisions[(mi, path)] = ("omit", None)
            continue
        budget -= len(fitted[0])
        decisions[(mi, path)] = None if fitted == (data, mime) else ("image", fitted)

    if not any(decisions.values()):
        return payload

    def _rebuild(content: list, prefix: tuple, mi: int) -> list:
        out = []
        for i, block in enumerate(content):
            key = (mi, (*prefix, i))
            if key in decisions and decisions[key] is not None:
                kind, val = decisions[key]
                if kind == "omit":
                    note = {"type": "text", "text": f"[image omitted: {_caption(content)}]"}
                    if isinstance(block, dict) and "cache_control" in block:
                        note["cache_control"] = block["cache_control"]  # keep the cache breakpoint
                    out.append(note)
                else:
                    out.append(_with_image(block, wire, val[0], val[1]))
            elif (
                not prefix
                and isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), list)
                and any(k[0] == mi and len(k[1]) == 2 and k[1][0] == i and v for k, v in decisions.items())
            ):
                out.append({**block, "content": _rebuild(block["content"], (i,), mi)})
            else:
                out.append(block)
        return out

    touched = {mi for (mi, _p), v in decisions.items() if v is not None}
    new_messages = [
        {**msg, "content": _rebuild(msg["content"], (), mi)} if mi in touched else msg
        for mi, msg in enumerate(messages)
    ]
    omitted = sum(1 for v in decisions.values() if v and v[0] == "omit")
    resized = sum(1 for v in decisions.values() if v and v[0] == "image")
    log.info(
        "[image_limits] request images: %d total, %d downscaled, %d omitted (cap %s, side ≤ %d)",
        len(refs),
        resized,
        omitted,
        cap or "none",
        side_limit,
    )
    return {**payload, "messages": new_messages}
