"""Multimodal tool results (#1930) — let a tool return an image the model can SEE.

Tool results are text-only ``ToolMessage``s by default, so a vision-capable chat
model could never look at an image a tool just produced (a generated chart, a
screenshot, a protobanana render) — it only read a path/URL string. This module
is the OPT-IN envelope for the tool→model direction, mirroring the inbound
vision path (``a2a_impl/executor.py::_extract_image_parts`` → ``image_url``
blocks gated on ``config.model_vision``).

How it works (the ``graph/components.py`` sentinel idiom):

- A tool that wants the model to see an image returns
  ``multimodal_tool_result(text, images=[{"b64"|"path": …, "mime": …}])`` —
  a sentinel-prefixed JSON string, so ordinary string-returning tools are
  untouched by construction (nothing is ever duck-typed).
- ``MultimodalToolResultMiddleware`` (graph/middleware/multimodal_tool.py)
  detects the sentinel on the finished ``ToolMessage`` and rewrites its content:
  vision-capable model (``model.vision: true``) → ``[{"type": "text"}, {"type":
  "image_url"}…]`` content blocks; text-only model → the text part alone,
  optionally enriched by the ``knowledge.image_describe_model`` describe path
  (#1381) so the model still "sees" a description instead of nothing.

Limits (context cost — enforced in ``render_multimodal_content`` and documented
in the plugin devkit): at most ``MAX_IMAGES_PER_RESULT`` images per tool result
and ``MAX_IMAGE_BYTES`` decoded bytes per image. An image over a limit is
dropped with an inline note (no downscaling — that would need an image
dependency core doesn't carry); the text part always survives.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

# Marker (record-separator char) prepended to the tool's return value — the same
# out-of-band idiom as graph/components.py, so envelope detection is one
# ``startswith`` and can never trigger on ordinary tool output.
_SENTINEL = "\x1e[multimodal-tool-v1]"

# Guardrails per ToolMessage (context cost). An oversized/extra image is dropped
# with an inline note; the text part is never dropped.
MAX_IMAGES_PER_RESULT = 3
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # decoded bytes per image

# ``(image_bytes, mime, filename) -> description`` — the shape
# graph/llm.py::create_describe_image_fn builds (#1381).
DescribeFn = Callable[[bytes, str, str], str]


def multimodal_tool_result(text: str, images: list[dict]) -> str:
    """Build a tool return value that carries images for the model to see.

    Each image is ``{"b64": <base64 str>}`` or ``{"path": <file path>}`` plus an
    optional ``"mime"`` (default ``image/png``). Enforces the module limits
    eagerly — a ``ValueError`` here surfaces in the tool's own result, at the
    source, instead of a silent drop later. Returns the sentinel-prefixed JSON
    envelope the middleware rewrites; everything else about the tool (schema,
    docstring, registration) stays a plain string-returning tool.
    """
    if not isinstance(images, list) or not images:
        raise ValueError("multimodal_tool_result needs at least one image (else just return the text)")
    if len(images) > MAX_IMAGES_PER_RESULT:
        raise ValueError(f"too many images: {len(images)} > MAX_IMAGES_PER_RESULT={MAX_IMAGES_PER_RESULT}")
    out: list[dict] = []
    for i, img in enumerate(images, start=1):
        if not isinstance(img, dict):
            raise ValueError(f"image #{i} must be a dict with 'b64' or 'path'")
        mime = str(img.get("mime") or "image/png")
        if not mime.startswith("image/"):
            raise ValueError(f"image #{i} has non-image mime {mime!r}")
        if img.get("b64"):
            b64 = str(img["b64"])
            try:
                raw = base64.b64decode(b64, validate=True)
            except (ValueError, TypeError) as e:
                raise ValueError(f"image #{i} is not valid base64: {e}") from e
        elif img.get("path"):
            with open(img["path"], "rb") as f:
                raw = f.read()
            b64 = base64.b64encode(raw).decode()
        else:
            raise ValueError(f"image #{i} must carry 'b64' or 'path'")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"image #{i} is {len(raw)} bytes > MAX_IMAGE_BYTES={MAX_IMAGE_BYTES} — "
                "downscale it in the tool before returning"
            )
        out.append({"b64": b64, "mime": mime})
    return _SENTINEL + json.dumps({"text": str(text or ""), "images": out}, ensure_ascii=False)


def is_multimodal_result(content) -> bool:
    """One cheap check — the ONLY thing the middleware pays on the hot path for
    every ordinary tool result."""
    return isinstance(content, str) and content.startswith(_SENTINEL)


def parse_multimodal_result(content) -> dict | None:
    """Parse the envelope out of a sentinel-bearing tool result, or ``None`` when
    the content is an ordinary (non-envelope) result.

    A sentinel with a malformed payload does NOT return None — leaving a broken
    multi-MB base64 blob in the ToolMessage would flood the context — it degrades
    to an empty-image envelope with an explanatory text."""
    if not is_multimodal_result(content):
        return None
    try:
        payload = json.loads(content[len(_SENTINEL) :])
        if not isinstance(payload, dict):
            raise ValueError("envelope is not an object")
    except (ValueError, TypeError):
        return {"text": "[multimodal tool result was malformed and its payload was dropped]", "images": []}
    images = payload.get("images")
    return {
        "text": str(payload.get("text") or ""),
        "images": [i for i in images if isinstance(i, dict)] if isinstance(images, list) else [],
    }


def render_multimodal_content(env: dict, *, vision: bool, describe_fn: DescribeFn | None = None) -> str | list:
    """Render a parsed envelope into final ``ToolMessage`` content.

    - ``vision=True`` → content blocks: one ``text`` block (caption + any
      guardrail notes) followed by an ``image_url`` data-URI block per accepted
      image — the exact shape the inbound vision path sends (server/chat.py).
    - ``vision=False`` → a plain string: the caption plus, per image, either the
      ``describe_fn`` description (the #1381 fallback) or an omission note.

    Guardrails are re-enforced here (the envelope may not have come from the
    helper): images beyond ``MAX_IMAGES_PER_RESULT`` or over ``MAX_IMAGE_BYTES``
    decoded bytes are dropped with an inline note. May do blocking work (file
    read for ``path`` images, a describe model call) — the middleware runs it
    off-loop on the async path.
    """
    text = str(env.get("text") or "")
    notes: list[str] = []
    accepted: list[tuple[str, str, bytes]] = []  # (b64, mime, raw)

    images = list(env.get("images") or [])
    if len(images) > MAX_IMAGES_PER_RESULT:
        notes.append(f"[{len(images) - MAX_IMAGES_PER_RESULT} image(s) dropped: max {MAX_IMAGES_PER_RESULT} per tool result]")
        images = images[:MAX_IMAGES_PER_RESULT]

    for i, img in enumerate(images, start=1):
        mime = str(img.get("mime") or "image/png")
        if not mime.startswith("image/"):
            notes.append(f"[image {i} dropped: non-image mime {mime!r}]")
            continue
        try:
            if img.get("b64"):
                b64 = str(img["b64"])
                raw = base64.b64decode(b64, validate=True)
            elif img.get("path"):
                with open(img["path"], "rb") as f:
                    raw = f.read()
                b64 = base64.b64encode(raw).decode()
            else:
                notes.append(f"[image {i} dropped: no 'b64' or 'path']")
                continue
        except (ValueError, TypeError, OSError) as e:
            notes.append(f"[image {i} dropped: {e}]")
            continue
        if len(raw) > MAX_IMAGE_BYTES:
            notes.append(
                f"[image {i} dropped: {len(raw)} bytes exceeds the {MAX_IMAGE_BYTES}-byte limit — "
                "the tool should downscale before returning]"
            )
            continue
        accepted.append((b64, mime, raw))

    caption = "\n".join(s for s in (text, *notes) if s).strip() or "(image tool result)"

    if vision and accepted:
        blocks: list[dict] = [{"type": "text", "text": caption}]
        blocks += [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            for b64, mime, _raw in accepted
        ]
        return blocks

    # Text-only model (or nothing survived the guardrails): degrade gracefully —
    # same contract as the #1381 attachment fallback. Never raises past here.
    parts = [caption]
    for i, (_b64, mime, raw) in enumerate(accepted, start=1):
        described = None
        if describe_fn is not None:
            try:
                described = (describe_fn(raw, mime, f"tool-image-{i}") or "").strip()
            except Exception:  # noqa: BLE001 — a describe outage must not fail the tool result
                described = None
        if described:
            parts.append(f"[image {i} ({mime}) — description for a text-only model: {described}]")
        else:
            parts.append(
                f"[image {i} ({mime}, {len(raw)} bytes) attached but the active model is text-only; "
                "configure model.vision or knowledge.image_describe_model to see it]"
            )
    return "\n".join(parts)
