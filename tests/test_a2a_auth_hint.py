"""A 401 from an A2A peer says what to do about it (#3042 live finding).

A tokenless delegate presents the fleet service token on LOOPBACK ONLY (ADR 0089) — the
token must never leave the box. So an off-box delegate sends no credential at all, by
design, and 401s. The bare passthrough ("HTTP 401: Unauthorized: expected 'Authorization:
Bearer '") tells the operator that something is unauthorized but not that the cause is a
deliberate decision they had no way to know about.
"""

from __future__ import annotations

from plugins.delegates.adapters import Delegate, _a2a_auth_hint


def _d(**kw) -> Delegate:
    d = Delegate(name=kw.pop("name", "protoEngineer"), type="a2a")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_an_off_box_401_names_the_field_and_the_reason():
    hint = _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token=""), 401)
    assert "no Auth token" in hint
    assert "never leaves the box" in hint
    assert "federation_token" in hint  # the least-privilege option, named first


def test_a_403_gets_the_same_hint():
    assert _a2a_auth_hint(_d(url="http://peer.lan:7875/a2a", auth_token=""), 403)


def test_a_configured_token_gets_no_hint():
    """The credential was sent and rejected — a wrong token, not a missing one. Telling
    the operator to set the field they already set would send them the wrong way."""
    assert _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token="secret"), 401) == ""


def test_a_loopback_401_gets_a_DIFFERENT_hint():
    """Loopback should have presented the service token, so a 401 there means the lookup
    failed rather than the token being withheld. Different cause, different fix."""
    hint = _a2a_auth_hint(_d(url="http://127.0.0.1:7875/a2a", auth_token=""), 401)
    assert "loopback delegate" in hint
    assert "could not be resolved" in hint
    assert "never leaves the box" not in hint  # that explanation would be wrong here


def test_localhost_counts_as_loopback():
    assert "loopback delegate" in _a2a_auth_hint(_d(url="http://localhost:7875/a2a"), 401)


def test_a_non_auth_error_gets_no_hint():
    for code in (400, 404, 429, 500, 503):
        assert _a2a_auth_hint(_d(url="http://192.168.1.20:7875/a2a", auth_token=""), code) == ""
