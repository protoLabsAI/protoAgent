"""``_timed_boot_phase`` (#2674) — the timing helper `_init_langgraph_agent` wraps
each builder call with. Same ``time.monotonic()`` idiom as `AuditMiddleware`'s
tool-call timing, applied to boot instead of a turn."""

from __future__ import annotations

import time

import pytest

import server.agent_init as ai
from observability import metrics


def test_timed_boot_phase_records_into_the_sink():
    sink: dict[str, float] = {}
    with ai._timed_boot_phase("plugins", sink):
        time.sleep(0.01)
    assert "plugins" in sink
    assert sink["plugins"] > 0


def test_timed_boot_phase_records_even_when_the_body_raises(monkeypatch):
    # A phase that fails (e.g. a bad plugin) must still be timed — that's exactly
    # the diagnostic case #2674 cares about, and the sibling phases already
    # completed shouldn't lose their data because a later one raised.
    sink: dict[str, float] = {}
    with pytest.raises(RuntimeError):
        with ai._timed_boot_phase("graph_compile", sink):
            raise RuntimeError("boom")
    assert "graph_compile" in sink


def test_timed_boot_phase_calls_record_boot_phase(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(metrics, "record_boot_phase", lambda phase, duration_s: calls.append((phase, duration_s)))
    with ai._timed_boot_phase("mcp"):
        pass
    assert len(calls) == 1
    assert calls[0][0] == "mcp"
    assert calls[0][1] >= 0


def test_timed_boot_phase_sink_is_optional():
    # No sink passed — must not raise (server boot's real call sites all pass one,
    # but the helper itself doesn't require it).
    with ai._timed_boot_phase("checkpointer"):
        pass
