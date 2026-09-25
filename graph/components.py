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

# The curated widgets the console knows how to render (ADR 0051 Slice 2). ``code-ref``
# (ADR 0112) is a pointer into a fenced project file — the console renders it as a chip
# that opens the code pane. It is emitted only by the ``show_code`` fs tool, which
# validates the fence/secret/range first; ``show_component`` refuses to build one.
COMPONENT_TYPES = ("table", "keyvalue", "timeline", "code-ref")

# code-ref prop limits (ADR 0112). A ref is a POINTER, never content: every prop is a
# short string or a positive int, and anything else drops the whole component.
CODE_REF_NOTE_MAX = 280
_CODE_REF_STR_MAX = {"project": 200, "path": 4096, "note": CODE_REF_NOTE_MAX}


def validate_component_props(component: str, props: dict) -> str | None:
    """Why ``props`` is not a valid payload for ``component``, or ``None`` when it is.

    Only ``code-ref`` has a strict schema (ADR 0112); the ADR 0051 widgets are
    free-form data the console renders defensively, so they pass unchanged.
    """
    if component != "code-ref":
        return None
    if not isinstance(props, dict):
        return "props must be an object"
    extra = set(props) - {"project", "path", "line", "end_line", "note"}
    if extra:
        return f"unexpected code-ref props: {', '.join(sorted(extra))}"
    for key, limit in _CODE_REF_STR_MAX.items():
        val = props.get(key, "")
        if not isinstance(val, str):
            return f"code-ref {key} must be a string"
        if len(val) > limit:
            return f"code-ref {key} is longer than {limit} chars"
    if not props.get("project") or not props.get("path"):
        return "code-ref needs a project and a path"
    line, end = props.get("line"), props.get("end_line")
    # bool is an int subclass — `True` is not a line number.
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return "code-ref line must be a positive integer"
    if not isinstance(end, int) or isinstance(end, bool) or end < line:
        return "code-ref end_line must be an integer >= line"
    return None


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
        payload = json.loads(text[i + len(_SENTINEL) :])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("component") not in COMPONENT_TYPES:
        return None
    props = payload.get("props")
    props = props if isinstance(props, dict) else {}
    if validate_component_props(payload["component"], props) is not None:
        return None
    return {"component": payload["component"], "props": props}


def strip_component(text: str) -> str:
    """Drop the sentinel + payload tail from a tool result, leaving the human prefix."""
    if not isinstance(text, str):
        return text
    i = text.find(_SENTINEL)
    return text[:i].rstrip() if i >= 0 else text
