"""Log formatting — human-readable (default) or JSON lines for aggregators.

``server/__init__.py`` calls :func:`configure_logging` once at import. The
default keeps the historic stdlib format
(``%(asctime)s %(levelname)s %(name)s %(message)s``) — fine for a human tailing
``docker logs``. Set ``LOG_FORMAT=json`` to emit one JSON object per line
instead, which parse-stable aggregators (Loki, CloudWatch, Datadog, …) can index
without a grok pattern. Level (``LOG_LEVEL``, default ``INFO``) and the stream
(standard error, via ``StreamHandler``) are unchanged from the previous
``basicConfig`` call regardless of format.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

_HUMAN_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Attributes a bare LogRecord already carries. Anything outside this set that a
# caller attached via ``extra=`` is emitted as a top-level JSON field so
# structured context survives the JSON formatter (the human format drops it).
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object.

    Stable keys (``ts``, ``level``, ``logger``, ``message``) plus the exception
    type + rendered traceback when the record carries ``exc_info``, plus any
    ``extra=`` fields the caller attached.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", str(record.exc_info[0]))
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


# The single handler this module owns on the root logger. Tracked so a re-call
# swaps the formatter (human ⇄ JSON) by replacing *our* handler only — NOT every
# root handler (a blunt basicConfig(force=True) would also evict handlers added by
# pytest's caplog or a host application, breaking log capture / their routing).
_handler: logging.Handler | None = None

# ---------------------------------------------------------------------------
# Bounded in-process log buffer (#3168)
# ---------------------------------------------------------------------------
# The fleet diagnostics endpoint needs a log source that exists on EVERY member,
# whatever its deployment shape. ``agent.log`` cannot be it: that file is a raw
# stdout/stderr fd redirect set up by ``graph/fleet/supervisor.start()`` for
# hub-spawned LOCAL children only (``Popen(..., stdout=logf, stderr=logf)``), so a
# registered remote member, a foreground ``python -m server``, and a desktop-run
# member have no such file at all — their stderr goes to a terminal, launchd, or
# the desktop log. An in-process ring is uniform across all of them.
#
# It is bounded by construction (``deque(maxlen=…)``), which is what lets the
# endpoint promise a hard cap without trusting a caller's ``lines=``.
_RING_DEFAULT_CAPACITY = 2000
_RING_MAX_CAPACITY = 50_000
# Per-record cap. Capacity alone does NOT bound memory: one line can be enormous (a
# dumped request/response body, a giant tool argument), so N records of unbounded size
# is an unbounded buffer. Truncating here — not at read time — is what makes the
# footprint predictable at roughly capacity × this.
_RING_MAX_MESSAGE_CHARS = 10_000


def _ring_capacity() -> int:
    """``LOG_BUFFER_LINES`` records to retain, clamped to a sane range.

    A garbage value keeps the default rather than raising — logging setup runs at
    import and must never be the reason a process fails to boot.
    """
    raw = os.environ.get("LOG_BUFFER_LINES", "").strip()
    if not raw:
        return _RING_DEFAULT_CAPACITY
    try:
        value = int(raw)
    except ValueError:
        return _RING_DEFAULT_CAPACITY
    if value <= 0:
        return 0
    return min(value, _RING_MAX_CAPACITY)


class RingBufferHandler(logging.Handler):
    """Retain the last N log records in memory for the diagnostics endpoint.

    Stores structured fields rather than pre-rendered lines so a reader can serve
    text or JSON and apply redaction at read time. Formatting happens here (while
    the record's args are alive), redaction does NOT — scrubbing every record on
    the hot logging path would tax all logging to serve a rare read.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=max(0, capacity))

    def emit(self, record: logging.LogRecord) -> None:
        if self._records.maxlen == 0:
            return
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            elif record.exc_text:
                message = f"{message}\n{record.exc_text}"
            if len(message) > _RING_MAX_MESSAGE_CHARS:
                dropped = len(message) - _RING_MAX_MESSAGE_CHARS
                message = f"{message[:_RING_MAX_MESSAGE_CHARS]}… [{dropped} more chars truncated]"
            self._records.append(
                {
                    "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )
        except Exception:  # noqa: BLE001 — a logging handler must never raise into the caller
            self.handleError(record)

    def snapshot(self, limit: int) -> list[dict[str, Any]]:
        """The most recent ``limit`` records, oldest first.

        Copies under the handler lock — the stdlib pattern; logging's own ``handle()``
        takes this same lock around ``emit()``, so reader and writer are mutually
        excluded.

        Defensive rather than a fixed bug: in principle ``list()`` over a deque a writer
        is evicting from can raise ``RuntimeError: deque mutated during iteration``, but
        that could NOT be provoked on CPython-with-GIL (four writer threads against
        20k copies of a 10k deque), where the copy is effectively atomic. The lock costs
        nothing and a free-threaded build removes that accidental protection, so it is
        taken here rather than relied upon not to matter.
        """
        if limit <= 0:
            return []
        self.acquire()
        try:
            items = list(self._records)
        finally:
            self.release()
        return items[-limit:]

    def capacity(self) -> int:
        return self._records.maxlen or 0

    def clear(self) -> None:
        self.acquire()
        try:
            self._records.clear()
        finally:
            self.release()


# Created ONCE and re-attached (never rebuilt) by ``configure_logging`` — a second
# call switching the formatter must not silently discard the buffered history.
_ring: RingBufferHandler | None = None


def log_buffer() -> RingBufferHandler | None:
    """The live ring buffer, or None when logging was never configured."""
    return _ring


def configure_logging() -> None:
    """Install/refresh the root log handler from ``LOG_LEVEL`` + ``LOG_FORMAT``.

    ``LOG_FORMAT=json`` → :class:`JsonFormatter`; anything else (default) keeps
    the historic human format. Re-entrant: a second call replaces the handler
    this module previously installed (so the formatter can switch) and leaves any
    other root handlers untouched.
    """
    global _handler
    handler = logging.StreamHandler()  # defaults to sys.stderr, as basicConfig did
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_HUMAN_FORMAT))

    root = logging.getLogger()
    if _handler is not None and _handler in root.handlers:
        root.removeHandler(_handler)
        _handler.close()
    root.addHandler(handler)

    # The ring is built once and only re-attached on a re-call, so switching the
    # formatter keeps the buffered history. Capacity is read at first build; a
    # changed LOG_BUFFER_LINES takes effect on the next process, matching how
    # LOG_LEVEL/LOG_FORMAT behave for anything already emitted.
    global _ring
    if _ring is None:
        _ring = RingBufferHandler(_ring_capacity())
    if _ring not in root.handlers:
        root.addHandler(_ring)

    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    _handler = handler
