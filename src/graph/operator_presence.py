"""Where the operator is right now, as reported by the consoles (#5669).

A console that has a chat selected reports, on a short interval, whether its window is
focused on that chat (``present``) and how long ago the operator last typed or clicked
there (``input_age_s``). The hub keeps the latest report per console, in memory, and
``operator_at(session_id)`` answers "is someone actively at this chat?".

This is the only evidence of presence. An open ``GET /api/chat/attend`` stream is NOT
evidence (a desktop app keeps a chat open all day; see #5663 and its revert), and this
module neither reads nor changes it.

The answer fails open: no report, a stale report, an old input, or any error all read as
"not there", so the phone still gets the push.

Clocks: ``received_at`` is the hub's ``time.time()`` when the report arrived, and
``input_at`` is derived from it (``received_at - input_age_s``), so a console with a wrong
clock cannot make itself look fresher than it is.

Test seam: the environment variable ``PA_PRESENCE_STALE_S``, read once at import,
overrides ``STALE_S`` when it is a positive finite float. Real-process tests use it to see
a report go stale without waiting 90 seconds. Anything else is ignored.

Lives in ``graph/`` so ``graph/decisions.py`` can read it without importing server code.
"""

from __future__ import annotations

import math
import os
import threading
import time

# A report older than this no longer counts: the console stopped reporting (closed, asleep,
# offline).
STALE_S = 90.0
# The operator's last input in the chat must be at most this old.
INPUT_S = 60.0
# One record per console; past this the console that reported longest ago is dropped.
MAX_CONSOLES = 256


def _env_stale_s(default: float) -> float:
    raw = os.environ.get("PA_PRESENCE_STALE_S")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


STALE_S = _env_stale_s(STALE_S)

_lock = threading.Lock()
# console_id -> {"session", "present", "input_at", "received_at"}
_records: dict[str, dict] = {}


def record(
    console_id: str,
    session: str,
    present: bool,
    input_age_s: float,
    now: float | None = None,
) -> None:
    """Store ``console_id``'s latest report, replacing its previous one."""
    received_at = time.time() if now is None else float(now)
    age = float(input_age_s)
    if not math.isfinite(age) or age < 0:
        age = 0.0
    entry = {
        "session": session,
        "present": bool(present),
        "input_at": received_at - age,
        "received_at": received_at,
    }
    with _lock:
        _records.pop(console_id, None)
        _records[console_id] = entry
        while len(_records) > MAX_CONSOLES:
            oldest = min(_records, key=lambda cid: _records[cid]["received_at"])
            del _records[oldest]


def operator_at(session_id: str, now: float | None = None) -> bool:
    """True only when some console freshly reports the operator active in ``session_id``.

    Needs a record on that session with ``present`` true, received within ``STALE_S`` and
    with input within ``INPUT_S``. Everything else is False, including any error here.
    """
    try:
        if not session_id:
            return False
        current = time.time() if now is None else float(now)
        with _lock:
            entries = list(_records.values())
        for entry in entries:
            if (
                entry["session"] == session_id
                and entry["present"] is True
                and current - entry["received_at"] <= STALE_S
                and current - entry["input_at"] <= INPUT_S
            ):
                return True
        return False
    except Exception:
        return False


def reset() -> None:
    """Forget every report (tests)."""
    with _lock:
        _records.clear()
