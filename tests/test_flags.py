"""Developer flags (ADR 0068, slice 1) — the registry + resolution.

Freezes the tier-vs-channel matrix, the env-override precedence, and channel derivation.
Tests monkeypatch ``flags.FLAGS`` to a hermetic registry and wipe flag/channel env vars."""

from __future__ import annotations

import datetime
import os
import types
import warnings

import pytest

from runtime import flags
from runtime.flags import Flag


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Wipe any real flag/channel/instance env so resolution is deterministic per test."""
    for k in list(os.environ):
        if k.startswith("PROTOAGENT_FLAG_") or k in (
            "PROTOAGENT_CHANNEL",
            "PROTOAGENT_INSTANCE",
            "PROTOAGENT_AUTO_SCOPE",
        ):
            monkeypatch.delenv(k, raising=False)
    yield


def _use(monkeypatch, *registry: Flag) -> None:
    monkeypatch.setattr(flags, "FLAGS", list(registry))


# ── tier vs channel ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "tier,channel,expected",
    [
        ("on", "prod", True), ("on", "beta", True), ("on", "dev", True),
        ("beta", "prod", False), ("beta", "beta", True), ("beta", "dev", True),
        ("dev", "prod", False), ("dev", "beta", False), ("dev", "dev", True),
        ("off", "prod", False), ("off", "beta", False), ("off", "dev", False),
    ],
)
def test_tier_vs_channel(monkeypatch, tier, channel, expected):
    _use(monkeypatch, Flag("x", "d", tier=tier))
    assert flags.flag_enabled("x", channel=channel) is expected


def test_unregistered_flag_is_off(monkeypatch):
    _use(monkeypatch)  # empty registry
    assert flags.flag_enabled("nope", channel="dev") is False


# ── env override precedence ─────────────────────────────────────────────────────

def test_env_override_forces_state(monkeypatch):
    _use(monkeypatch, Flag("chat.new", "d", tier="off"))
    monkeypatch.setenv("PROTOAGENT_FLAG_CHAT_NEW", "on")
    assert flags.flag_enabled("chat.new", channel="prod") is True  # off tier, env forces on
    monkeypatch.setenv("PROTOAGENT_FLAG_CHAT_NEW", "0")
    assert flags.flag_enabled("chat.new", channel="dev") is False  # on-in-dev, env forces off


def test_env_key_derivation():
    assert flags._env_key("chat.new_dashboard") == "PROTOAGENT_FLAG_CHAT_NEW_DASHBOARD"


# ── channel derivation ──────────────────────────────────────────────────────────

def test_channel_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_CHANNEL", "beta")
    assert flags.current_channel() == "beta"


def test_channel_dev_sandbox_instance(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")  # the dev sandbox (ADR 0065)
    assert flags.current_channel() == "dev"


def test_channel_from_config_field(monkeypatch):
    import graph.sdk

    monkeypatch.setattr(graph.sdk, "config", lambda: types.SimpleNamespace(developer_channel="beta"))
    assert flags.current_channel() == "beta"


def test_channel_defaults_to_prod(monkeypatch):
    import graph.sdk

    monkeypatch.setattr(graph.sdk, "config", lambda: types.SimpleNamespace(developer_channel=""))
    assert flags.current_channel() == "prod"


def test_bad_channel_value_ignored(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_CHANNEL", "banana")  # not a real channel → ignored
    import graph.sdk

    monkeypatch.setattr(graph.sdk, "config", lambda: types.SimpleNamespace(developer_channel=""))
    assert flags.current_channel() == "prod"


# ── the API payload ─────────────────────────────────────────────────────────────

def test_resolved_flags_payload(monkeypatch):
    _use(monkeypatch, Flag("a.b", "desc", tier="beta", owner="me", remove_by="v1.0"))
    out = flags.resolved_flags(channel="beta")
    assert out["channel"] == "beta"
    assert out["flags"] == [
        {
            "id": "a.b", "description": "desc", "tier": "beta", "owner": "me",
            "remove_by": "v1.0", "enabled": True, "source": "channel",
        }
    ]
    # an env override flips enabled + tags the source.
    monkeypatch.setenv("PROTOAGENT_FLAG_A_B", "off")
    flipped = flags.resolved_flags(channel="dev")["flags"][0]
    assert flipped["enabled"] is False and flipped["source"] == "env"


# ── registry hygiene (guards the real FLAGS as it grows) ────────────────────────

def test_real_registry_is_well_formed():
    ids = [f.id for f in flags.FLAGS]
    assert len(ids) == len(set(ids)), "duplicate flag id in FLAGS"
    for f in flags.FLAGS:
        assert f.tier in ("off", "dev", "beta", "on"), f"{f.id}: invalid tier {f.tier!r}"
        assert f.id and f.description, "every flag needs an id + description"


def _overdue_flags(registry, today: str) -> list[str]:
    """Flags in ``registry`` whose ISO-date `remove_by` is before ``today``. `remove_by` values
    that aren't ISO dates (e.g. a version like "v2.0") can't be compared, so they're skipped."""
    overdue = []
    for f in registry:
        rb = (f.remove_by or "").strip()
        try:
            datetime.date.fromisoformat(rb)  # only date-form remove_by is auto-checked
        except ValueError:
            continue
        if rb < today:  # ISO dates sort chronologically as strings
            overdue.append(f"{f.id} (remove_by {rb}, owner {f.owner or '?'})")
    return overdue


def test_overdue_flags_warn_and_never_fail_ci():
    """The cleanup contract (ADR 0068 D6): a flag past its `remove_by` is debt to graduate or
    delete. This warns instead of failing, because a test that reads the clock turns red on a
    date with no code change, which blocks every PR on main."""
    for line in _overdue_flags(flags.FLAGS, datetime.date.today().isoformat()):
        warnings.warn(f"overdue developer flag, graduate or delete: {line}", stacklevel=1)


def test_overdue_flags_are_found_by_date():
    registry = [
        Flag(id="past", description="", owner="kj", remove_by="2026-01-01"),
        Flag(id="today", description="", remove_by="2026-06-01"),
        Flag(id="future", description="", remove_by="2027-01-01"),
        Flag(id="version", description="", remove_by="v2.0"),
        Flag(id="unset", description=""),
    ]
    assert _overdue_flags(registry, "2026-06-01") == ["past (remove_by 2026-01-01, owner kj)"]
