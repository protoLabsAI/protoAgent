"""A2A 1.0 response parse helpers — shared by the delegates a2a adapter.

This module used to ship the ``peer_consult`` / ``peer_list`` tools (env-var peer
federation). Those were **retired**: ``delegate_to`` over the unified delegate
registry (ADR 0025, ``plugins/delegates``) is the one federation tool — same A2A
consult alongside openai/acp delegates, with a console panel. What remains here
are the two pure parse helpers the a2a adapter still reuses to read a reply off
an A2A 1.0 ``SendMessage`` / ``GetTask`` result.
"""

from __future__ import annotations


def _extract_text(result) -> str | None:
    """Pull text out of an A2A 1.0 result — a ``{"task": ...}`` envelope (the
    ``SendMessage`` / ``GetTask`` response) or a bare Message. Tolerant of parts
    with or without an explicit ``kind`` tag (1.0 text parts carry just ``text``)."""
    if not isinstance(result, dict):
        return None
    task = result.get("task", result) or {}
    for art in task.get("artifacts") or []:
        chunks = [p.get("text", "") for p in art.get("parts", []) if p.get("text")]
        if any(chunks):
            return "\n".join(c for c in chunks if c)
    msg = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in (msg.get("parts") or []) if p.get("text")]
    text = "\n".join(p for p in parts if p)
    return text or None


_TERMINAL = {"completed", "failed", "canceled"}  # v0.3 spellings (back-compat)


def _is_terminal(state) -> bool:
    """True for A2A 1.0 terminal task states (``TASK_STATE_COMPLETED`` / ``FAILED``
    / ``CANCELLED`` / ``REJECTED``) and their v0.3 lowercase spellings."""
    return str(state or "").upper().endswith(("COMPLETED", "FAILED", "CANCELED", "CANCELLED", "REJECTED"))
