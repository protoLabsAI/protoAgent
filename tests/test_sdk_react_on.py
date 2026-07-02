"""graph.sdk.react_on — reactive-rule sugar over registry.on + run_in_session (#1633).

Uses a REAL EventBus (with no running loop, ``publish`` delivers inline on the calling
thread — exactly the threaded delivery path the debounce must survive) and a captured
``run_in_session``. Debounce assertions are event-driven (``threading.Event``), not
wall-clock guesses.
"""

from __future__ import annotations

import threading

import pytest

from events.bus import EventBus


@pytest.fixture
def rig(monkeypatch):
    """A real bus wired into HOST.on + a captured run_in_session; yields (bus, calls, fired)."""
    from graph import sdk
    from graph.plugins.host import HOST

    bus = EventBus()
    monkeypatch.setattr(HOST, "on", bus.subscribe_handler)

    calls: list[tuple] = []
    fired = threading.Event()

    def fake_run_in_session(session_id, prompt, *, delay_seconds=0.0, job_id=None):
        calls.append((session_id, prompt, job_id))
        fired.set()
        return {"ok": True, "job_id": job_id, "fires_at": "now", "message": "enqueued"}

    monkeypatch.setattr(sdk, "run_in_session", fake_run_in_session)
    return bus, calls, fired


def test_react_on_enqueues_a_turn_from_an_event(rig):
    from graph import sdk

    bus, calls, _ = rig
    unsub = sdk.react_on(
        "spacetraders.opportunity",
        session="chat-1",
        prompt=lambda ev: f"Route {ev['data']['route']} appeared. Evaluate it.",
        job_id="st-opportunity",
    )
    bus.publish("spacetraders.opportunity", {"route": "X1->X2"})
    assert calls == [("chat-1", "Route X1->X2 appeared. Evaluate it.", "st-opportunity")]
    unsub()


def test_react_on_defaults_to_the_activity_session(rig):
    from events import ACTIVITY_CONTEXT
    from graph import sdk

    bus, calls, _ = rig
    unsub = sdk.react_on("notes.*", prompt=lambda ev: "react", job_id="j1")
    bus.publish("notes.created", {})
    assert calls == [(ACTIVITY_CONTEXT, "react", "j1")]
    unsub()


def test_react_on_skips_when_prompt_returns_none_or_empty(rig):
    from graph import sdk

    bus, calls, _ = rig
    unsub = sdk.react_on("st.*", prompt=lambda ev: ev["data"].get("text"), job_id="j1")
    bus.publish("st.tick", {})  # prompt -> None
    bus.publish("st.tick", {"text": "  "})  # whitespace-only counts as empty
    assert calls == []
    bus.publish("st.tick", {"text": "go"})
    assert [c[1] for c in calls] == ["go"]
    unsub()


def test_react_on_debounce_coalesces_a_burst_into_one_turn_last_event_wins(rig):
    from graph import sdk

    bus, calls, fired = rig
    unsub = sdk.react_on(
        "st.opportunity",
        prompt=lambda ev: f"margin {ev['data']['margin']}",
        job_id="st-opp",
        debounce_s=0.08,
    )
    for margin in (5, 9, 42):
        bus.publish("st.opportunity", {"margin": margin})
    assert calls == []  # nothing fires inside the window
    assert fired.wait(3.0), "debounced turn never fired"
    assert calls == [(sdk.ACTIVITY_CONTEXT, "margin 42", "st-opp")]  # ONE turn, last event wins
    unsub()


def test_react_on_debounce_is_threadsafe_under_concurrent_delivery(rig):
    """The bus publish path is threadsafe — with no loop bound, each thread delivers
    the handler inline, so this exercises real concurrent timer-reset contention."""
    from graph import sdk

    bus, calls, fired = rig
    unsub = sdk.react_on("st.*", prompt=lambda ev: f"n={ev['data']['n']}", job_id="j", debounce_s=0.15)

    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def _publish(i: int) -> None:
        barrier.wait()
        bus.publish("st.burst", {"n": i})

    threads = [threading.Thread(target=_publish, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fired.wait(3.0), "debounced turn never fired"
    assert len(calls) == 1  # the whole concurrent burst coalesced into one turn
    unsub()


def test_react_on_skipped_events_do_not_extend_or_clobber_the_debounce_window(rig):
    from graph import sdk

    bus, calls, fired = rig
    unsub = sdk.react_on("st.*", prompt=lambda ev: ev["data"].get("text"), job_id="j", debounce_s=0.05)
    bus.publish("st.a", {"text": "real"})
    bus.publish("st.a", {})  # filtered — must not clobber or re-arm the pending text
    assert fired.wait(3.0)
    assert [c[1] for c in calls] == ["real"]
    unsub()


def test_react_on_unsubscribe_stops_delivery(rig):
    from graph import sdk

    bus, calls, _ = rig
    unsub = sdk.react_on("st.*", prompt=lambda ev: "go", job_id="j")
    bus.publish("st.a", {})
    assert len(calls) == 1
    unsub()
    bus.publish("st.a", {})
    assert len(calls) == 1  # no further delivery
    unsub()  # idempotent


def test_react_on_unsubscribe_cancels_a_pending_debounce_timer(rig):
    from graph import sdk

    bus, calls, fired = rig
    unsub = sdk.react_on("st.*", prompt=lambda ev: "go", job_id="j", debounce_s=0.05)
    bus.publish("st.a", {})
    unsub()  # inside the window — the armed timer must be canceled
    assert not fired.wait(0.3), "canceled debounce timer still fired"
    assert calls == []


def test_react_on_without_a_host_bus_is_a_warned_noop(monkeypatch, caplog):
    import logging

    from graph import sdk
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "on", None)
    with caplog.at_level(logging.WARNING, logger="graph.sdk"):
        unsub = sdk.react_on("st.*", prompt=lambda ev: "go", job_id="j")
    assert "no event bus wired" in caplog.text
    unsub()  # callable no-op, nothing raises


def test_react_on_validates_inputs(rig):
    from graph import sdk

    with pytest.raises(TypeError):
        sdk.react_on("st.*", prompt="not-callable", job_id="j")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        sdk.react_on("st.*", prompt=lambda ev: "go", job_id="  ")


def test_react_on_logs_but_survives_an_enqueue_failure(rig, monkeypatch, caplog):
    import logging

    from graph import sdk

    bus, _calls, _ = rig
    monkeypatch.setattr(
        sdk,
        "run_in_session",
        lambda *a, **kw: {"ok": False, "message": "scheduler unavailable — cannot enqueue a turn"},
    )
    unsub = sdk.react_on("st.*", prompt=lambda ev: "go", job_id="j")
    with caplog.at_level(logging.WARNING, logger="graph.sdk"):
        bus.publish("st.a", {})
        bus.publish("st.a", {})  # the rule survives — still subscribed after a failure
    assert caplog.text.count("could not enqueue turn") == 2
    unsub()
