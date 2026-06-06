"""Opt-in CIDR allowlist for outbound A2A destinations (#572) — callbacks +
peer_consult. Unset = permissive (today's behavior); when set, a destination is
allowed iff every resolved IP is inside a listed CIDR. Uses IP-literal URLs so
no DNS is involved."""

import pytest

import security
import a2a_stores
from graph.config import LangGraphConfig


@pytest.fixture(autouse=True)
def _reset_allowlist():
    security.set_callback_allowlist([])
    yield
    security.set_callback_allowlist([])


# ── the allowlist primitive ─────────────────────────────────────────────────

def test_unset_is_permissive():
    assert security.is_enabled() is False
    assert security.check_url("http://8.8.8.8/cb") is None  # no opinion when off


def test_in_allowlist_allowed_out_blocked():
    security.set_callback_allowlist(["10.0.0.0/8", "100.64.0.0/10"])
    assert security.is_enabled() is True
    assert security.check_url("http://10.5.6.7/cb") is None       # in 10/8
    assert security.check_url("https://100.64.1.1:8443/x") is None  # in tailnet
    blocked = security.check_url("http://8.8.8.8/cb")             # public, not listed
    assert blocked and "not in the callback allowlist" in blocked


def test_non_http_and_malformed_rejected_when_enabled():
    security.set_callback_allowlist(["10.0.0.0/8"])
    assert "non-http" in (security.check_url("ftp://10.0.0.1/x") or "")
    assert security.check_url("file:///etc/passwd")  # rejected (some error)


def test_malformed_cidr_is_ignored():
    security.set_callback_allowlist(["10.0.0.0/8", "not-a-cidr", ""])
    assert security.allowlist() == ["10.0.0.0/8"]


# ── config parse ────────────────────────────────────────────────────────────

def test_config_parses_security_section(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("security:\n  callback_allowlist:\n    - 10.0.0.0/8\n    - 100.64.0.0/10\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.security_callback_allowlist == ["10.0.0.0/8", "100.64.0.0/10"]


def test_config_empty_security_section_tolerated(tmp_path):
    # an all-commented / value-less `security:` parses to None — must not throw
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("model:\n  provider: openai\nsecurity:\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.security_callback_allowlist == []


# ── push-callback integration (a2a_stores.is_safe_webhook_url) ──────────────

def test_callback_default_denylist_when_allowlist_unset():
    # unset → existing private-IP denylist holds; public is fine
    assert a2a_stores.is_safe_webhook_url("http://10.0.0.1/cb") is False  # RFC1918 denied
    assert a2a_stores.is_safe_webhook_url("http://8.8.8.8/cb") is True    # public ok


def test_callback_allowlist_overrides_denylist_and_restricts():
    security.set_callback_allowlist(["10.0.0.0/8"])
    # in-allowlist private IP is now PERMITTED (allowlist overrides the denylist)
    assert a2a_stores.is_safe_webhook_url("http://10.5.6.7/cb") is True
    # a public IP NOT in the allowlist is now REJECTED (positive allowlist is the policy)
    assert a2a_stores.is_safe_webhook_url("http://8.8.8.8/cb") is False
