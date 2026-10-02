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
   request body (Anthropic Messages, OpenAI chat-completions or OpenAI Responses) so
   every image honours the hard limits: all images — inline and provider-fetched —
   count toward the many-image rule, the oldest past ``max_images_per_request`` become
   a short ``[image omitted: …]`` note, and the total stays inside a request budget. It
   builds new containers instead of mutating, so the stored history is never touched —
   which is what UNPOISONS a session that is already broken: its checkpoint still holds
   the big image, but no request carries it any more.

Downscaling uses Pillow (a core dependency). Decoding is bounded: a canvas over
:data:`MAX_DECODE_PIXELS` is refused from its header before any pixel is decoded, so a
tiny file declaring a gigantic image can't balloon memory on the request path. If
Pillow is somehow unavailable (a broken install), the same rules hold without it:
an image that would break the many-image limit is dropped at storage time and replaced
by the omitted note at the boundary — degraded, but never a poisoned session.
Dimensions are always read from the PNG/JPEG/GIF/WebP header in pure Python.
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


# ── Downscaling (Pillow) ───────────────────────────────────────────────────────────────

#: The most pixels we will ever DECODE. A small file can declare a huge canvas (a
#: 13000×13000 PNG compresses to under 1 MB and decodes to ~500 MB of RGB), so anything
#: past this is refused from its header, before a single pixel is decoded. 40 MP is
#: above every real screenshot/photo a tool or user sends (an 8K frame is 33 MP).
MAX_DECODE_PIXELS = 40_000_000


def _pil():
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 — Pillow missing (it is a core dep; a broken install) → callers degrade
        return None
    return Image, ImageOps


def pillow_available() -> bool:
    return _pil() is not None


def _too_many_pixels(dims: tuple[int, int] | None) -> bool:
    return dims is not None and dims[0] * dims[1] > MAX_DECODE_PIXELS


def _to_8bit(im):
    """Bring a high-bit-depth single-channel image (``I;16*``, ``I``, ``F``) into 8-bit ``L``.

    Pillow's ``convert("L"/"RGB")`` CLIPS these modes rather than scaling them, so a
    16-bit PNG came out almost entirely white. 16-bit data is scaled by 1/257 (keeps the
    absolute brightness); anything wider is scaled by its own maximum."""
    if im.mode.startswith("I;16"):
        im = im.convert("I")
    if im.mode not in ("I", "F"):
        return im
    lo, hi = im.getextrema()
    if hi > 255:
        divisor = 257.0 if hi <= 65535 else hi / 255.0
        im = im.point(lambda v: v * (1.0 / divisor))
    return im.convert("L")


def downscale(raw: bytes, *, max_side: int, max_bytes: int) -> tuple[bytes, str] | None:
    """Re-encode ``raw`` to fit ``max_side`` (long side, aspect preserved) and
    ``max_bytes``. JPEG for opaque images, PNG (then WebP) for ones with transparency.
    Returns ``(bytes, mime)``, or None when Pillow is missing, the canvas is over
    :data:`MAX_DECODE_PIXELS`, or the image can't be decoded/fitted."""
    pil = _pil()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        with Image.open(BytesIO(raw)) as src:
            # ``open`` reads only the header: refuse a decompression bomb BEFORE decoding
            # (covers formats the pure-Python sniffer doesn't know, too).
            w0, h0 = src.size
            if w0 * h0 > MAX_DECODE_PIXELS:
                return None
            src.seek(0)  # first frame of an animation
            if src.format == "JPEG":
                # Let libjpeg decode at 1/2, 1/4 or 1/8 scale — never below the target.
                scale = min(1.0, max_side / max(w0, h0))
                src.draft("RGB", (max(1, int(w0 * scale) + 1), max(1, int(h0 * scale) + 1)))
            im = ImageOps.exif_transpose(src) or src
            im.load()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = _to_8bit(im)
            im = im.convert("RGBA" if has_alpha else "RGB")
            side = max_side
            for _ in range(6):
                frame = im.copy()
                frame.thumbnail((side, side), Image.LANCZOS, reducing_gap=3.0)
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
    except Exception:  # noqa: BLE001 — undecodable / Pillow's own bomb guard / codec gap
        log.debug("[image_limits] downscale failed", exc_info=True)
    return None


def fit_image(
    raw: bytes, mime: str, *, max_side: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[bytes, str] | None:
    """Source-side fit, run before an image is STORED in the history.

    Returns the image unchanged when it is already within ``max_side`` and
    ``max_bytes``, else a downscaled re-encode. Returns **None** — the caller must drop
    the image, with a note — when it can't be made safe: a canvas over
    :data:`MAX_DECODE_PIXELS`, or an image over the 2000 px many-image limit that could
    not be downscaled (undecodable, or Pillow missing from a broken install). Storing
    such an image is what poisons a session, so it is never kept. An image whose only
    problem is unknown dimensions or bytes comes back unchanged; the caller's own byte
    cap and the request boundary still apply."""
    side = max_side or _limits["max_side"]
    dims = image_dimensions(raw)
    if _too_many_pixels(dims):
        return None
    if dims is not None and max(dims) <= side and len(raw) <= max_bytes:
        return raw, mime
    out = downscale(raw, max_side=side, max_bytes=max_bytes)
    if out is None:
        if dims is not None and max(dims) > MANY_IMAGES_MAX_SIDE:
            return None
        return raw, mime
    if dims is not None and max(dims) <= side and len(out[0]) >= len(raw):
        return raw, mime  # nothing to gain
    return out


def fit_data_uri(uri: str, *, max_side: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> str | None:
    """:func:`fit_image` for a ``data:<mime>;base64,…`` URI. Any other URI (http, a
    malformed data URI) is returned unchanged; None means the image must be dropped."""
    parsed = _parse_data_uri(uri)
    if parsed is None:
        return uri
    mime, data = parsed
    if _too_many_pixels(b64_dimensions(data)):
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return uri
    fitted = fit_image(raw, mime, max_side=max_side, max_bytes=max_bytes)
    if fitted is None:
        return None
    new_raw, new_mime = fitted
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
    if _too_many_pixels(dims):
        log.warning("[image_limits] an image of %dx%d px exceeds the decode cap; sending a placeholder", *dims)
        return None
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
            "downscaled; sending a placeholder instead",
            f"{dims[0]}x{dims[1]}" if dims else "unknown size",
            len(data),
        )
    return result


def _image_kind(block: Any) -> tuple[str, str | None, str | None] | None:
    """Classify an image block in ANY of the three wires this runtime speaks:

    - Anthropic Messages ``{"type": "image", "source": {...}}``
    - OpenAI chat ``{"type": "image_url", "image_url": {"url": ...}}``
    - OpenAI Responses ``{"type": "input_image", "image_url": "...", "file_id"?}``

    → ``("b64", mime, data)`` for an inline base64 image (measurable, downscalable),
    ``("ref", None, None)`` for one the provider fetches itself (http URL, file id — it
    still COUNTS toward the many-image limit, but can't be measured or resized), or
    None for a non-image block."""
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "image":
        src = block.get("source")
        if isinstance(src, dict):  # Anthropic wire
            if src.get("type") == "base64" and isinstance(src.get("data"), str):
                return "b64", str(src.get("media_type") or "image/png"), src["data"]
            return "ref", None, None
        # LangChain standard blocks (seen on stored messages, before wire conversion):
        # v1 ``{"base64": …}`` / v0 ``{"source_type": "base64", "data": …}``.
        data = block.get("base64") or (block.get("data") if block.get("source_type") == "base64" else None)
        if isinstance(data, str):
            return "b64", str(block.get("mime_type") or "image/png"), data
        return "ref", None, None
    if kind == "image_url":
        iu = block.get("image_url")
        parsed = _parse_data_uri(iu.get("url") if isinstance(iu, dict) else iu)
        return ("b64", *parsed) if parsed else ("ref", None, None)
    if kind == "input_image":
        parsed = _parse_data_uri(block.get("image_url"))
        return ("b64", *parsed) if parsed else ("ref", None, None)
    return None


def _with_image(block: dict, data: str, mime: str) -> dict:
    uri = f"data:{mime};base64,{data}"
    kind = block.get("type")
    if kind == "image":
        if isinstance(block.get("source"), dict):
            return {**block, "source": {**block["source"], "media_type": mime, "data": data}}
        return {**block, ("base64" if "base64" in block else "data"): data, "mime_type": mime}
    if kind == "input_image":
        return {**block, "image_url": uri}
    iu = block.get("image_url")
    return {**block, "image_url": {**iu, "url": uri} if isinstance(iu, dict) else uri}


def _caption(content: list) -> str:
    for b in content:
        text = ""
        if isinstance(b, dict) and b.get("type") in ("text", "input_text"):
            text = str(b.get("text") or "")
        elif isinstance(b, str):
            text = b
        text = " ".join(text.split())
        if text:
            return text[:117] + "…" if len(text) > 120 else text
    return "earlier image"


# A content list is addressed as (top_key, item_idx, field, outer_idx); outer_idx is the
# index of an Anthropic ``tool_result`` block whose nested ``content`` holds the list, or
# None for the item's own list. An image is that address plus its index in the list.
_TOP_FIELDS = {"messages": ("content",), "input": ("content", "output")}


def _content_lists(payload: dict):
    for top, fields in _TOP_FIELDS.items():
        items = payload.get(top)
        if not isinstance(items, list):
            continue
        for mi, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for field in fields:
                content = item.get(field)
                if not isinstance(content, list):
                    continue
                yield (top, mi, field, None), content
                for bi, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                        yield (top, mi, field, bi), block["content"]


def _plan(kinds: list[str], max_images: int | None = None) -> tuple[int, int, bool, int]:
    """``(cap, n_drop, omit_refs, side_limit)`` for a request whose images, oldest
    first, are of ``kinds`` (``"b64"`` / ``"ref"``) — shared by the clamp and the
    off-loop prewarm so both reach the same side limit (and so the same cache keys)."""
    cap = _limits["max_images"] if max_images is None else max(0, int(max_images))
    n_drop = len(kinds) - cap if cap and len(kinds) > cap else 0
    kept = kinds[n_drop:]
    omit_refs = len(kept) > MANY_IMAGES_THRESHOLD
    n_kept = len(kept) - (kept.count("ref") if omit_refs else 0)
    return cap, n_drop, omit_refs, MANY_IMAGES_MAX_SIDE if n_kept > MANY_IMAGES_THRESHOLD else HARD_MAX_SIDE


def _needs_work(data: str, side_limit: int) -> bool:
    dims = b64_dimensions(data)
    if _too_many_pixels(dims):
        return False  # refused from the header: no decode, nothing to precompute
    return len(data) > HARD_MAX_IMAGE_B64_CHARS or (dims is not None and max(dims) > side_limit)


async def aprewarm(messages: Any) -> None:
    """Do the expensive part of :func:`clamp_request_images` OFF the event loop.

    The clamp runs inside the client's synchronous ``_get_request_payload``, which the
    async stream path calls on the loop; decoding and re-encoding a large image there
    would stall every other coroutine. The async entry points await this first: it reads
    headers (cheap), and only if some image actually needs downscaling does it fill the
    fit cache in a worker thread, so the clamp then finds every result cached. Never
    raises — on any problem the clamp simply does the work itself."""
    try:
        images: list[tuple[str, str | None, str | None]] = []
        for msg in messages or []:
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                kind = _image_kind(block)
                if kind:
                    images.append(kind)
        if not images:
            return
        _cap, n_drop, _omit_refs, side_limit = _plan([k[0] for k in images])
        todo = [(data, mime) for kind, mime, data in images[n_drop:] if kind == "b64" and _needs_work(data, side_limit)]
        if not todo:
            return
        import asyncio

        await asyncio.to_thread(lambda: [_fit_b64_hard(data, mime, side_limit) for data, mime in todo])
    except Exception:  # noqa: BLE001 — prewarming is an optimisation, never a failure
        log.debug("[image_limits] prewarm skipped", exc_info=True)


def clamp_request_images(payload: dict, *, max_images: int | None = None) -> dict:
    """Return ``payload`` with every image inside the provider limits.

    Works on an Anthropic Messages body (``image`` blocks, including those nested in
    ``tool_result``), an OpenAI chat-completions body (``image_url``) and an OpenAI
    Responses body (``input`` items' ``input_image`` in ``content``/``output``). Steps:

    1. EVERY image counts — inline base64 and provider-fetched (URL / file id) alike.
       Keep the newest ``max_images`` (default ``model.max_images_per_request``); older
       ones become ``[image omitted: <caption>]`` text.
    2. If more than 20 images remain, the provider applies its 2000 px many-image limit
       to all of them — including the URL images it fetches, which we can neither
       measure nor resize, so those are omitted too in that case.
    3. Each kept inline image must fit the side limit (2000 px when more than 20 remain,
       else 8000) and the per-image size cap — downscaled when it doesn't, or omitted
       when it can't be (including any canvas over :data:`MAX_DECODE_PIXELS`).
    4. Newest-first, inline images past the request's total image budget are omitted.

    Copy-on-write: only the items/blocks that change are rebuilt, so the message objects
    the caller built from (the checkpointed history) are never mutated. A payload with
    no images is returned as the same object.
    """
    if not isinstance(payload, dict):
        return payload
    lists = list(_content_lists(payload))
    refs: list[tuple[tuple, int, tuple]] = []  # (list address, index, kind)
    for addr, content in lists:
        for i, block in enumerate(content):
            kind = _image_kind(block)
            if kind:
                refs.append((addr, i, kind))
    if not refs:
        return payload

    cap, n_drop, omit_refs, side_limit = _plan([k[0] for _a, _i, k in refs], max_images)
    omit: set[tuple] = {(addr, i) for addr, i, _k in refs[:n_drop]}
    kept = refs[n_drop:]
    if omit_refs:
        omit.update((addr, i) for addr, i, k in kept if k[0] == "ref")
        kept = [r for r in kept if (r[0], r[1]) not in omit]

    replace: dict[tuple, tuple[str, str]] = {}
    budget = REQUEST_IMAGE_B64_BUDGET
    for addr, i, (kind, mime, data) in reversed(kept):  # newest first: the budget favours recent
        if kind != "b64":
            continue
        fitted = _fit_b64_hard(data, mime, side_limit) if budget > 0 else None
        if fitted is None or len(fitted[0]) > budget:
            omit.add((addr, i))
            continue
        budget -= len(fitted[0])
        if fitted != (data, mime):
            replace[(addr, i)] = fitted

    if not omit and not replace:
        return payload

    content_by_addr = dict(lists)

    def _rebuilt(addr: tuple) -> list:
        content = content_by_addr[addr]
        out = []
        for i, block in enumerate(content):
            key = (addr, i)
            if key in omit:
                note = {
                    "type": "input_text" if block.get("type") == "input_image" else "text",
                    "text": f"[image omitted: {_caption(content)}]",
                }
                if "cache_control" in block:
                    note["cache_control"] = block["cache_control"]  # keep the cache breakpoint
                out.append(note)
            elif key in replace:
                out.append(_with_image(block, *replace[key]))
            elif addr[3] is None and (addr[:3] + (i,)) in touched_addrs:
                out.append({**block, "content": _rebuilt(addr[:3] + (i,))})
            else:
                out.append(block)
        return out

    touched_addrs = {addr for addr, _i in (*omit, *replace)}
    new_payload = dict(payload)
    for top in _TOP_FIELDS:
        items = payload.get(top)
        if not isinstance(items, list):
            continue
        touched_items = {a[1] for a in touched_addrs if a[0] == top}
        if not touched_items:
            continue
        new_items = list(items)
        for mi in touched_items:
            item = dict(items[mi])
            for field in {a[2] for a in touched_addrs if a[0] == top and a[1] == mi}:
                item[field] = _rebuilt((top, mi, field, None))
            new_items[mi] = item
        new_payload[top] = new_items

    log.info(
        "[image_limits] request images: %d total, %d downscaled, %d omitted (cap %s, side ≤ %d)",
        len(refs),
        len(replace),
        len(omit),
        cap or "none",
        side_limit,
    )
    return new_payload
