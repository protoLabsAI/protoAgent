"""Soft spend limit: one typed operator notice per UTC day (ADR 0135 D8/D11/D12)."""
from datetime import UTC, datetime, timedelta

import pytest

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict
from graph.decisions import DecisionStore
from graph.plugins import chat_surface
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE
from server import turn_control

DAY = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.fixture
def soft(tmp_path, monkeypatch):
    config = LangGraphConfig(spend_cap_enabled=True, spend_cap_daily_cost_hard_limit=10,
                             spend_cap_daily_token_hard_limit=1000, spend_cap_daily_cost_soft_limit=2,
                             spend_cap_daily_token_soft_limit=100)
    telemetry = TelemetryStore(str(tmp_path / "telemetry.db"))
    clock = [DAY]
    meter = telemetry.daily_autonomous_spend
    monkeypatch.setattr(telemetry, "daily_autonomous_spend", lambda origins: meter(origins, now=clock[0]))
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(STATE, "telemetry_store", telemetry)
    monkeypatch.setattr(STATE, "decision_store", DecisionStore(tmp_path / "decisions.db", clock=lambda: clock[0]))
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    monkeypatch.setattr("graph.sdk.console_url", lambda: "http://console.test/")
    monkeypatch.setattr("security.public_console.public_origin", lambda: "")
    sent = []
    # The real seam: a registered dispatcher receives the typed ChatNotice.
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": sent.append})
    return config, telemetry, clock, sent


def record(store, when, *, origin="scheduler", cost=0, tokens=0):
    store.record({"task_id": "turn", "origin": origin, "cost_usd": cost, "total_tokens": tokens,
                  "ended_at": when.isoformat(), "state": "completed"})


def admit(origin="scheduler"):
    return turn_control.autonomous_admission_error(origin, "job")


@pytest.mark.parametrize("cost,tokens,named", [(2, 0, "$2 cost"), (0, 100, "100 token")])
def test_passing_a_soft_limit_sends_one_typed_notice_that_day(soft, cost, tokens, named):
    _, telemetry, clock, sent = soft
    record(telemetry, clock[0], cost=cost / 2, tokens=tokens // 2)
    assert admit() is None and sent == []  # under both soft limits
    record(telemetry, clock[0], cost=cost / 2, tokens=tokens // 2)
    assert admit() is None  # a soft limit never refuses the turn
    assert len(sent) == 1
    notice = sent[0]
    assert (notice.kind, notice.importance) == ("notify", turn_control._SPEND_SOFT_NOTICE_IMPORTANCE)
    assert notice.link == "http://console.test/app/"
    assert notice.text.startswith("Unattended work has passed") and named in notice.text
    assert "Settings > Spend cap" in notice.text  # the operator's next step
    # Further autonomous turns that day, from any origin and after more spend: none.
    record(telemetry, clock[0], cost=1, tokens=50)
    for origin in ("scheduler", "watch", "inbox"):
        assert admit(origin) is None
    assert len(sent) == 1


def test_next_day_notifies_again_and_marker_survives_restart(soft, monkeypatch):
    _, telemetry, clock, sent = soft
    record(telemetry, clock[0], tokens=100)
    admit()
    # Reattach to the same file, as after a restart: the day's notice is not repeated.
    reopened = TelemetryStore(telemetry.path)
    meter = reopened.daily_autonomous_spend
    monkeypatch.setattr(reopened, "daily_autonomous_spend", lambda origins: meter(origins, now=clock[0]))
    monkeypatch.setattr(STATE, "telemetry_store", reopened)
    admit()
    assert len(sent) == 1
    clock[0] += timedelta(days=1)
    admit()
    assert len(sent) == 1  # a new day starts under the limit
    record(reopened, clock[0], cost=2)
    admit()
    admit()
    assert len(sent) == 2


@pytest.mark.parametrize("change", [
    {"spend_cap_enabled": False},
    {"spend_cap_daily_cost_soft_limit": 0.0, "spend_cap_daily_token_soft_limit": 0},
])
def test_no_notice_when_cap_disabled_or_no_soft_limit(soft, change):
    config, telemetry, clock, sent = soft
    for name, value in change.items():
        setattr(config, name, value)
    record(telemetry, clock[0], cost=5, tokens=500)
    assert admit() is None and sent == []
    # Nothing was claimed while off, so turning it on that day still warns once.
    config.spend_cap_enabled, config.spend_cap_daily_token_soft_limit = True, 100
    admit()
    assert len(sent) == 1


def test_operator_turns_and_operator_spend_never_notify(soft):
    _, telemetry, clock, sent = soft
    record(telemetry, clock[0], origin="api-chat", cost=50, tokens=5000)
    assert admit() is None and admit("api-chat") is None
    record(telemetry, clock[0], tokens=100)
    assert admit("api-chat") is None and sent == []


def test_hard_limit_decision_stands_in_for_the_soft_notice(soft):
    _, telemetry, clock, sent = soft
    record(telemetry, clock[0], tokens=1000)  # straight past soft AND hard
    assert admit() and sent == []
    rec = STATE.decision_store.list()[0]
    STATE.decision_store.answer(rec.id, "resume")
    assert admit() is None and sent == []  # the operator already answered for today


def test_notice_failure_never_blocks_admission(soft, monkeypatch):
    _, telemetry, clock, sent = soft
    record(telemetry, clock[0], tokens=100)

    def fail(day, kind):
        raise OSError("locked")

    monkeypatch.setattr(telemetry, "claim_daily_notice", fail)
    assert admit() is None and sent == []


def test_soft_limits_default_off_and_round_trip():
    assert LangGraphConfig().spend_cap_daily_cost_soft_limit == 0
    assert LangGraphConfig().spend_cap_daily_token_soft_limit == 0
    block = {"enabled": True, "daily_cost_soft_limit": 1.5, "daily_token_soft_limit": 250}
    out = config_to_dict(LangGraphConfig.from_dict({"spend_cap": block}, _plugin_config={}))["spend_cap"]
    assert {key: out[key] for key in block} == block
    unset = LangGraphConfig.from_dict(
        {"spend_cap": {"daily_cost_soft_limit": None, "daily_token_soft_limit": None}}, _plugin_config={})
    assert (unset.spend_cap_daily_cost_soft_limit, unset.spend_cap_daily_token_soft_limit) == (0.0, 0)
