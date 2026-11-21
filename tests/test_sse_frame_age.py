"""An event frame says how old it is on the server's clock (so a console never has to
trust its own clock to tell news from catch-up)."""
import json

from operator_api import routes


def _frame(evt):
    text = routes._sse_frame(evt)
    return text, json.loads(text.split("data: ", 1)[1])


def test_frame_carries_its_age_on_the_servers_clock(monkeypatch):
    monkeypatch.setattr(routes.time, "time", lambda: 1_000_000.0)
    text, live = _frame({"event": "goal.achieved", "data": {"id": 1}, "seq": 7, "ts": 999_999.5})
    assert text.startswith("id: 7\n")
    assert live == {"topic": "goal.achieved", "data": {"id": 1}, "seq": 7, "ts": 999_999.5, "age_s": 0.5}
    _, replayed = _frame({"event": "goal.achieved", "data": {}, "seq": 8, "ts": 1_000_000.0 - 3600})
    assert replayed["age_s"] == 3600.0


def test_an_event_from_the_future_is_age_zero_and_no_timestamp_means_no_age(monkeypatch):
    monkeypatch.setattr(routes.time, "time", lambda: 1_000_000.0)
    _, ahead = _frame({"event": "x", "data": {}, "seq": 1, "ts": 1_000_050.0})
    assert ahead["age_s"] == 0.0
    _, bare = _frame({"event": "x", "data": {}})
    assert "ts" not in bare and "age_s" not in bare
