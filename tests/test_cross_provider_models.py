"""Every lane's models in one list, so a slot can be offered a choice (#2550 follow-up).

`POST /api/config/models` answers "what does THIS provider offer" — the wizard's
question. The picker's question is different: "what can I choose from at all", across
the gateway and every signed-in subscription, so a slot can run Claude while the main
brain runs on the gateway.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.providers import discovery


def _cfg(**over) -> LangGraphConfig:
    return LangGraphConfig(**{"api_base": "https://gw.example.com/v1", "api_key": "gw-key", **over})


@pytest.fixture
def lanes(monkeypatch):
    """Gateway configured, Claude signed in, Codex signed out — the mixed real case."""
    monkeypatch.setattr(discovery, "list_gateway_models", lambda base, key: (["protolabs/coder"], ""), raising=False)
    monkeypatch.setattr("graph.config_io.list_gateway_models", lambda base, key: (["protolabs/coder"], ""))
    monkeypatch.setattr(
        discovery,
        "oauth_status",
        lambda p: discovery.OAuthStatus(
            p, p == "anthropic-oauth", "instance_store", "", "" if p == "anthropic-oauth" else "Sign in first."
        ),
    )
    monkeypatch.setattr(
        discovery, "list_provider_models", lambda provider, cfg: (["claude-sonnet-5", "claude-haiku-4-5"], "")
    )


def test_every_lane_is_reported_including_the_ones_you_cant_use(lanes):
    """An unusable lane comes back with a reason rather than being omitted — "sign in to
    use ChatGPT" is a better answer than silence, and the difference between "not
    available" and "not offered" is the whole point of the picker."""
    got = {lane["provider"]: lane for lane in discovery.available_model_lanes(_cfg())}

    assert set(got) == {"gateway", "anthropic-oauth", "openai-codex"}
    assert got["gateway"]["configured"] and got["gateway"]["models"] == ["protolabs/coder"]
    assert got["anthropic-oauth"]["configured"]
    assert not got["openai-codex"]["configured"]
    assert "Sign in" in got["openai-codex"]["error"]
    assert got["anthropic-oauth"]["label"] == "Claude subscription"


def test_options_are_qualified_even_with_a_single_lane(monkeypatch):
    """An unqualified name silently means "whatever model.provider is", so a saved slot
    would change lanes the day the operator switches providers. Naming the lane makes
    the choice durable."""
    monkeypatch.setattr("graph.config_io.list_gateway_models", lambda base, key: (["protolabs/coder"], ""))
    monkeypatch.setattr(discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, False, "", "", "no"))

    assert discovery.qualified_model_options(_cfg()) == ["gateway:protolabs/coder"]


def test_options_span_the_lanes_and_dedupe(lanes):
    opts = discovery.qualified_model_options(_cfg())

    assert opts == [
        "gateway:protolabs/coder",
        "anthropic-oauth:claude-sonnet-5",
        "anthropic-oauth:claude-haiku-4-5",
    ]
    assert len(set(opts)) == len(opts)


def test_one_lanes_outage_never_blanks_the_others(monkeypatch):
    """A picker that goes empty because one credential expired is worse than a picker
    that says which lane is down."""

    def _boom(base, key):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr("graph.config_io.list_gateway_models", _boom)
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, p == "anthropic-oauth", "s", "", "")
    )
    monkeypatch.setattr(discovery, "list_provider_models", lambda provider, cfg: (["claude-sonnet-5"], ""))

    got = {lane["provider"]: lane for lane in discovery.available_model_lanes(_cfg())}

    assert "unreachable" in got["gateway"]["error"] and got["gateway"]["models"] == []
    assert got["anthropic-oauth"]["models"] == ["claude-sonnet-5"]
    assert discovery.qualified_model_options(_cfg()) == ["anthropic-oauth:claude-sonnet-5"]


def test_an_unconfigured_gateway_is_not_probed(monkeypatch):
    """No key means no network call — a blank gateway must not cost a timeout on every
    settings render."""

    def _must_not_run(base, key):
        raise AssertionError("probed a gateway with no key")

    monkeypatch.setattr("graph.config_io.list_gateway_models", _must_not_run)
    monkeypatch.setattr(discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, False, "", "", "no"))
    cfg = _cfg(api_key="")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    got = {lane["provider"]: lane for lane in discovery.available_model_lanes(cfg)}

    assert not got["gateway"]["configured"]
    assert "No gateway key" in got["gateway"]["error"]
