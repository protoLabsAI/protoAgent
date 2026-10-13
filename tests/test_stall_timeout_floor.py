"""A non-zero stall window can't undercut the liveness cadence (#4168).

``/<workflow>`` and ``/<subagent>`` runs report progress at most every
``PROGRESS_MIN_INTERVAL_S`` (5s). A ``model.turn_stall_timeout_seconds`` below that cadence
stopped a run that was still working between two rate-limited frames. A non-zero window
is now floored at 3x the cadence (one WARNING per configured value), the Settings API
refuses a value under it, and 0 still turns the guard off.
"""

from __future__ import annotations

import logging

import pytest

import graph.turn_liveness as liveness
import server.chat_dispatch as chat_dispatch
from graph.settings_schema import _BY_KEY, validate_flat
from graph.turn_liveness import MIN_STALL_TIMEOUT_S, PROGRESS_MIN_INTERVAL_S, effective_stall_timeout

KEY = "model.turn_stall_timeout_seconds"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(liveness, "_warned_stall_floor", set())


def _floor_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "floor" in r.getMessage()]


def test_the_floor_is_three_frames_of_the_dispatchers_real_cadence():
    assert chat_dispatch._PROGRESS_MIN_INTERVAL_S == PROGRESS_MIN_INTERVAL_S == 5.0
    assert MIN_STALL_TIMEOUT_S == 3 * PROGRESS_MIN_INTERVAL_S == 15.0


@pytest.mark.parametrize("configured", [0, 0.0, None, -5])
def test_zero_still_disables_the_guard_and_never_warns(configured, caplog):
    caplog.set_level(logging.WARNING)
    assert effective_stall_timeout(configured) <= 0  # `_stall_guarded`: seconds <= 0 = off
    assert _floor_warnings(caplog) == []


@pytest.mark.parametrize("configured", [MIN_STALL_TIMEOUT_S, 60, 900.0])
def test_a_window_at_or_above_the_floor_is_used_as_is(configured, caplog):
    caplog.set_level(logging.WARNING)
    assert effective_stall_timeout(configured) == configured
    assert _floor_warnings(caplog) == []


@pytest.mark.parametrize("configured", [0.4, 2, 5, 14.9])
def test_a_non_zero_window_below_the_floor_is_raised_to_it(configured):
    assert effective_stall_timeout(configured) == MIN_STALL_TIMEOUT_S


def test_the_clamp_warns_once_per_value_naming_the_value_used(caplog):
    caplog.set_level(logging.WARNING)

    for _ in range(3):  # read per turn: the same bad value must not warn every turn
        effective_stall_timeout(2)
    (msg,) = _floor_warnings(caplog)
    assert "turn_stall_timeout_seconds=2s" in msg and "using 15s" in msg

    effective_stall_timeout(4)  # live-reloaded to another bad value: say so again
    assert len(_floor_warnings(caplog)) == 2


def test_settings_api_refuses_a_window_under_the_floor():
    ok, err = validate_flat({KEY: 5})
    assert not ok and "0 (off) or at least 15s" in err


@pytest.mark.parametrize("value", [0, MIN_STALL_TIMEOUT_S, 900])
def test_settings_api_accepts_zero_and_the_floor_and_up(value):
    assert validate_flat({KEY: value}) == (True, None)


def test_the_settings_help_text_names_the_floor():
    assert "at least 15s" in _BY_KEY[KEY].description and "0 disables" in _BY_KEY[KEY].description
