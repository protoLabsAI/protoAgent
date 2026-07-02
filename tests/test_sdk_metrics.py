"""sdk.record_metric / metric_history / metric_last (#1632) — plugin metric timeseries:
small named numeric series (treasury, net worth, fleet size), namespaced
``<plugin_id>:<name>`` into one instance-dir ``metrics.db``, retention-capped per
series. The history a live-state watch verifier can't get any other way (drawdown vs
high-water, flatline detection) and the substrate for dashboard sparklines.

Exercises the REAL MetricsStore on a tmp path (connection-per-call — the concurrency
smoke below is the point: plugin engines record from worker threads, and #1500 taught
us what a shared sqlite conn across threads does)."""

from __future__ import annotations

import threading
import time

import pytest

from graph import sdk
from observability.metrics_store import MetricsStore
from runtime.state import STATE

# All sample timestamps ride on a recent base: the age trim is WALL-CLOCK based (by
# design — backfill older than the retention window is dropped on the next write), so
# bare values like ts=100.0 would be 1970 samples and age out immediately.
T0 = round(time.time() - 1000.0, 3)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = MetricsStore(str(tmp_path / "metrics.db"))
    monkeypatch.setattr(STATE, "metrics_store", s)
    return s


# --- record + history round-trip ----------------------------------------------------


def test_record_and_history_round_trip(store):
    for ts, value in ((T0 + 100.0, 10.0), (T0 + 200.0, 12.5), (T0 + 300.0, 11.0)):
        res = sdk.record_metric("credits", value, ts=ts, plugin_id="spacetraders")
        assert res["ok"] is True
        assert res["series"] == "spacetraders:credits"  # namespaced key surfaced
    # Chronological (oldest→newest) (ts, value) tuples — verifier/sparkline order.
    assert sdk.metric_history("credits", plugin_id="spacetraders") == [
        (T0 + 100.0, 10.0),
        (T0 + 200.0, 12.5),
        (T0 + 300.0, 11.0),
    ]


def test_history_since_is_inclusive(store):
    for off in (100.0, 200.0, 300.0):
        sdk.record_metric("m", off, ts=T0 + off, plugin_id="p")
    assert sdk.metric_history("m", since=T0 + 200.0, plugin_id="p") == [(T0 + 200.0, 200.0), (T0 + 300.0, 300.0)]
    assert sdk.metric_history("m", since=T0 + 301.0, plugin_id="p") == []


def test_history_limit_keeps_the_newest_window(store):
    for i in range(1, 8):  # 1..7
        sdk.record_metric("m", float(i), ts=T0 + i, plugin_id="p")
    # limit=3 → the NEWEST 3 points, still returned oldest→newest.
    assert sdk.metric_history("m", limit=3, plugin_id="p") == [(T0 + 5, 5.0), (T0 + 6, 6.0), (T0 + 7, 7.0)]


def test_history_same_ts_keeps_insert_order(store):
    sdk.record_metric("m", 1.0, ts=T0, plugin_id="p")
    sdk.record_metric("m", 2.0, ts=T0, plugin_id="p")
    assert sdk.metric_history("m", plugin_id="p") == [(T0, 1.0), (T0, 2.0)]
    assert sdk.metric_last("m", plugin_id="p") == (T0, 2.0)  # rowid breaks the tie


# --- last -----------------------------------------------------------------------------


def test_metric_last(store):
    assert sdk.metric_last("credits", plugin_id="spacetraders") is None  # never recorded
    sdk.record_metric("credits", 10.0, ts=T0 + 100.0, plugin_id="spacetraders")
    sdk.record_metric("credits", 12.0, ts=T0 + 200.0, plugin_id="spacetraders")
    assert sdk.metric_last("credits", plugin_id="spacetraders") == (T0 + 200.0, 12.0)


def test_ts_defaults_to_now(store):
    before = time.time()
    sdk.record_metric("m", 1.0, plugin_id="p")
    after = time.time()
    ts, value = sdk.metric_last("m", plugin_id="p")
    assert before <= ts <= after
    assert value == 1.0


# --- namespacing isolation --------------------------------------------------------------


def test_namespacing_isolates_plugins(store):
    # Two plugins record the SAME metric name — neither sees the other's series.
    sdk.record_metric("treasury", 100.0, ts=T0, plugin_id="spacetraders")
    sdk.record_metric("treasury", 999.0, ts=T0, plugin_id="prototrader")
    assert sdk.metric_history("treasury", plugin_id="spacetraders") == [(T0, 100.0)]
    assert sdk.metric_history("treasury", plugin_id="prototrader") == [(T0, 999.0)]
    assert sdk.metric_last("treasury", plugin_id="spacetraders") == (T0, 100.0)


def test_colon_in_plugin_id_cannot_cross_namespaces(store):
    # Plugin "a" records under name "b:credits" → series "a:b:credits". A malicious
    # plugin_id "a:b" is REJECTED outright (the #1656 precedent), so it can never
    # write or read "a:b:credits" — or anything else.
    sdk.record_metric("b:credits", 7.0, ts=T0, plugin_id="a")
    res = sdk.record_metric("credits", 666.0, plugin_id="a:b")
    assert res["ok"] is False and ":" in res["message"]
    assert sdk.metric_history("credits", plugin_id="a:b") == []
    assert sdk.metric_last("credits", plugin_id="a:b") is None
    assert sdk.metric_history("b:credits", plugin_id="a") == [(T0, 7.0)]  # untouched


# --- retention ---------------------------------------------------------------------------


def test_retention_caps_points_per_series(tmp_path, monkeypatch):
    s = MetricsStore(str(tmp_path / "metrics.db"), max_points=5)
    monkeypatch.setattr(STATE, "metrics_store", s)
    for i in range(1, 9):  # 8 points into a 5-point cap
        sdk.record_metric("m", float(i), ts=T0 + i, plugin_id="p")
    points = sdk.metric_history("m", plugin_id="p")
    assert points == [(T0 + 4, 4.0), (T0 + 5, 5.0), (T0 + 6, 6.0), (T0 + 7, 7.0), (T0 + 8, 8.0)]  # oldest trimmed
    # The cap is PER SERIES — a busy sibling series doesn't evict this one.
    sdk.record_metric("other", 1.0, ts=T0, plugin_id="p")
    assert len(sdk.metric_history("m", plugin_id="p")) == 5


def test_retention_ages_out_old_points(tmp_path, monkeypatch):
    s = MetricsStore(str(tmp_path / "metrics.db"), retention_days=1)
    monkeypatch.setattr(STATE, "metrics_store", s)
    stale = time.time() - 2 * 86400.0  # two days old, one-day cap
    sdk.record_metric("m", 1.0, ts=stale, plugin_id="p")
    sdk.record_metric("m", 2.0, plugin_id="p")  # the write that triggers the trim
    points = sdk.metric_history("m", plugin_id="p")
    assert len(points) == 1
    assert points[0][1] == 2.0


# --- input validation + degradation ------------------------------------------------------


def test_validates_inputs(store):
    assert not sdk.record_metric("", 1.0, plugin_id="p")["ok"]  # no name
    assert not sdk.record_metric("m", 1.0, plugin_id=" ")["ok"]  # no plugin_id
    assert not sdk.record_metric("m", float("nan"), plugin_id="p")["ok"]  # NaN poisons drawdown math
    assert not sdk.record_metric("m", float("inf"), plugin_id="p")["ok"]
    assert not sdk.record_metric("m", "not-a-number", plugin_id="p")["ok"]
    assert not sdk.record_metric("m", 1.0, ts="yesterday", plugin_id="p")["ok"]
    assert not sdk.record_metric("m", 1.0, ts=float("nan"), plugin_id="p")["ok"]
    assert sdk.metric_history("", plugin_id="p") == []
    assert sdk.metric_last("m", plugin_id="") is None
    assert sdk.metric_history("m", plugin_id="p") == []  # nothing slipped through


def test_degrades_without_a_store(monkeypatch):
    monkeypatch.setattr(STATE, "metrics_store", None)
    res = sdk.record_metric("m", 1.0, plugin_id="p")
    assert res["ok"] is False and "unavailable" in res["message"]
    assert sdk.metric_history("m", plugin_id="p") == []
    assert sdk.metric_last("m", plugin_id="p") is None


# --- concurrency smoke --------------------------------------------------------------------


def test_two_threads_recording_concurrently(store):
    # Connection-per-call + WAL + busy_timeout: two engine threads hammering the same
    # file (same series AND different series) must lose nothing and raise nothing.
    errors: list[Exception] = []

    def hammer(plugin_id: str) -> None:
        try:
            for i in range(50):
                res = sdk.record_metric("shared", float(i), ts=T0 + i, plugin_id=plugin_id)
                assert res["ok"] is True, res["message"]
                res = sdk.record_metric("own", float(i), plugin_id=plugin_id)
                assert res["ok"] is True, res["message"]
        except Exception as e:  # noqa: BLE001 — surfaced to the main thread below
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(pid,)) for pid in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for pid in ("alpha", "beta"):
        assert len(sdk.metric_history("shared", limit=1000, plugin_id=pid)) == 50
        assert len(sdk.metric_history("own", limit=1000, plugin_id=pid)) == 50
