"""Tests for headless setup validation (ADR 0010)."""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.config_io import validate_for_headless


@pytest.fixture(autouse=True)
def _no_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield


def test_ok_with_api_base_and_config_key():
    ok, reason = validate_for_headless(LangGraphConfig(api_key="sk-test"))
    assert ok and reason == "ok"


def test_ok_with_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    ok, _ = validate_for_headless(LangGraphConfig(api_key=""))
    assert ok


def test_fail_without_any_key():
    ok, reason = validate_for_headless(LangGraphConfig(api_key=""))
    assert not ok and "api_key" in reason


def test_fail_without_api_base():
    ok, reason = validate_for_headless(LangGraphConfig(api_base="", api_key="sk-test"))
    assert not ok and "api_base" in reason


def test_marker_roundtrip(tmp_path, monkeypatch):
    # Point the instance root at a temp location so we don't touch the real marker.
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    import graph.config_io as cio

    assert cio.is_setup_complete() is False
    cio.mark_setup_complete()
    assert cio.is_setup_complete() is True
    cio.reset_setup()
    assert cio.is_setup_complete() is False


# #4262 — a provider-QUALIFIED lead routes by its prefix, not by `model.provider`. Fleet members
# boot headless, so a hub on `anthropic-oauth:<model>` used to spawn members that exited at boot.
def test_ok_with_a_native_oauth_qualified_lead(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict({"model": {"name": "anthropic-oauth:claude-sonnet-5-5"}})
    assert validate_for_headless(cfg) == (True, "ok")


def test_a_qualified_lead_is_validated_on_its_own_connection(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    gw = {"id": "gateway", "type": "openai-compat", "base_url": "https://gw.example/v1"}
    lead = {"name": "gateway:protolabs/smart"}
    # Its endpoint is enough — no legacy model.api_key/api_base needed (keyless sends a placeholder).
    assert validate_for_headless(LangGraphConfig.from_dict({"providers": [gw], "model": lead})) == (True, "ok")
    # A connection with no endpoint can't serve the lead.
    ok, reason = validate_for_headless(LangGraphConfig.from_dict({"providers": [dict(gw, base_url="")], "model": lead}))
    assert not ok and "gateway" in reason
