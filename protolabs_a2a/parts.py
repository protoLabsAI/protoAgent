"""A2A 1.0 Part build/read helpers — the protoLabs wire shape.

A2A 1.0 made ``Part`` a member-discriminated union: the payload lives under a
``content`` member tagged with ``$case`` (``"text"`` | ``"data"`` | ``"file"``),
and a custom DataPart additionally carries a ``metadata.mimeType`` discriminator
plus ``mediaType`` / ``filename`` envelope fields. This is the encoding the TS
reference layer (``@protolabs/a2a``) emits, and the one this package mirrors so
the hub and every agent interoperate byte-for-byte.

Canonical custom DataPart on the wire::

    {
      "content": {"$case": "data", "value": <payload>},
      "metadata": {"mimeType": "<MIME>"},
      "filename": "",
      "mediaType": "application/json"
    }

These helpers produce / read that exact shape as plain ``dict`` objects. They do
NOT depend on ``a2a-sdk``'s protobuf ``Part`` — the protobuf JSON serializer
flattens the ``content`` oneof to a top-level ``data`` field, which is a
different on-the-wire encoding. Producing the dicts directly keeps the protoLabs
wire shape stable regardless of which A2A runtime serializes the envelope.
``read_*`` accepts both encodings so a part produced by either side parses.
"""

from __future__ import annotations

from typing import Any

# Discriminator location for a custom DataPart: the MIME lives in
# ``metadata.mimeType`` (NOT ``mediaType`` — that stays "application/json").
MIME_KEY = "mimeType"
DATA_MEDIA_TYPE = "application/json"


def text_part(text: str) -> dict[str, Any]:
    """Build a 1.0 TextPart in the member-discriminated shape."""
    return {"content": {"$case": "text", "value": text}}


def data_part(payload: Any, mime_type: str) -> dict[str, Any]:
    """Build a 1.0 custom DataPart carrying ``payload`` under ``mime_type``.

    ``mime_type`` is the discriminator a consumer matches on (it lands in
    ``metadata.mimeType``); ``mediaType`` is always ``application/json`` and
    ``filename`` is empty for a JSON data payload, per the contract.
    """
    return {
        "content": {"$case": "data", "value": payload},
        "metadata": {MIME_KEY: mime_type},
        "filename": "",
        "mediaType": DATA_MEDIA_TYPE,
    }


def read_text(part: dict[str, Any]) -> str | None:
    """Return the text of a TextPart, or None if ``part`` is not text.

    Accepts both the member-discriminated 1.0 shape (``content.$case == 'text'``)
    and the flattened proto-JSON shape (top-level ``text``).
    """
    content = part.get("content")
    if isinstance(content, dict) and content.get("$case") == "text":
        value = content.get("value")
        return value if isinstance(value, str) else None
    # Flattened proto-JSON / 0.3 ``{"kind": "text", "text": ...}`` fallback.
    if isinstance(part.get("text"), str):
        return part["text"]
    return None


def read_data(part: dict[str, Any]) -> tuple[str | None, Any]:
    """Return ``(mime_type, payload)`` for a DataPart, or ``(None, None)``.

    Reads the discriminator from ``metadata.mimeType`` and the payload from
    ``content.value`` (1.0 member-discriminated) or top-level ``data``
    (flattened proto-JSON). Non-data parts return ``(None, None)``.
    """
    metadata = part.get("metadata") or {}
    mime = metadata.get(MIME_KEY) if isinstance(metadata, dict) else None

    content = part.get("content")
    if isinstance(content, dict) and content.get("$case") == "data":
        return mime, content.get("value")
    if "data" in part:  # flattened proto-JSON
        return mime, part.get("data")
    return None, None


def part_mime(part: dict[str, Any]) -> str | None:
    """The ``metadata.mimeType`` discriminator of a part, or None."""
    metadata = part.get("metadata") or {}
    return metadata.get(MIME_KEY) if isinstance(metadata, dict) else None
