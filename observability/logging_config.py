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
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    _handler = handler
