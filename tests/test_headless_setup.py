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
