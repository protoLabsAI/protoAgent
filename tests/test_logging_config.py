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
    return any(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers)


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


# ── bounded in-process log ring (#3168) ──────────────────────────────────────
# The fleet-diagnostics log source. `agent.log` could not be it: that file is a raw
# stdout/stderr redirect the supervisor sets up for hub-spawned LOCAL children, so a
# remote/foreground/desktop member has none. The ring is in-process, hence uniform.


def _emit(buf, message: str, *, level: int = logging.INFO, logger: str = "probe") -> None:
    buf.emit(logging.LogRecord(logger, level, __file__, 1, message, None, None))


def test_ring_buffer_is_bounded_by_construction():
    """The cap holds regardless of what a reader asks for — it is the deque's maxlen,
    not a check the endpoint has to remember to perform."""
    from observability.logging_config import RingBufferHandler

    buf = RingBufferHandler(3)
    for i in range(10):
        _emit(buf, f"line {i}")
    assert [r["message"] for r in buf.snapshot(100)] == ["line 7", "line 8", "line 9"]
    assert buf.capacity() == 3


def test_ring_buffer_snapshot_is_oldest_first_and_limited():
    from observability.logging_config import RingBufferHandler

    buf = RingBufferHandler(10)
    for i in range(5):
        _emit(buf, f"line {i}")
    assert [r["message"] for r in buf.snapshot(2)] == ["line 3", "line 4"]
    assert buf.snapshot(0) == []


def test_ring_buffer_captures_exception_text():
    from observability.logging_config import RingBufferHandler

    buf = RingBufferHandler(5)
    try:
        raise ValueError("boom")
    except ValueError:
        logger = logging.getLogger("probe")
        record = logger.makeRecord("probe", logging.ERROR, __file__, 1, "failed", None, __import__("sys").exc_info())
        buf.emit(record)
    message = buf.snapshot(1)[0]["message"]
    assert "failed" in message and "ValueError: boom" in message


def test_ring_buffer_zero_capacity_retains_nothing():
    """``LOG_BUFFER_LINES=0`` is a real opt-out — an operator who does not want log
    content held in memory at all must get exactly that."""
    from observability.logging_config import RingBufferHandler

    buf = RingBufferHandler(0)
    _emit(buf, "line")
    assert buf.snapshot(10) == []
    assert buf.capacity() == 0


def test_ring_capacity_env_parsing(monkeypatch):
    from observability.logging_config import _RING_DEFAULT_CAPACITY, _RING_MAX_CAPACITY, _ring_capacity

    monkeypatch.delenv("LOG_BUFFER_LINES", raising=False)
    assert _ring_capacity() == _RING_DEFAULT_CAPACITY
    monkeypatch.setenv("LOG_BUFFER_LINES", "42")
    assert _ring_capacity() == 42
    monkeypatch.setenv("LOG_BUFFER_LINES", "0")
    assert _ring_capacity() == 0
    # Garbage keeps the default: logging setup runs at import and must never be the
    # reason a process fails to boot.
    monkeypatch.setenv("LOG_BUFFER_LINES", "not-a-number")
    assert _ring_capacity() == _RING_DEFAULT_CAPACITY
    monkeypatch.setenv("LOG_BUFFER_LINES", "999999999")
    assert _ring_capacity() == _RING_MAX_CAPACITY


def test_configure_logging_attaches_exactly_one_ring(monkeypatch):
    from observability.logging_config import RingBufferHandler

    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging()
    configure_logging()
    rings = [h for h in logging.getLogger().handlers if isinstance(h, RingBufferHandler)]
    assert len(rings) == 1


def test_reconfiguring_preserves_buffered_history(monkeypatch):
    """Switching the formatter must not throw away the logs an operator is about to
    read — the ring is re-attached, never rebuilt."""
    from observability import logging_config

    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging()
    buf = logging_config.log_buffer()
    marker = "keep me across reconfigure"
    _emit(buf, marker)

    monkeypatch.setenv("LOG_FORMAT", "json")
    try:
        configure_logging()
        # The SAME buffer object, still holding the record written before the switch.
        # Asserted by identity + content rather than by a count: this ring is
        # process-wide, so in a full-suite run it is long since full at capacity and a
        # new record EVICTS the oldest instead of growing the buffer.
        assert logging_config.log_buffer() is buf
        assert marker in [r["message"] for r in buf.snapshot(10_000)]
    finally:
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        configure_logging()


def test_ring_does_not_evict_other_root_handlers(caplog):
    """The module's contract is that it swaps only ITS OWN handler. Adding a second one
    must not regress that — a blunt reconfigure would break pytest's caplog and any
    host application's routing."""
    configure_logging()
    with caplog.at_level(logging.INFO, logger="ring.probe"):
        logging.getLogger("ring.probe").info("still captured")
    assert "still captured" in caplog.text


def test_ring_buffer_caps_a_single_huge_record():
    """Capacity alone doesn't bound memory — one dumped payload can be megabytes, so
    N records of unbounded size is an unbounded buffer."""
    from observability.logging_config import _RING_MAX_MESSAGE_CHARS, RingBufferHandler

    buf = RingBufferHandler(5)
    _emit(buf, "x" * (_RING_MAX_MESSAGE_CHARS * 3))
    message = buf.snapshot(1)[0]["message"]
    assert len(message) < _RING_MAX_MESSAGE_CHARS + 100
    assert "more chars truncated" in message
