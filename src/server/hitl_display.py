"""Host-side validation of a HITL interrupt's optional ``display`` (ADR 0118).

A paused tool (``ask_human`` / ``request_user_input`` / a ``run_command`` approval) may
attach a ``display`` object to its interrupt value: **inert data the console renders next
to the prompt**, never an inline artifact and never code that runs. HITL alone resumes the
tool; the display changes nothing about the pause's prose, options or resume value.

This first delivery supports exactly one built-in shape — a bounded ``table``::

    display = {"component": "table",
               "props": {"columns": ["Name", "Age"],
                         "rows": [["Ada", 36], ["Alan", 41]]}}

Registered/custom component execution and ``keyvalue`` are deliberately out of scope here;
an unknown component is simply dropped. :func:`validate_display` returns a **fresh,
sanitized copy** of a valid table (so the caller never shares references with, or mutates,
the inbound payload) or ``None`` for anything malformed, unknown, oversized or not
JSON-scalar-safe. The seam in ``server/chat.py::_interrupt_payload`` drops only the
``display`` on ``None`` and keeps the pause fully answerable.

The limits below are **fixed and documented** — a display that trips any of them is dropped
whole rather than truncated, because a half-rendered table is worse than none.
"""

from __future__ import annotations

import json
import math

# The one built-in component this delivery renders. An unknown/registered/custom kind is
# dropped (no arbitrary plugin code is loaded for a HITL display — ADR 0118 D1).
_SUPPORTED_COMPONENT = "table"

# ── Fixed dimension / size ceilings ────────────────────────────────────────────────────
# A HITL table is a small, glanceable aid beside a prompt, not a data export: keep it
# bounded so a pathological payload can never bloat the parked interrupt or the wire frame.
MAX_COLUMNS = 16          # header cells
MAX_ROWS = 100            # body rows
MAX_HEADER_CHARS = 200    # per column-header string
MAX_CELL_CHARS = 1000     # per string cell
MAX_DISPLAY_BYTES = 64 * 1024  # serialized ceiling for the whole {component, props} blob


def _is_scalar_cell(cell: object) -> bool:
    """A JSON scalar safe to render verbatim: ``None``, ``bool``, a bounded ``str``, any
    ``int``, or a **finite** ``float``. Non-finite numbers (``NaN``/``±Infinity``) are not
    JSON and never render as an answer, so they're rejected; dicts/lists/other types are
    not scalars. ``bool`` is checked before ``int`` (it's an ``int`` subclass) and before
    the ``str`` length gate."""
    if cell is None or isinstance(cell, bool):
        return True
    if isinstance(cell, str):
        return len(cell) <= MAX_CELL_CHARS
    if isinstance(cell, int):
        return True
    if isinstance(cell, float):
        return math.isfinite(cell)
    return False


def validate_display(display: object) -> dict | None:
    """Return a sanitized copy of a valid bounded ``table`` display, or ``None``.

    ``None`` is returned (and the caller drops ``display`` entirely) for anything that is
    not the supported built-in table shaped within every fixed limit: a non-object display,
    an unknown component, non-object props, missing/!list ``columns``/``rows``, a non-string
    or over-long header, a ragged row, a non-scalar cell, a non-finite number, excessive
    dimensions or cell lengths, or an oversized serialized blob.

    On success the return is a **brand-new** ``{"component": "table", "props": {"columns":
    [...], "rows": [...]}}`` built from vetted fields only (extra keys on ``display``/``props``
    are not copied forward), so the caller never mutates or aliases the inbound payload.
    """
    if not isinstance(display, dict):
        return None
    if display.get("component") != _SUPPORTED_COMPONENT:
        return None
    props = display.get("props")
    if not isinstance(props, dict):
        return None

    columns = props.get("columns")
    rows = props.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    if len(columns) > MAX_COLUMNS or len(rows) > MAX_ROWS:
        return None

    clean_columns: list[str] = []
    for col in columns:
        if not isinstance(col, str) or len(col) > MAX_HEADER_CHARS:
            return None
        clean_columns.append(col)
    width = len(clean_columns)

    clean_rows: list[list] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != width:  # ragged rows are rejected
            return None
        for cell in row:
            if not _is_scalar_cell(cell):
                return None
        clean_rows.append(list(row))

    out = {"component": _SUPPORTED_COMPONENT, "props": {"columns": clean_columns, "rows": clean_rows}}

    # Serialized-size ceiling, measured on the vetted copy: catches an in-bounds *shape*
    # whose aggregate content is still too large to carry on a parked interrupt. The copy
    # holds only JSON scalars (non-finite floats already rejected), so dumps can't raise on
    # it; the guard stays defensive.
    try:
        if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_DISPLAY_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return out
