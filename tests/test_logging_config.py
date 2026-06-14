"""Tests for the opt-in JSON log formatter (LOG_FORMAT=json) — #876.

Locks in that the JSON formatter emits one parse-stable object per record with
the stable keys aggregators index, carries exception tracebacks, passes through
``extra=`` fields, and that configure_logging() only switches to JSON when the
env opts in.
"""

from __future__ import annotations

import json
import logging

from observability.logging_config import JsonFormatter, configure_logging


def _record(**kw) -> logging.LogRecord:
    defaults = dict(
        name="protoagent.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    defaults.update(kw)
    return logging.LogRecord(func="t", **defaults)


def test_json_formatter_emits_stable_keys():
    out = JsonFormatter().format(_record())
    obj = json.loads(out)  # raises if not valid single-line JSON
    assert obj["level"] == "INFO"
    assert obj["logger"] == "protoagent.server"
    assert obj["message"] == "hello world"  # %-args interpolated
    assert "ts" in obj
    assert "\n" not in out


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record(level=logging.ERROR, msg="failed", args=(), exc_info=sys.exc_info())
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["exc_type"] == "ValueError"
    assert "ValueError: boom" in obj["exc"]
    assert "Traceback" in obj["exc"]


def test_json_formatter_passes_through_extra_fields():
    rec = _record()
    rec.thread_id = "s-123"  # what logging's extra= attaches
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["thread_id"] == "s-123"


def _has_json_handler() -> bool:
    # Scan all root handlers (pytest's caplog handler also lives here) rather than
    # assume ours is first — configure_logging appends ours.
    return any(
        isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers
    )


def test_configure_logging_human_by_default(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging()
    assert not _has_json_handler()


def test_configure_logging_json_when_opted_in(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()
    try:
        assert _has_json_handler()
    finally:
        # Restore the human default so this test doesn't leak JSON logging into
        # the rest of the session.
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        configure_logging()
