"""Renderable UI components over the A2A envelope (ADR 0051 Slice 2).

The agent calls the ``show_component`` tool to render a typed, data-only widget inline
in the chat. The tool's return value carries the payload past the LangGraph stream via a
sentinel; ``server/chat.py`` extracts it into a ``("component", payload)`` frame, and the
A2A executor emits it as a ``component-v1`` DataPart — the same typed-DataPart contract as
``tool-call-v1``/``hitl-v1``. The console decodes the MIME and renders a curated widget
(no code execution → safe without a sandbox; free-form generated UI stays on the ADR 0038
iframe/artifact path).
"""

from __future__ import annotations

import json

# MIME the executor stamps on the DataPart and the console matches on.
COMPONENT_MIME = "application/vnd.protolabs.component-v1+json"

# The curated widgets the console knows how to render (ADR 0051 Slice 2).
COMPONENT_TYPES = ("table", "keyvalue", "timeline")

# A marker (record-separator char) prepended to the tool's return so the chat stream can
# recover the structured payload without the model needing to emit raw wire JSON.
_SENTINEL = "\x1e[component-v1]"


def encode_component(component: str, props: dict) -> str:
    """Serialize a component payload behind the sentinel, for a tool return value."""
    return _SENTINEL + json.dumps({"component": component, "props": props}, ensure_ascii=False)


def extract_component(text: str) -> dict | None:
    """Parse a ``{component, props}`` payload out of a sentinel-bearing string, or None.
    Validates the component type so a malformed/unknown payload is ignored."""
    if not isinstance(text, str):
        return None
    i = text.find(_SENTINEL)
    if i < 0:
        return None
    try:
        payload = json.loads(text[i + len(_SENTINEL):])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("component") not in COMPONENT_TYPES:
        return None
    props = payload.get("props")
    return {"component": payload["component"], "props": props if isinstance(props, dict) else {}}


def strip_component(text: str) -> str:
    """Drop the sentinel + payload tail from a tool result, leaving the human prefix."""
    if not isinstance(text, str):
        return text
    i = text.find(_SENTINEL)
    return text[:i].rstrip() if i >= 0 else text
