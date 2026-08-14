"""``_timed_boot_phase`` (#2674) — the timing helper `_init_langgraph_agent` wraps
each builder call with. Same ``time.monotonic()`` idiom as `AuditMiddleware`'s
tool-call timing, applied to boot instead of a turn."""

from __future__ import annotations

import dataclasses

import pytest

import server.agent_init as ai
from observability import metrics


def test_timed_boot_phase_records_the_measured_elapsed_time(monkeypatch):
    # Deterministic clock instead of a real sleep. The original version slept 10ms
    # and asserted the result was `> 0`, which fails at random on the native
    # Windows runner: under Python 3.12 `time.monotonic()` is backed by
    # `GetTickCount64()` with ~15.6ms granularity, so a 10ms sleep legitimately
    # measures as exactly 0.0. (`time.monotonic()` only moved to
    # `QueryPerformanceCounter` in 3.13.) The sibling
    # `test_timed_boot_phase_calls_record_boot_phase` already asserts `>= 0` for
    # this reason — this test was the odd one out.
    #
    # Scripting the clock also makes the assertion stronger than the one it
    # replaces: it pins the recorded value to the exact delta between the
    # helper's two reads, so a hardcoded 0.0 (or a swapped subtraction) fails
    # here, where `> 0` would have been satisfied by any positive number.
    ticks = iter([100.0, 100.25])
    last = 100.25

    def fake_monotonic() -> float:
        # Falls back to the final tick once the script is exhausted — anything
        # else running in-process (logging, pytest internals) must not blow up
        # on a StopIteration while the patch is live.
        nonlocal last
        try:
            last = next(ticks)
        except StopIteration:
            pass
        return last

    monkeypatch.setattr(ai.time, "monotonic", fake_monotonic)

    sink: dict[str, float] = {}
    with ai._timed_boot_phase("plugins", sink):
        pass

    assert "plugins" in sink
    assert sink["plugins"] == pytest.approx(0.25)


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


def test_log_boot_phase_summary_noop_on_empty_dict(caplog):
    ai._log_boot_phase_summary({})
    assert "[boot] phase timings" not in caplog.text


def test_log_boot_phase_summary_logs_every_phase(caplog):
    with caplog.at_level("INFO"):
        ai._log_boot_phase_summary({"checkpointer": 0.01, "mcp": 0.5})
    assert "checkpointer=0.01s" in caplog.text
    assert "mcp=0.50s" in caplog.text
    assert "total 0.51s" in caplog.text


@pytest.fixture
def state_guard():
    # Snapshot/restore every AppState field — _init_langgraph_agent mutates the
    # process-wide singleton (same guard tests/test_oauth_disconnected_boot.py uses).
    from runtime.state import STATE, AppState

    saved = {f.name: getattr(STATE, f.name) for f in dataclasses.fields(AppState)}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(STATE, name, value)


def test_setup_pending_boot_still_emits_the_partial_summary(tmp_path, monkeypatch, state_guard):
    # #2678-adjacent CodeRabbit finding: the setup-pending early return (no
    # .setup-complete marker, non-headless) used to skip the boot-summary log
    # entirely — a fresh install's first boot would run the checkpointer phase
    # and then say nothing about it. The helper must fire on THIS path too, not
    # just the full-graph-compile path at the bottom of _init_langgraph_agent.
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    calls = []
    monkeypatch.setattr(ai, "_log_boot_phase_summary", lambda phases: calls.append(dict(phases)))

    ai._init_langgraph_agent()

    from runtime.state import STATE

    assert STATE.graph is None  # setup wizard not completed — graphless boot
    assert len(calls) == 1
    assert "checkpointer" in calls[0]  # the one phase that runs before the early return
