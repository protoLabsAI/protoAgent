"""The hub's console presence store and its report route (#5669, part 1)."""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server
from graph import operator_presence as presence

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def clean_store():
    presence.reset()
    yield
    presence.reset()


def test_a_fresh_present_report_with_recent_input_reads_true():
    presence.record("c1", "chat-a", True, 10, now=T0)
    assert presence.operator_at("chat-a", now=T0 + 5) is True


@pytest.mark.parametrize(
    ("report", "ask", "at"),
    [
        (None, "chat-a", T0),  # no record
        (("c1", "chat-b", True, 0), "chat-a", T0),  # another session
        (("c1", "chat-a", False, 0), "chat-a", T0),  # window not on the chat
        (("c1", "chat-a", True, 0), "chat-a", T0 + 90.5),  # report went stale
        (("c1", "chat-a", True, 60.5), "chat-a", T0),  # input too old on arrival
        (("c1", "chat-a", True, 30), "chat-a", T0 + 31),  # input aged past the limit since
        (("c1", "chat-a", True, 0), "", T0),  # no session asked for
    ],
    ids=["no-record", "other-session", "not-present", "stale", "old-input", "input-aged", "empty-session"],
)
def test_everything_else_reads_false(report, ask, at):
    if report is not None:
        presence.record(*report, now=T0)
    assert presence.operator_at(ask, now=at) is False


def test_the_limits_are_inclusive():
    presence.record("c1", "chat-a", True, 0, now=T0)
    assert presence.operator_at("chat-a", now=T0 + presence.INPUT_S) is True
    presence.record("c1", "chat-a", True, 0, now=T0)
    assert presence.operator_at("chat-a", now=T0 + presence.INPUT_S + 0.01) is False


def test_a_raising_store_reads_false(monkeypatch):
    presence.record("c1", "chat-a", True, 0, now=T0)

    class Broken(dict):
        def values(self):
            raise RuntimeError("store is broken")

    monkeypatch.setattr(presence, "_records", Broken())
    assert presence.operator_at("chat-a", now=T0) is False


def test_a_console_that_moves_to_another_chat_leaves_the_first():
    presence.record("c1", "chat-a", True, 0, now=T0)
    presence.record("c1", "chat-b", True, 0, now=T0 + 1)
    assert presence.operator_at("chat-a", now=T0 + 1) is False
    assert presence.operator_at("chat-b", now=T0 + 1) is True


def test_two_consoles_on_two_chats_both_read_true():
    presence.record("c1", "chat-a", True, 0, now=T0)
    presence.record("c2", "chat-b", True, 0, now=T0)
    assert presence.operator_at("chat-a", now=T0) is True
    assert presence.operator_at("chat-b", now=T0) is True


def test_one_present_console_is_enough_when_another_is_away():
    presence.record("c1", "chat-a", False, 0, now=T0)
    presence.record("c2", "chat-a", True, 0, now=T0)
    assert presence.operator_at("chat-a", now=T0) is True


def test_input_at_never_runs_ahead_of_the_report():
    presence.record("c1", "chat-a", True, -500, now=T0)  # clamped to 0, not 500 s in the future
    assert presence.operator_at("chat-a", now=T0 + 60.5) is False


def test_the_store_is_bounded_and_evicts_the_oldest_report():
    for i in range(presence.MAX_CONSOLES):
        presence.record(f"c{i}", f"chat-{i}", True, 0, now=T0 + i)
    presence.record("c0", "chat-0", True, 0, now=T0 + 1000)  # c0 reports again: c1 is now oldest
    presence.record("new", "chat-new", True, 0, now=T0 + 1000)
    assert len(presence._records) == presence.MAX_CONSOLES
    assert "c1" not in presence._records
    assert {"c0", "c2", "new"} <= set(presence._records)


def test_default_clock_is_the_hubs(monkeypatch):
    monkeypatch.setattr(presence.time, "time", lambda: T0)
    presence.record("c1", "chat-a", True, 0)
    assert presence.operator_at("chat-a") is True
    monkeypatch.setattr(presence.time, "time", lambda: T0 + 91)
    assert presence.operator_at("chat-a") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3", 3.0), ("0.5", 0.5), ("0", 90.0), ("-2", 90.0), ("nan", 90.0), ("inf", 90.0), ("soon", 90.0), ("", 90.0)],
)
def test_stale_override_env_is_read_at_import(monkeypatch, value, expected):
    monkeypatch.setenv("PA_PRESENCE_STALE_S", value)
    try:
        assert importlib.reload(presence).STALE_S == expected
    finally:
        monkeypatch.delenv("PA_PRESENCE_STALE_S")
        assert importlib.reload(presence).STALE_S == 90.0


# --- POST /api/chat/presence ---------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    server._register_chat_presence_route(app)
    return TestClient(app)


GOOD = {"console_id": "c1", "session": "chat-a", "present": True, "input_age_s": 4}


def test_route_stores_a_valid_report(client, monkeypatch):
    monkeypatch.setattr(presence.time, "time", lambda: T0)
    r = client.post("/api/chat/presence", json=GOOD)
    assert r.status_code == 204 and r.content == b""
    assert presence.operator_at("chat-a", now=T0 + 50) is True
    assert presence.operator_at("chat-a", now=T0 + 57) is False  # input was already 4 s old


def test_route_report_of_absence_replaces_presence(client):
    assert client.post("/api/chat/presence", json=GOOD).status_code == 204
    assert client.post("/api/chat/presence", json={**GOOD, "present": False}).status_code == 204
    assert presence.operator_at("chat-a") is False


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD, "console_id": ""},
        {**GOOD, "console_id": "x" * 201},
        {**GOOD, "console_id": 7},
        {**GOOD, "session": ""},
        {**GOOD, "session": "x" * 201},
        {**GOOD, "session": None},
        {**GOOD, "present": "true"},
        {**GOOD, "present": 1},
        {**GOOD, "input_age_s": -1},
        {**GOOD, "input_age_s": "4"},
        {**GOOD, "input_age_s": True},
        {**GOOD, "input_age_s": None},
        {k: v for k, v in GOOD.items() if k != "present"},
        {k: v for k, v in GOOD.items() if k != "input_age_s"},
        [GOOD],
        "nope",
    ],
)
def test_route_rejects_a_malformed_body(client, body):
    assert client.post("/api/chat/presence", json=body).status_code == 400
    assert presence._records == {}


@pytest.mark.parametrize("raw", ["", "{not json", '{"console_id":"c1","session":"s","present":true,"input_age_s":NaN}',
                                 '{"console_id":"c1","session":"s","present":true,"input_age_s":Infinity}',
                                 '{"console_id":"c1","session":"s","present":true,"input_age_s":1e999}'])
def test_route_rejects_unparseable_or_non_finite_bodies(client, raw):
    r = client.post("/api/chat/presence", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert presence._records == {}


def test_route_accepts_ids_at_the_length_limit(client):
    body = {**GOOD, "console_id": "c" * 200, "session": "s" * 200}
    assert client.post("/api/chat/presence", json=body).status_code == 204
    assert presence.operator_at("s" * 200) is True


def test_route_is_mounted_by_the_server_and_hidden_from_the_schema(client):
    import inspect

    assert "_register_chat_presence_route(fastapi_app)" in inspect.getsource(server)
    assert "/api/chat/presence" not in client.get("/openapi.json").json().get("paths", {})
