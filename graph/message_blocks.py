"""Canonical message-shape primitives — the ONE place that knows what's inside a message.

An Anthropic-style ``AIMessage``'s ``content`` is a block LIST that already contains the
``tool_use`` blocks (arguments included); ``tool_calls`` is LangChain's parsed MIRROR of
those same blocks. Anything that walks messages — exports, chat bundles, context audits —
must treat the two as one thing, or it double-counts / double-renders / double-redacts
the arguments (a live context audit hit exactly that, overstating a thread by ~34k tokens).

The contract that makes composition safe:

- ``text_of``       → the TEXT parts only; a non-text block becomes an ``_[type]_``
                      placeholder, never its payload.
- ``tool_calls_of`` → the tool arguments, exactly once.
- ``role_of``       → the role, tolerant of LangChain objects and plain dicts.

Sum the sizes of ``text_of`` + ``tool_calls_of`` and you have counted the message once.
Grew out of ``graph/export_op.py`` (which re-exports these for its callers); shared with
``graph.chat_bundle`` and ``graph.context_audit_op`` so the walks can't drift.
"""

from __future__ import annotations


def role_of(message) -> str:
    """The message's role, tolerant of LangChain objects and plain dicts."""
    for attr in ("type", "role"):
        value = getattr(message, attr, None) or (message.get(attr) if isinstance(message, dict) else None)
        if value:
            return str(value).lower()
    return type(message).__name__.replace("Message", "").lower() or "unknown"


def text_of(message) -> str:
    """Message content as text. Multi-part content (the vision/tool-block shape) is
    flattened to its text parts so a caller never renders a raw Python repr — and never
    receives a ``tool_use`` block's arguments (those come from ``tool_calls_of``, once).
    """
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{type: text, text: …}, {type: image_url, …}]
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("type"):
                    parts.append(f"_[{block['type']}]_")
        return "\n\n".join(parts)
    return "" if content is None else str(content)


def tool_calls_of(message) -> list[dict]:
    """The message's tool calls (name + args), exactly once — see the module contract."""
    calls = getattr(message, "tool_calls", None)
    if not calls and isinstance(message, dict):
        calls = message.get("tool_calls")
    return list(calls or [])
