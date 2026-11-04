"""Failure text that is safe to store and show (#4726).

An error message from an exception or a provider can carry a credential: an echoed
header, a URL with a key in it. Every place that stores or returns one (a turn's
telemetry row, a background job's ``error``) goes through :func:`safe_error_text`, so the
rule lives in one spot.
"""

from __future__ import annotations

#: What a caller gets when the redactor itself fails. Fail closed: raw text is never kept.
WITHHELD = "(error text withheld: redaction failed)"


def safe_error_text(text: object, *, max_len: int = 500) -> str:
    """``text`` redacted, then clipped to ``max_len`` characters. Never raises.

    Redaction runs on the WHOLE text before the clip, because a secret cut in half no
    longer matches the redactor. If redaction fails, :data:`WITHHELD` is returned.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        from graph.middleware.redaction import redact

        raw = str(redact(raw))
    except Exception:  # noqa: BLE001 — fail closed
        return WITHHELD
    return raw if len(raw) <= max_len else raw[: max_len - 1] + "…"
